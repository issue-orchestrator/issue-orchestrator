"""Required admission-only lifecycle capability; grants no publication authority."""

from typing import Protocol
from ..domain.validated_work_commands import AutomaticCaptureCommand, ValidatedWorkDispositionBatch
from ..domain.validated_work import ValidatedWorkKey
from ..domain.validated_work_remote_authority import PublishedOnOpenPullRequest
from ..domain.validated_work_store import (
    AdmissionOutcome, EvidenceAdmission, EvidenceAdmissionSelection, EvidenceLookup, EvidenceRow,
    OpenPrPublicationStatus,
)


class ValidatedWorkAdmissionStore(Protocol):
    def retained_evidence(self, issue_number: int) -> tuple[EvidenceRow, ...]: ...
    def admit(self, admission: EvidenceAdmission) -> AdmissionOutcome: ...
    def for_issue(self, issue_number: int) -> ValidatedWorkDispositionBatch: ...
    def has_unresolved_work(self, issue_number: int) -> bool: ...
    def evidence_for_id(self, evidence_id: str) -> EvidenceLookup | None: ...

    def record_open_pr_publication(
        self, key: ValidatedWorkKey, *, published: PublishedOnOpenPullRequest, observed_at: str,
    ) -> OpenPrPublicationStatus:
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
                       selection: EvidenceAdmissionSelection) -> AdmissionOutcome | None:
        """Atomically admit the selection, or report a changed current evidence."""
        ...
