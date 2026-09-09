"""Identity and availability facts for exact Repository Engine lifecycle actions."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

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
        require_text(self.host, "engine host")
        require_text(self.label, "engine label")
        if type(self.process) is not ProcessIdentity:
            raise ValueError("engine identity requires a process identity")
        if self.instance_id is not None:
            require_text(self.instance_id, "engine instance id")
        if (self.host, self.instance_id) != (
            self.process.host,
            self.process.instance_id,
        ):
            raise ValueError("engine identity and process identity disagree")


class EngineStopAvailability(StrEnum):
    AVAILABLE = "available"
    REMOTE_HOST = "remote_host"
    EXACT_TARGET_UNAVAILABLE = "exact_target_unavailable"
