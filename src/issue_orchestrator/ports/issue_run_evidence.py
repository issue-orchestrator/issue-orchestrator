"""Run registration and evidence queries, without filesystem rediscovery."""

from typing import Protocol

from ..domain.issue_run_evidence import IssueRunEvidence, IssueRunRecord


from .completion_intake import CompletionIntakeLedger


class IssueRunLedger(CompletionIntakeLedger, Protocol):
    def issue_numbers(self) -> tuple[int, ...]: ...
    def record_run(self, issue_number: int, record: IssueRunRecord) -> None:
        """Persist exact allocated assets before the launch may spawn."""
        ...

    def recorded_runs(self, issue_number: int) -> tuple[IssueRunRecord, ...]:
        """All retained runs; unreadable ownership raises, never returns empty."""
        ...


class IssueRunEvidenceSource(Protocol):
    def issue_numbers(self) -> tuple[int, ...]: ...
    def record_run(self, issue_number: int, record: IssueRunRecord) -> None: ...

    def evidence_for_issue(self, issue_number: int) -> IssueRunEvidence:
        """Read every retained run and verify agreement with live ownership."""
        ...
