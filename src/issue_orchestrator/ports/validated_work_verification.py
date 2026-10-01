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

    def retain(self, reference: CommitReference) -> bool:
        """Pin ``reference.key.validated_head_sha`` at ``reference.pinned_ref``.

        For a landed merged-PR head (§2.8): GC must never collect the object a
        landing proves against, once the fetched tracking ref is pruned. True
        when the ref now names exactly that commit; never moves an existing pin.
        """
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
