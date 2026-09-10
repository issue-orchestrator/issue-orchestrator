"""Typed commands and results for stopping an exact validated-work owner."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import assert_never

from .repository_engine_lifecycle import EngineIdentity
from .validated_work import require_positive, require_text
from .validated_work_discovery import ClaimOwnerFact


@dataclass(frozen=True, slots=True)
class StopValidatedWorkOwnerCommand:
    record_id: str
    expected_engine: EngineIdentity
    expected_owner_fence: int
    actor: str
    reason: str

    def __post_init__(self) -> None:
        require_text(self.record_id, "record id")
        if type(self.expected_engine) is not EngineIdentity:
            raise ValueError("stop-owner command requires a typed engine")
        require_positive(self.expected_owner_fence, "expected owner fence")
        require_text(self.actor, "stop actor")
        require_text(self.reason, "stop reason")


class StopOwnerStatus(StrEnum):
    STOPPED = "stopped"
    NO_SUCH_RECORD = "no_such_record"
    RECORD_UNAVAILABLE = "record_unavailable"
    NOT_OWNED = "not_owned"
    OWNER_CHANGED = "owner_changed"
    STOP_IN_PROGRESS = "stop_in_progress"
    REPO_MISMATCH = "repo_mismatch"
    REMOTE_HOST = "remote_host"
    STOP_FAILED = "stop_failed"


@dataclass(frozen=True, slots=True)
class StopOwnerOutcome:
    status: StopOwnerStatus
    observed_owner: ClaimOwnerFact | None
    message: str

    def __post_init__(self) -> None:
        if type(self.status) is not StopOwnerStatus:
            raise ValueError("stop-owner status must be typed")
        require_text(self.message, "stop-owner message")
        if (
            self.observed_owner is not None
            and type(self.observed_owner) is not ClaimOwnerFact
        ):
            raise ValueError("observed owner must be a typed owner fact")
        match self.status:
            case (
                StopOwnerStatus.NO_SUCH_RECORD
                | StopOwnerStatus.RECORD_UNAVAILABLE
                | StopOwnerStatus.NOT_OWNED
            ):
                if self.observed_owner is not None:
                    raise ValueError(
                        f"{self.status.value} cannot carry an observed owner"
                    )
            case (
                StopOwnerStatus.STOPPED
                | StopOwnerStatus.STOP_IN_PROGRESS
                | StopOwnerStatus.REMOTE_HOST
                | StopOwnerStatus.STOP_FAILED
            ):
                if self.observed_owner is None:
                    raise ValueError(f"{self.status.value} requires an observed owner")
            case StopOwnerStatus.OWNER_CHANGED | StopOwnerStatus.REPO_MISMATCH:
                pass
            case _:
                assert_never(self.status)


@dataclass(frozen=True, slots=True)
class StopReservation:
    reservation_id: str
    record_id: str
    engine: EngineIdentity
    owner_fence: int

    def __post_init__(self) -> None:
        require_text(self.reservation_id, "stop reservation id")
        require_text(self.record_id, "stop reservation record id")
        if type(self.engine) is not EngineIdentity:
            raise ValueError("stop reservation requires a typed engine")
        require_positive(self.owner_fence, "stop reservation owner fence")


@dataclass(frozen=True, slots=True)
class StopReservationRefusal:
    status: StopOwnerStatus
    observed_owner: ClaimOwnerFact | None
    message: str

    def __post_init__(self) -> None:
        StopOwnerOutcome(self.status, self.observed_owner, self.message)
        if self.status not in {
            StopOwnerStatus.NO_SUCH_RECORD,
            StopOwnerStatus.NOT_OWNED,
            StopOwnerStatus.OWNER_CHANGED,
            StopOwnerStatus.STOP_IN_PROGRESS,
            StopOwnerStatus.REPO_MISMATCH,
            StopOwnerStatus.REMOTE_HOST,
        }:
            raise ValueError("reservation refusal must precede a stop attempt")
