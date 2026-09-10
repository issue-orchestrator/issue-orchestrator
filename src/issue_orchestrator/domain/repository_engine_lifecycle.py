"""Identity and availability facts for exact Repository Engine lifecycle actions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from math import isfinite
from pathlib import Path

from .validated_work import require_text
from .validated_work_claim import ProcessIdentity


@dataclass(frozen=True, slots=True)
class EngineIdentity:
    """Stable identity for an exact Repository Engine process incarnation."""

    repo_root: str
    instance_id: str | None
    host: str
    label: str
    process: ProcessIdentity

    def __post_init__(self) -> None:
        require_text(self.repo_root, "repository root")
        if not Path(self.repo_root).is_absolute():
            raise ValueError("engine repository root must be absolute")
        require_text(self.host, "engine host")
        require_text(self.label, "engine label")
        if type(self.process) is not ProcessIdentity:
            raise ValueError("engine identity requires a process identity")
        if self.instance_id is not None:
            require_text(self.instance_id, "engine instance id")
            if Path(self.instance_id).name != self.instance_id or self.instance_id in {
                ".",
                "..",
            }:
                raise ValueError("engine instance id must be one safe path component")
        if (self.host, self.instance_id) != (
            self.process.host,
            self.process.instance_id,
        ):
            raise ValueError("engine identity and process identity disagree")


class EngineStopAvailability(StrEnum):
    AVAILABLE = "available"
    REMOTE_HOST = "remote_host"
    EXACT_TARGET_UNAVAILABLE = "exact_target_unavailable"


ENGINE_STOP_GRACEFUL_TIMEOUT_SECONDS = 120.0


@dataclass(frozen=True, slots=True)
class StopEngineCommand:
    """Stop one exact engine incarnation under an explicit stop policy."""

    engine: EngineIdentity
    actor: str
    reason: str
    graceful_timeout_seconds: float = ENGINE_STOP_GRACEFUL_TIMEOUT_SECONDS
    force_on_timeout: bool = True

    def __post_init__(self) -> None:
        if type(self.engine) is not EngineIdentity:
            raise ValueError("stop command requires a typed engine identity")
        require_text(self.actor, "stop actor")
        require_text(self.reason, "stop reason")
        if (
            type(self.graceful_timeout_seconds) not in {int, float}
            or not isfinite(self.graceful_timeout_seconds)
            or self.graceful_timeout_seconds <= 0
        ):
            raise ValueError("graceful stop timeout must be finite and positive")
        if type(self.force_on_timeout) is not bool:
            raise ValueError("force-on-timeout policy must be a boolean")


class StopEngineStatus(StrEnum):
    STOPPED = "stopped"
    TARGET_CHANGED = "target_changed"
    REMOTE_HOST = "remote_host"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class StopEngineOutcome:
    """Typed result for the requested incarnation, never a replacement."""

    status: StopEngineStatus
    engine: EngineIdentity
    message: str

    def __post_init__(self) -> None:
        if type(self.status) is not StopEngineStatus:
            raise ValueError("stop outcome requires a typed status")
        if type(self.engine) is not EngineIdentity:
            raise ValueError("stop outcome requires a typed engine identity")
        require_text(self.message, "stop outcome message")
