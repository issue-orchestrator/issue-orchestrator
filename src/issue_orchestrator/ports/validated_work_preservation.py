"""Required admission-only lifecycle capability; grants no publication authority."""

from typing import Protocol
from ..domain.validated_work_commands import AutomaticCaptureCommand, ValidatedWorkDispositionBatch
from ..domain.validated_work import ValidatedWorkKey
from ..domain.validated_work_remote_authority import CarriedByIssuePullRequest, PullRequestPublication
from ..domain.validated_work_store import (
    AdmissionOutcome, EvidenceAdmission, EvidenceAdmissionSelection, EvidenceLookup, EvidenceRow,
    PrPublicationStatus,
)


class ValidatedWorkAdmissionStore(Protocol):
    def retained_evidence(self, issue_number: int) -> tuple[EvidenceRow, ...]: ...
    def admit(
        self, admission: EvidenceAdmission, *, carried: CarriedByIssuePullRequest | None = None,
    ) -> AdmissionOutcome:
        """Admit ``admission``. ``carried`` is the open-PR proof the capture's
        own remote observation just made (#8137): only it may resolve the
        record against an open PR on another branch."""
        ...

    def for_issue(self, issue_number: int) -> ValidatedWorkDispositionBatch: ...
    def has_unresolved_work(self, issue_number: int) -> bool: ...
    def evidence_for_id(self, evidence_id: str) -> EvidenceLookup | None: ...

    def record_pr_publication(
        self, key: ValidatedWorkKey, *, published: PullRequestPublication, observed_at: str,
    ) -> PrPublicationStatus:
        """Record that the issue's open PR publishes a head containing ``key``'s head.

        The store verifies the containment itself, then advances the
        lineage's published head (``OBSERVED_OPEN_PR``) and reclassifies the
        lineage, so the records that head contains resolve as published.
        """
        ...


class ValidatedWorkPreservation(Protocol):
    def dispose_at_termination(self, command: AutomaticCaptureCommand) -> ValidatedWorkDispositionBatch: ...
    def has_unresolved_work(self, issue_number: int) -> bool: ...
    def for_issue(self, issue_number: int) -> ValidatedWorkDispositionBatch: ...


class ValidatedWorkAdmissionBackend(ValidatedWorkAdmissionStore, Protocol):
    def admit_selected(self, admission: EvidenceAdmission, expected_current: str | None,
                       selection: EvidenceAdmissionSelection,
                       *, carried: CarriedByIssuePullRequest | None = None) -> AdmissionOutcome | None:
        """Atomically admit the selection, or report a changed current evidence."""
        ...
