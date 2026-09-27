"""Explicit, launch-owned evidence of the runs belonging to an issue."""

from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path

from .session_key import SessionKey
from .session_run import SessionRunAssets


class IssueRunEvidenceOrigin(StrEnum):
    RUN_LEDGER = "run_ledger"
    BOTH = "both"


class IssueRunEvidenceStatus(StrEnum):
    RUNS_RECORDED = "runs_recorded"
    NO_RUNS_RECORDED = "no_runs_recorded"


@dataclass(frozen=True, slots=True)
class ReworkTarget:
    """The PR a rework run is fixing, and the review cycle it answers.

    Recorded by the launch that allocated the run (#7347 review r4), so a
    restart restores a rework - and a rework's validation retry - onto its PR
    from the orchestrator's own ledger, never from an agent-writable manifest.
    """

    pr_number: int
    cycle: int

    def __post_init__(self) -> None:
        for name in ("pr_number", "cycle"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0:
                raise ValueError(f"rework target {name} must be a positive int, got {value!r}")

    @classmethod
    def of(cls, pr_number: int | None, cycle: int | None) -> "ReworkTarget | None":
        """The target when both halves are known; ``None`` (unknown) otherwise."""
        if pr_number is None or cycle is None:
            return None
        return cls(pr_number, cycle)


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
    # The run's KIND is ``session_key.kind``: the launch-stamped authority
    # (#7347), decoded for pre-#7347 rows by ``SessionKind.from_ledger_stamps``.
    agent_label: str | None = field(default=None, kw_only=True)
    # The PR a REWORK run fixes; None for every other kind, and for a rework
    # recorded before #7347 (unknown, never guessed).
    rework_target: ReworkTarget | None = field(default=None, kw_only=True)

    def __post_init__(self) -> None:
        if self.terminal_binding is not None and type(self.terminal_binding) is not RunTerminalBinding:
            raise TypeError("run requires typed terminal ownership")
        if self.rework_target is not None and not self.session_key.kind.capabilities.pushes_to_an_open_pr:
            raise ValueError(
                f"a {self.session_key.kind.value} run cannot record a rework target"
            )
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
