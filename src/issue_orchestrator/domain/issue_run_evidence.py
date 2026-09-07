"""Explicit, launch-owned evidence of the runs belonging to an issue."""

from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path

from .session_key import SessionKey, TaskKind
from .session_run import SessionRunAssets


class IssueRunEvidenceOrigin(StrEnum):
    RUN_LEDGER = "run_ledger"
    BOTH = "both"


class IssueRunEvidenceStatus(StrEnum):
    RUNS_RECORDED = "runs_recorded"
    NO_RUNS_RECORDED = "no_runs_recorded"


@dataclass(frozen=True, slots=True)
class RunTerminalBinding:
    """Owner-recorded visible terminal, or explicit background-only allocation."""
    terminal_id: str | None

    def __post_init__(self) -> None:
        if self.terminal_id is not None and (type(self.terminal_id) is not str or not self.terminal_id.strip()):
            raise ValueError("terminal binding must be a non-empty identity")


@dataclass(frozen=True, slots=True)
class IssueRunRecord:
    session_key: SessionKey
    run: SessionRunAssets
    recorded_at: str
    branch_name: str | None  # None only for explicitly unbound pre-migration rows.
    terminal_binding: RunTerminalBinding | None  # None means unknown legacy ownership.
    # None denotes a legacy allocation whose role was never durably recorded.
    agent_label: str | None = field(default=None, kw_only=True)
    completion_task: TaskKind | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        if self.terminal_binding is not None and type(self.terminal_binding) is not RunTerminalBinding:
            raise TypeError("run requires typed terminal ownership")
        if not self.recorded_at.strip():
            raise ValueError("run record requires recorded_at")


@dataclass(frozen=True, slots=True)
class IssueRunEvidence:
    issue_number: int
    status: IssueRunEvidenceStatus
    runs: tuple[IssueRunRecord, ...]
    origin: IssueRunEvidenceOrigin
    observed_at: str

    def __post_init__(self) -> None:
        if type(self.issue_number) is not int or self.issue_number <= 0:
            raise ValueError("evidence requires a positive issue number")
        if type(self.status) is not IssueRunEvidenceStatus:
            raise TypeError("evidence status must be typed")
        if type(self.origin) is not IssueRunEvidenceOrigin:
            raise TypeError("evidence origin must be typed")
        if (self.status is IssueRunEvidenceStatus.RUNS_RECORDED) != bool(self.runs):
            raise ValueError("evidence status must agree with recorded runs")
        if not self.observed_at.strip():
            raise ValueError("evidence requires observed_at")


    def owns_terminal(self, terminal_id: str) -> bool:
        if any(row.terminal_binding is None for row in self.runs):
            raise IssueRunEvidenceUnavailable("legacy run has no recorded terminal binding")
        return any(row.terminal_binding is not None and row.terminal_binding.terminal_id == terminal_id for row in self.runs)

    def select(self, *, terminal_id: str | None = None, run: SessionRunAssets | None = None, worktree_path: Path | None = None) -> "IssueRunEvidence":
        if terminal_id is not None and run is None:
            self.owns_terminal(terminal_id)
        selected = tuple(row for row in self.runs if (terminal_id is None or run is not None or
            (row.terminal_binding is not None and row.terminal_binding.terminal_id == terminal_id))
            and (run is None or row.run == run)
            and (worktree_path is None or row.run.worktree_path == worktree_path))
        if run is not None and not selected:
            raise IssueRunEvidenceUnavailable("terminal run has no exact durable owner")
        return replace(self, runs=selected, status=IssueRunEvidenceStatus.RUNS_RECORDED
            if selected else IssueRunEvidenceStatus.NO_RUNS_RECORDED)


class IssueRunEvidenceUnavailable(RuntimeError):
    """Unknown ownership must never be interpreted as absence of work."""
