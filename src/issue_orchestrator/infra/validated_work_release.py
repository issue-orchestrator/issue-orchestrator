"""Evidence-bound write-ahead release receipts, under store retention authority."""

import hashlib
import os
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ..domain.validated_work import canonical_json
from ..domain.validated_work_escrow import (
    ARTIFACT_FILENAMES,
    evidence_artifacts,
    evidence_pins,
)
from ..domain.validated_work_store import EvidenceAdmission, EvidenceRow
from ..ports.exact_git import ExactGit
from .escrow_files import (
    durable_directory,
    fsync_directory,
    read_regular,
    write_durable,
)
from .validated_work_codec import load_document
from .validated_work_envelope import decode_envelope, encode_envelope


@dataclass(frozen=True)
class ReleaseProgress:
    original: EvidenceAdmission
    intent: int

    def encode(self) -> bytes:
        payload = {
            "version": 1,
            "capture": encode_envelope(self.original).decode(),
            "intent": self.intent,
        }
        return canonical_json(
            {
                **payload,
                "checksum": hashlib.sha256(
                    canonical_json(payload).encode()
                ).hexdigest(),
            }
        ).encode()

    @classmethod
    def decode(cls, data: bytes) -> "ReleaseProgress":
        payload = load_document(data)
        result = cls(decode_envelope(payload["capture"].encode()), payload["intent"])
        if (
            type(result.intent) is not int
            or result.intent < 0
            or result.encode() != data
        ):
            raise ValueError("invalid durable release progress")
        return result


class EscrowRelease:
    """Only the store's rechecked, locked eligibility may call this owner.

    Each durable intent precedes exactly one destructive step. The receipt is
    kept after filesystem completion so a failed SQLite commit remains resumable.
    Missing future items, changed items and unrecognized contents always refuse.
    """

    def __init__(
        self, *, root: Path, directory: Path, repository: Path, git: ExactGit
    ) -> None:
        self._root = root
        self._directory = directory
        self._repository = repository
        self._git = git

    def release(
        self,
        evidence: EvidenceRow,
        *,
        inspect: Callable[[str], EvidenceAdmission],
        verifies: Callable[[EvidenceRow], bool],
    ) -> None:
        receipts = self._root / ".releases"
        if receipts.resolve() != receipts:
            raise ValueError("release receipts must not be symlinked")
        receipt = receipts / (evidence.evidence_id + ".json")
        if receipt.is_symlink():
            raise ValueError("release receipt must not be symlinked")
        if receipt.exists():
            progress = ReleaseProgress.decode(
                read_regular(receipt, receipt.lstat().st_size)
            )
        else:
            original = inspect(evidence.admission.escrow_dir)
            if not verifies(evidence):
                raise ValueError("invalid retention evidence; retained")
            progress = ReleaseProgress(original, 0)
            self._validate_remaining(ReleaseProgress(original, -1))
            durable_directory(receipts)
            self._save(receipt, progress)
        original = progress.original
        if (
            original.evidence.identity,
            original.escrow_dir,
            original.pinned_ref,
            original.observed_ref,
        ) != (
            evidence.admission.evidence.identity,
            evidence.admission.escrow_dir,
            evidence.admission.pinned_ref,
            evidence.admission.observed_ref,
        ):
            raise ValueError("release receipt belongs to different evidence")
        self._validate_remaining(progress)
        count = self._count(original)
        if progress.intent > count:
            raise ValueError("release progress exceeds its evidence plan")
        # Recheck/delete previously intended items too: their removal may not
        # have survived the interruption. Never regress durable authorization.
        for step in range(count):
            self._save(receipt, ReleaseProgress(original, max(progress.intent, step)))
            self._delete_step(original, step)
        self._save(receipt, ReleaseProgress(original, count))

    def _files(self, original: EvidenceAdmission) -> tuple[tuple[str, int, str], ...]:
        envelope = encode_envelope(original)
        return (
            *(
                (ARTIFACT_FILENAMES[a.slot], a.byte_size, a.sha256)
                for a in evidence_artifacts(original.evidence)
            ),
            ("capture.json", len(envelope), hashlib.sha256(envelope).hexdigest()),
        )

    def _count(self, original: EvidenceAdmission) -> int:
        return len(evidence_pins(original.evidence)) + len(self._files(original)) + 1

    def _save(self, receipt: Path, progress: ReleaseProgress) -> None:
        staging = receipt.parent / (".pending-" + uuid.uuid4().hex)
        write_durable(staging, progress.encode())
        os.replace(staging, receipt)
        fsync_directory(receipt.parent)

    def _validate_remaining(self, progress: ReleaseProgress) -> None:
        self._validate_pins(progress)
        self._validate_files(progress)

    def _validate_pins(self, progress: ReleaseProgress) -> None:
        pins = evidence_pins(progress.original.evidence)
        for step, (ref, sha) in enumerate(pins):
            present = self._git.read_pinned_ref(self._repository, ref=ref)
            if present is None and step <= progress.intent:
                continue
            if not self._git.verify_ref(self._repository, ref=ref, sha=sha):
                raise ValueError(
                    "release pin missing or changed without durable intent"
                )

    def _validate_files(self, progress: ReleaseProgress) -> None:
        files = self._files(progress.original)
        for step, (name, size, sha) in enumerate(
            files, start=len(evidence_pins(progress.original.evidence))
        ):
            path = self._directory / name
            if path.is_symlink():
                raise ValueError("release artifact must not be symlinked")
            if not path.exists() and step <= progress.intent:
                continue
            if hashlib.sha256(read_regular(path, size)).hexdigest() != sha:
                raise ValueError("release artifact changed; retained")
        if self._directory.exists():
            if not {p.name for p in self._directory.iterdir()} <= {f[0] for f in files}:
                raise ValueError("unexpected escrow contents; retained")
        elif progress.intent < self._count(progress.original) - 1:
            raise ValueError("capture directory missing without durable release intent")

    def _delete_step(self, original: EvidenceAdmission, step: int) -> None:
        pins, files = evidence_pins(original.evidence), self._files(original)
        if step < len(pins):
            ref, sha = pins[step]
            if self._git.read_pinned_ref(self._repository, ref=ref) is not None:
                self._git.delete_pinned_ref(self._repository, ref=ref, sha=sha)
        elif step < len(pins) + len(files):
            path = self._directory / files[step - len(pins)][0]
            if path.exists():
                path.unlink()
            if self._directory.exists():
                fsync_directory(self._directory)
        else:
            if self._directory.exists():
                self._directory.rmdir()
            fsync_directory(self._directory.parent)
