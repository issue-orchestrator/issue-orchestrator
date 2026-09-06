"""Explicit, launch-owned evidence of the runs belonging to an issue."""

from dataclasses import dataclass
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
        if not isinstance(self.status, IssueRunEvidenceStatus):
            raise TypeError("evidence status must be typed")
        if not isinstance(self.origin, IssueRunEvidenceOrigin):
            raise TypeError("evidence origin must be typed")
        if (self.status is IssueRunEvidenceStatus.RUNS_RECORDED) != bool(self.runs):
            raise ValueError("evidence status must agree with recorded runs")
        if not self.observed_at.strip():
            raise ValueError("evidence requires observed_at")


class IssueRunEvidenceUnavailable(RuntimeError):
    """Unknown ownership must never be interpreted as absence of work."""
