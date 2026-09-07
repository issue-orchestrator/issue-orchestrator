"""Explicit, launch-owned evidence of the runs belonging to an issue."""

from dataclasses import dataclass, replace
from enum import StrEnum

from .session_key import SessionKey
from .session_run import SessionRunAssets


class IssueRunEvidenceOrigin(StrEnum):
    RUN_LEDGER = "run_ledger"
    BOTH = "both"


class IssueRunEvidenceStatus(StrEnum):
    RUNS_RECORDED = "runs_recorded"
    NO_RUNS_RECORDED = "no_runs_recorded"


@dataclass(frozen=True, slots=True)
class IssueRunRecord:
    session_key: SessionKey
    run: SessionRunAssets
    recorded_at: str
    branch_name: str | None  # None only for explicitly unbound pre-migration rows.

    def __post_init__(self) -> None:
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


    def select(self, *, terminal_id: str, run: SessionRunAssets | None = None) -> "IssueRunEvidence":
        selected = tuple(row for row in self.runs if row.run.session_name == terminal_id
            and (run is None or row.run == run))
        if run is not None and not selected:
            raise IssueRunEvidenceUnavailable("terminal run has no exact durable owner")
        return replace(self, runs=selected, status=IssueRunEvidenceStatus.RUNS_RECORDED
            if selected else IssueRunEvidenceStatus.NO_RUNS_RECORDED)


class IssueRunEvidenceUnavailable(RuntimeError):
    """Unknown ownership must never be interpreted as absence of work."""
