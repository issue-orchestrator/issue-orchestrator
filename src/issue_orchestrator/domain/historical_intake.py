"""Operator-selected historical intake: publication approval is deliberately absent."""

import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Literal

from .validated_work import require_positive, require_sha, require_text


@dataclass(frozen=True, slots=True)
class HistoricalIntakeCommand:
    repo_slug: str
    issue_number: int
    branch_name: str
    target_head_sha: str
    candidate_path: Path
    candidate_sha256: str
    actor: str
    reason: str

    def __post_init__(self) -> None:
        for name in ("repo_slug", "branch_name", "actor", "reason"):
            require_text(getattr(self, name), name)
        require_positive(self.issue_number, "issue_number")
        require_sha(self.target_head_sha)
        require_sha(self.candidate_sha256, size=64)
        if (
            not self.candidate_path.is_absolute()
            or Path(os.path.normpath(self.candidate_path)) != self.candidate_path
        ):
            raise ValueError("candidate path must be absolute and normalized")


class HistoricalIntakeRefusal(StrEnum):
    WRONG_REPOSITORY = "wrong_repository"
    CANDIDATE_CHANGED = "candidate_changed"
    INVALID_COMPLETION = "invalid_completion"
    INVALID_SELECTION = "invalid_selection"
    PREREQUISITE_UNAVAILABLE = "prerequisite_unavailable"


@dataclass(frozen=True, slots=True)
class HistoricalIntakeParked:
    record_id: str
    evidence_id: str
    status: Literal["parked"] = field(default="parked", init=False)


@dataclass(frozen=True, slots=True)
class HistoricalIntakeRefused:
    reason: HistoricalIntakeRefusal
    status: Literal["refused"] = field(default="refused", init=False)


@dataclass(frozen=True, slots=True)
class HistoricalIntakeValidationFailed:
    entry_id: str
    validation_sha256: str
    validation_path: str
    status: Literal["validation_failed"] = field(
        default="validation_failed", init=False
    )


HistoricalIntakeOutcome = (
    HistoricalIntakeParked | HistoricalIntakeRefused | HistoricalIntakeValidationFailed
)
