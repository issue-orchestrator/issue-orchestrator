"""Durable escrow outside disposable worktrees; no discovery of source artifacts."""

import hashlib
import os
import stat
import uuid
from pathlib import Path

from ..domain.exact_git import RefPinOutcome
from ..domain.validated_work_escrow import (
    ARTIFACT_FILENAMES,
    EscrowArtifacts,
    EscrowProblem,
    EscrowReport,
    evidence_artifacts,
    evidence_pins,
    validate_capture_locations,
)
from ..domain.validated_work_store import EvidenceAdmission, EvidenceRow
from ..ports.exact_git import ExactGit
from .validated_work_envelope import decode_envelope, encode_envelope


def fsync_directory(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable_directory(path: Path) -> None:
    if path.exists():
        if path.is_symlink() or not path.is_dir():
            raise ValueError("escrow directory must not be a symlink or file")
        return
    durable_directory(path.parent)
    path.mkdir(exist_ok=True)
    fsync_directory(path.parent)


def read_regular(path: Path, size: int) -> bytes:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            raise ValueError(f"artifact is not a regular file of expected size: {path}")
        data = stream.read(size + 1)
    if len(data) != size:
        raise ValueError(f"artifact changed while reading: {path}")
    return data


def write_durable(path: Path, data: bytes) -> None:
    with path.open("xb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())


class FilesystemValidatedWorkEscrow:
    """Owns immutable bytes and exact pins; store owns admission and evidence roles."""

    def __init__(
        self, root: Path, *, repository: Path, repo_slug: str, git: ExactGit
    ) -> None:
        self.root = root.absolute()
        self._repository = repository.resolve()
        self._repo_slug = repo_slug
        self._git = git
        if self.root.resolve() != self.root:
            raise ValueError("escrow root must be a canonical path without symlinks")
        for worktree in git.linked_worktrees(self._repository):
            if self.root.is_relative_to(
                worktree.resolve()
            ) or self._repository.is_relative_to(worktree.resolve()):
                raise ValueError(
                    "escrow and repository must be outside disposable worktrees"
                )

    def _directory(self, locator: str) -> Path:
        path = self.root / locator
        if (
            not path.is_relative_to(self.root)
            or path.resolve() != path
            or path == self.root
        ):
            raise ValueError("escrow locator must be contained without symlinks")
        return path

    def _validate_admission(self, admission: EvidenceAdmission) -> None:
        validate_capture_locations(admission)
        if admission.evidence.identity.key.repo_slug != self._repo_slug:
            raise ValueError("capture belongs to another repository")

    def inspect(self, locator: str) -> EvidenceAdmission:
        directory = self._directory(locator)
        envelope = directory / "capture.json"
        size = envelope.lstat().st_size
        if size > 1_048_576:
            raise ValueError("capture envelope exceeds size limit")
        admission = decode_envelope(read_regular(envelope, size))
        self._validate_admission(admission)
        if admission.escrow_dir != locator:
            raise ValueError("capture directory does not match identity")
        for artifact in evidence_artifacts(admission.evidence):
            data = read_regular(
                directory / ARTIFACT_FILENAMES[artifact.slot], artifact.byte_size
            )
            if hashlib.sha256(data).hexdigest() != artifact.sha256:
                raise ValueError(f"artifact hash mismatch: {artifact.slot}")
        return admission

    def capture(
        self, admission: EvidenceAdmission, sources: EscrowArtifacts
    ) -> EvidenceAdmission:
        """Escrow, then pin. Replay returns the original immutable envelope."""
        self._validate_admission(admission)
        envelope = encode_envelope(admission)
        directory = self._directory(admission.escrow_dir)
        if directory.exists():
            original = self.inspect(admission.escrow_dir)
        else:
            self._write_capture(directory, admission, sources, envelope)
            original = self.inspect(admission.escrow_dir)
        self.ensure_pins(original)
        return original

    def _write_capture(
        self,
        directory: Path,
        admission: EvidenceAdmission,
        sources: EscrowArtifacts,
        envelope: bytes,
    ) -> None:
        durable_directory(directory.parent)
        staging_root = self.root / ".tmp"
        durable_directory(staging_root)
        staging = staging_root / uuid.uuid4().hex
        durable_directory(staging)
        for artifact in evidence_artifacts(admission.evidence):
            data = read_regular(sources.for_slot(artifact.slot), artifact.byte_size)
            if hashlib.sha256(data).hexdigest() != artifact.sha256:
                raise ValueError(f"source artifact hash mismatch: {artifact.slot}")
            write_durable(staging / ARTIFACT_FILENAMES[artifact.slot], data)
        write_durable(staging / "capture.json", envelope)
        fsync_directory(staging)
        try:
            os.rename(staging, directory)
        except OSError:
            if not directory.exists():
                raise
            # A concurrent capture may have won. Never replace its envelope.
            self.inspect(admission.escrow_dir)
            return
        fsync_directory(directory.parent)
        fsync_directory(staging_root)

    def ensure_pins(self, admission: EvidenceAdmission) -> None:
        self._validate_admission(admission)
        for ref, sha in evidence_pins(admission.evidence):
            outcome = self._git.pin_ref(self._repository, ref=ref, sha=sha)
            if outcome not in {RefPinOutcome.PINNED, RefPinOutcome.ALREADY_PINNED}:
                raise ValueError(f"{ref}: {outcome.value}; escrow retained")

    def verify_pins(self, admission: EvidenceAdmission) -> None:
        for ref, sha in evidence_pins(admission.evidence):
            if not self._git.verify_ref(self._repository, ref=ref, sha=sha):
                raise ValueError(f"pin missing or mismatched: {ref}; escrow retained")

    def verifies(self, evidence: EvidenceRow) -> bool:
        try:
            original = self.inspect(evidence.admission.escrow_dir)
            if (
                original.evidence.identity,
                original.pinned_ref,
                original.observed_ref,
            ) != (
                evidence.admission.evidence.identity,
                evidence.admission.pinned_ref,
                evidence.admission.observed_ref,
            ):
                return False
            self.verify_pins(original)
            return True
        except (OSError, ValueError, KeyError, TypeError, RuntimeError):
            return False

    def inventory(self) -> tuple[tuple[str, ...], EscrowReport]:
        locators: list[str] = []
        problems: list[EscrowProblem] = []
        if not self.root.exists():
            return (), EscrowReport()
        for issue in sorted(self.root.iterdir()):
            if issue.name == ".tmp" and issue.is_dir() and not issue.is_symlink():
                for partial in issue.iterdir():
                    problems.append(
                        EscrowProblem(
                            str(partial.relative_to(self.root)),
                            "partial capture retained",
                        )
                    )
            elif issue.name.isdecimal() and issue.is_dir() and not issue.is_symlink():
                locators.extend(
                    str(entry.relative_to(self.root))
                    for entry in sorted(issue.iterdir())
                )
            else:
                problems.append(
                    EscrowProblem(issue.name, "unrecognized escrow entry retained")
                )
        return tuple(locators), EscrowReport(problems=tuple(problems))

    def orphan_pins(
        self, admissions: tuple[EvidenceAdmission, ...]
    ) -> tuple[EscrowProblem, ...]:
        known = {
            ref
            for admission in admissions
            for ref, _ in evidence_pins(admission.evidence)
        }
        return tuple(
            EscrowProblem(ref.name, "pin has no valid capture envelope; retained")
            for ref in self._git.retained_refs(self._repository)
            if ref.name not in known
        )

    def release(self, evidence: EvidenceRow) -> None:
        """Called only under the store's resolved-record retention transaction."""
        original = self.inspect(evidence.admission.escrow_dir)
        if not self.verifies(evidence):
            raise ValueError("invalid retention evidence; retained")
        directory = self._directory(original.escrow_dir)
        allowed = {
            "capture.json",
            *(
                ARTIFACT_FILENAMES[a.slot]
                for a in evidence_artifacts(original.evidence)
            ),
        }
        if {p.name for p in directory.iterdir()} != allowed:
            raise ValueError("unexpected escrow contents; retained")
        # No recursive deletion: a publication workspace needs its later owner.
        for ref, sha in evidence_pins(original.evidence):
            self._git.delete_pinned_ref(self._repository, ref=ref, sha=sha)
        for filename in allowed:
            (directory / filename).unlink()
        directory.rmdir()
        fsync_directory(directory.parent)
