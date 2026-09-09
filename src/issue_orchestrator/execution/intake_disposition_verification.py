"""Real object and artifact verification for historical admission lineage."""

from hashlib import sha256
from pathlib import Path

from ..domain.validated_work_store import AncestryRelation, CommitReference, EvidenceRow
from ..ports.git import Git
from .completion_intake_artifacts import read_regular


class IntakeDispositionVerification:
    def __init__(self, repo_root: Path, state_root: Path, git: Git) -> None:
        self._repo = repo_root
        self._state = state_root
        self._git = git

    def _exists(self, reference: CommitReference) -> bool:
        result = self._git.run(
            self._repo,
            ["rev-parse", "--verify", reference.pinned_ref + "^{commit}"],
            check=False,
        )
        return (
            result.returncode == 0
            and result.stdout.strip() == reference.key.validated_head_sha
        )

    def compare(
        self, left: CommitReference, right: CommitReference
    ) -> AncestryRelation:
        exists = (self._exists(left), self._exists(right))
        if exists != (True, True):
            return {
                (False, False): AncestryRelation.BOTH_UNREACHABLE,
                (False, True): AncestryRelation.LEFT_UNREACHABLE,
                (True, False): AncestryRelation.RIGHT_UNREACHABLE,
            }[exists]
        a, b = left.key.validated_head_sha, right.key.validated_head_sha
        if a == b:
            return AncestryRelation.EQUAL
        if (
            self._git.run(
                self._repo, ["merge-base", "--is-ancestor", a, b], check=False
            ).returncode
            == 0
        ):
            return AncestryRelation.ANCESTOR
        if (
            self._git.run(
                self._repo, ["merge-base", "--is-ancestor", b, a], check=False
            ).returncode
            == 0
        ):
            return AncestryRelation.DESCENDANT
        return AncestryRelation.DIVERGENT

    def verifies(self, evidence: EvidenceRow) -> bool:
        admission = evidence.admission
        identity = admission.evidence.identity
        if not self._exists(CommitReference(identity.key, admission.pinned_ref)):
            return False
        root = self._state / admission.escrow_dir
        for name, artifact in (
            ("completion.json", identity.completion_artifact),
            ("validation.json", identity.validation_artifact),
        ):
            raw = read_regular(root / name)
            if (
                len(raw) != artifact.byte_size
                or sha256(raw).hexdigest() != artifact.sha256
            ):
                return False
        return True
