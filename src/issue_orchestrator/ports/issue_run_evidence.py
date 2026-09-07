"""Run registration and evidence queries, without filesystem rediscovery."""

from typing import Protocol
from pathlib import Path

from ..domain.issue_run_evidence import IssueRunEvidence, IssueRunRecord
from ..domain.session_run import SessionRunAssets


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
    def worktree_evidence(self, issue_number: int, path: Path) -> IssueRunEvidence: ...
    def terminal_issues(self, terminal_id: str) -> tuple[int, ...]: ...
    def terminal_evidence(self, issue_number: int, terminal_id: str, run: SessionRunAssets | None) -> IssueRunEvidence: ...
    def issues_for_worktree(self, path: Path) -> tuple[int, ...]: ...
    def issue_numbers(self) -> tuple[int, ...]: ...
    def record_run(self, issue_number: int, record: IssueRunRecord) -> None: ...

    def evidence_for_issue(self, issue_number: int) -> IssueRunEvidence:
        """Read every retained run and verify agreement with live ownership."""
        ...
