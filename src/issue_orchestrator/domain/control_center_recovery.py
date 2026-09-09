"""Internal Control Center projection for retained validated work."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from pathlib import Path
from typing import Literal

from .repository_engine_lifecycle import (
    ENGINE_STOP_GRACEFUL_TIMEOUT_SECONDS,
    EngineIdentity,
    EngineStopAvailability,
)
from .validated_work import (
    UNRESOLVED_STATES,
    ValidatedWorkFailure,
    ValidatedWorkState,
    require_text,
)
from .validated_work_commands import ValidatedWorkAuthoritySnapshot
from .validated_work_discovery import ClaimOwnerFact

CONFIGURED_REPOSITORY_KEY_PATTERN = r"^repo-[0-9a-f]{64}$"
DEFAULT_RECOVERY_ENGINE_INSTANCE_KEY = "default"


def recovery_engine_instance_key(instance_id: str | None) -> str:
    """Encode one engine instance for an unambiguous Control Center route."""
    if instance_id is None:
        return DEFAULT_RECOVERY_ENGINE_INSTANCE_KEY
    require_text(instance_id, "engine instance id")
    if instance_id == DEFAULT_RECOVERY_ENGINE_INSTANCE_KEY:
        raise ValueError("default is reserved for the single-instance engine")
    if Path(instance_id).name != instance_id or instance_id in {".", ".."}:
        raise ValueError("engine instance id must be one safe path component")
    return instance_id


class RecoveryRowsStatus(StrEnum):
    AVAILABLE = "available"
    EMPTY = "empty"
    DATABASE_ABSENT = "database_absent"
    UNREADABLE = "unreadable"
    UNSUPPORTED_SCHEMA = "unsupported_schema"


class RecoveryEnginePresentation(StrEnum):
    OBSERVED = "observed"
    MISSING = "missing"
    REPLACED = "replaced"
    UNKNOWN = "unknown"


@dataclass(frozen=True, slots=True)
class ConfiguredRepository:
    """Repository scope resolved by the Control Center registry."""

    repo_key: str
    repo_root: str
    repo_slug: str

    def __post_init__(self) -> None:
        require_text(self.repo_key, "configured repository key")
        require_text(self.repo_root, "configured repository root")
        require_text(self.repo_slug, "configured repository slug")
        if not Path(self.repo_root).is_absolute():
            raise ValueError("configured repository root must be absolute")


@dataclass(frozen=True, slots=True)
class RecoveryRecordFact:
    authority: ValidatedWorkAuthoritySnapshot
    state: ValidatedWorkState
    failure: ValidatedWorkFailure | None
    reason: str
    escrow_retained: bool

    def __post_init__(self) -> None:
        if type(self.authority) is not ValidatedWorkAuthoritySnapshot:
            raise ValueError("recovery fact requires typed authority")
        if type(self.state) is not ValidatedWorkState:
            raise ValueError("recovery fact requires a typed state")
        if self.failure is not None and type(self.failure) is not ValidatedWorkFailure:
            raise ValueError("failure must be an enumerated reason")
        if self.state is ValidatedWorkState.FAILED and self.failure is None:
            raise ValueError("FAILED requires an enumerated failure")
        if type(self.reason) is not str:
            raise ValueError("recovery fact reason must be a string")
        if type(self.escrow_retained) is not bool:
            raise ValueError("escrow_retained must be a boolean")


@dataclass(frozen=True, slots=True)
class GuardedRecoveryStopAction:
    record_id: str
    expected_engine: EngineIdentity
    expected_owner_fence: int
    graceful_timeout_seconds: float = ENGINE_STOP_GRACEFUL_TIMEOUT_SECONDS
    force_on_timeout: bool = True

    def __post_init__(self) -> None:
        require_text(self.record_id, "stop action record id")
        if type(self.expected_engine) is not EngineIdentity:
            raise ValueError("stop action requires a typed engine")
        if type(self.expected_owner_fence) is not int or self.expected_owner_fence < 0:
            raise ValueError("stop action requires a non-negative integer fence")
        if (
            type(self.graceful_timeout_seconds) is not float
            or self.graceful_timeout_seconds <= 0
            or not isfinite(self.graceful_timeout_seconds)
        ):
            raise ValueError("stop action requires a positive finite number of seconds")
        if type(self.force_on_timeout) is not bool:
            raise ValueError("stop action force policy must be a boolean")


@dataclass(frozen=True, slots=True)
class OwnedRecoveryRecord:
    kind: Literal["owned"]
    work: RecoveryRecordFact
    owner: ClaimOwnerFact
    stop_action: GuardedRecoveryStopAction | None

    def __post_init__(self) -> None:
        if self.kind != "owned":
            raise ValueError("owned record requires the owned discriminator")
        if (
            type(self.work) is not RecoveryRecordFact
            or type(self.owner) is not ClaimOwnerFact
        ):
            raise ValueError("owned record requires typed work and owner facts")
        available = self.owner.stop_availability is EngineStopAvailability.AVAILABLE
        if available != (self.stop_action is not None):
            raise ValueError("stop action must match owner-produced availability")
        if self.stop_action is not None:
            if type(self.stop_action) is not GuardedRecoveryStopAction:
                raise ValueError("stop action must be typed")
            if (
                self.stop_action.record_id != self.work.authority.record_id
                or self.stop_action.expected_engine != self.owner.engine
                or self.stop_action.expected_owner_fence != self.owner.owner_fence
            ):
                raise ValueError(
                    "stop action must bind this exact record, owner and fence"
                )


@dataclass(frozen=True, slots=True)
class UnownedRecoveryRecord:
    kind: Literal["unowned"]
    work: RecoveryRecordFact

    def __post_init__(self) -> None:
        if self.kind != "unowned" or type(self.work) is not RecoveryRecordFact:
            raise ValueError("unowned record requires typed work and its discriminator")
        if self.work.state not in UNRESOLVED_STATES:
            raise ValueError("resolved unowned work is outside discovery")


@dataclass(frozen=True, slots=True)
class RecoveryEngineGroup:
    engine: EngineIdentity
    presentation: RecoveryEnginePresentation
    presentation_message: str
    records: tuple[OwnedRecoveryRecord, ...]

    def __post_init__(self) -> None:
        if (
            type(self.engine) is not EngineIdentity
            or type(self.presentation) is not RecoveryEnginePresentation
        ):
            raise ValueError("engine group requires typed identity and presentation")
        require_text(self.presentation_message, "engine presentation message")
        if type(self.records) is not tuple or not self.records:
            raise ValueError("an engine group requires owned records")
        if any(
            type(row) is not OwnedRecoveryRecord or row.owner.engine != self.engine
            for row in self.records
        ):
            raise ValueError("every grouped row must name this exact engine")
        record_ids = tuple(row.work.authority.record_id for row in self.records)
        if len(record_ids) != len(set(record_ids)):
            raise ValueError("engine group cannot repeat a record")


@dataclass(frozen=True, slots=True)
class EnginePresentationFact:
    presentation: RecoveryEnginePresentation
    message: str

    def __post_init__(self) -> None:
        if type(self.presentation) is not RecoveryEnginePresentation:
            raise ValueError("engine presentation fact requires a typed presentation")
        require_text(self.message, "engine presentation fact message")


@dataclass(frozen=True, slots=True)
class ControlCenterRecoveryRows:
    repo_key: str
    status: RecoveryRowsStatus
    engine_groups: tuple[RecoveryEngineGroup, ...]
    unowned_records: tuple[UnownedRecoveryRecord, ...]
    message: str

    def __post_init__(self) -> None:
        require_text(self.repo_key, "configured repository key")
        if type(self.status) is not RecoveryRowsStatus:
            raise ValueError("recovery rows require a typed status")
        require_text(self.message, "recovery rows display message")
        if type(self.engine_groups) is not tuple or any(
            type(group) is not RecoveryEngineGroup for group in self.engine_groups
        ):
            raise ValueError("engine_groups must be typed groups in a tuple")
        if type(self.unowned_records) is not tuple or any(
            type(row) is not UnownedRecoveryRecord for row in self.unowned_records
        ):
            raise ValueError("unowned_records must be typed rows in a tuple")
        has_rows = bool(self.engine_groups or self.unowned_records)
        if (self.status is RecoveryRowsStatus.AVAILABLE) != has_rows:
            raise ValueError(
                "only AVAILABLE carries rows and it must carry at least one"
            )
        engines = tuple(group.engine for group in self.engine_groups)
        if len(engines) != len(set(engines)):
            raise ValueError("a full engine identity has exactly one group")
        record_ids = [
            row.work.authority.record_id
            for group in self.engine_groups
            for row in group.records
        ]
        record_ids.extend(row.work.authority.record_id for row in self.unowned_records)
        if len(record_ids) != len(set(record_ids)):
            raise ValueError(
                "a record occurs exactly once across all groups and unowned rows"
            )
