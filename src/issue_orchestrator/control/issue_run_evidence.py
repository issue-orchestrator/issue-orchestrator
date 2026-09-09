"""Reconcile durable run ownership with the live registry, failing closed."""

from collections.abc import Callable
from pathlib import Path

from ..domain.issue_run_evidence import (
    IssueRunEvidence,
    IssueRunEvidenceOrigin,
    IssueRunEvidenceStatus,
    IssueRunEvidenceUnavailable,
    IssueRunRecord,
)
from ..ports.issue_run_evidence import IssueRunLedger
from ..domain.session_run import SessionRunAssets


class IssueRunEvidenceService:
    def __init__(
        self,
        ledger: IssueRunLedger,
        *,
        live_runs: Callable[[int], tuple[IssueRunRecord, ...]],
        now: Callable[[], str],
    ) -> None:
        self._ledger = ledger
        self._live_runs = live_runs
        self._now = now

    def worktree_evidence(self, issue_number: int, path: Path) -> IssueRunEvidence:
        return self.evidence_for_issue(issue_number).select(worktree_path=path)

    def terminal_issues(self, terminal_id: str) -> tuple[int, ...]:
        return tuple(issue for issue in self.issue_numbers()
            if self.evidence_for_issue(issue).owns_terminal(terminal_id))

    def terminal_evidence(self, issue_number: int, terminal_id: str, run: SessionRunAssets | None) -> IssueRunEvidence:
        return self.evidence_for_issue(issue_number).select(terminal_id=terminal_id, run=run)

    def issue_numbers(self) -> tuple[int, ...]:
        return self._ledger.issue_numbers()

    def issues_for_worktree(self, path: Path) -> tuple[int, ...]:
        # Exact allocated paths, never agent metadata or filename patterns.
        return tuple(issue for issue in self.issue_numbers()
            if any(row.run.worktree_path == path for row in self._ledger.recorded_runs(issue)))

    def record_run(self, issue_number: int, record: IssueRunRecord) -> None:
        self._ledger.record_run(issue_number, record)

    def evidence_for_issue(self, issue_number: int) -> IssueRunEvidence:
        try:
            recorded = self._ledger.recorded_runs(issue_number)
            live = self._live_runs(issue_number)
        except Exception as exc:
            raise IssueRunEvidenceUnavailable(
                f"Cannot establish run ownership for issue #{issue_number}"
            ) from exc
        for active in live:
            if not any(
                row.session_key == active.session_key and row.run == active.run
                and row.branch_name == active.branch_name
                and row.terminal_binding == active.terminal_binding
                for row in recorded
            ):
                raise IssueRunEvidenceUnavailable(
                    f"Live run {active.run.identity} for issue #{issue_number} "
                    "has no matching durable ownership record"
                )
        return IssueRunEvidence(
            issue_number=issue_number,
            status=(
                IssueRunEvidenceStatus.RUNS_RECORDED
                if recorded else IssueRunEvidenceStatus.NO_RUNS_RECORDED
            ),
            runs=recorded,
            origin=(IssueRunEvidenceOrigin.BOTH if live else IssueRunEvidenceOrigin.RUN_LEDGER),
            observed_at=self._now(),
        )
