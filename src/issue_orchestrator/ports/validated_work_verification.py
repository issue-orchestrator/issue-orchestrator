"""Trusted constructor-injected verification; callers cannot submit proof booleans."""

from typing import Protocol

from ..domain.validated_work_claim import ProcessIdentity
from ..domain.validated_work_store import AncestryRelation, CommitReference, EvidenceRow


class ValidatedWorkAncestry(Protocol):
    def compare(
        self, left: CommitReference, right: CommitReference
    ) -> AncestryRelation:
        """Compare pinned exact commits, identifying which object is unreachable."""
        ...


class ValidatedWorkArtifactVerifier(Protocol):
    def verifies(self, evidence: EvidenceRow) -> bool:
        """Re-verify admitted artifact hashes and pins, without trusting caller facts."""
        ...


class OrchestratorLivenessPort(Protocol):
    def current(self) -> ProcessIdentity:
        """Actual calling process identity, read again on every fenced operation."""
        ...

    def is_provably_dead(self, owner: ProcessIdentity) -> bool:
        """Positive gate-backed proof only; unknown, live or remote means False."""
        ...
