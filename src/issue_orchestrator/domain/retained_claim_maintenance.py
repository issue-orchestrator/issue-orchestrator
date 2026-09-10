"""Typed observations from retained publication-claim maintenance."""

from dataclasses import dataclass
from enum import StrEnum

from .validated_work import ValidatedWorkState, require_text


class RetainedClaimMaintenanceStatus(StrEnum):
    RELEASED = "released"
    BUSY = "busy"
    CHANGED = "changed"
    OWNER_UNAVAILABLE = "owner_unavailable"
    RELEASE_DEFERRED = "release_deferred"
    ERROR = "error"


@dataclass(frozen=True, slots=True)
class RetainedClaimMaintenanceItem:
    record_id: str
    evidence_id: str
    state: ValidatedWorkState
    status: RetainedClaimMaintenanceStatus
    error: str = ""

    def __post_init__(self) -> None:
        require_text(self.record_id, "record_id")
        require_text(self.evidence_id, "evidence_id")
        if type(self.state) is not ValidatedWorkState:
            raise ValueError("maintenance item requires typed state")
        if type(self.status) is not RetainedClaimMaintenanceStatus:
            raise ValueError("maintenance item requires typed status")
        if self.status is RetainedClaimMaintenanceStatus.ERROR:
            require_text(self.error, "maintenance error")
        elif self.error:
            raise ValueError("only an error result may carry error text")


@dataclass(frozen=True, slots=True)
class RetainedClaimMaintenanceReport:
    items: tuple[RetainedClaimMaintenanceItem, ...]
    scan_error: str = ""

    def __post_init__(self) -> None:
        if type(self.items) is not tuple:
            raise ValueError("maintenance items must be immutable")
        if any(type(item) is not RetainedClaimMaintenanceItem for item in self.items):
            raise ValueError("maintenance report requires typed items")
        if self.scan_error:
            require_text(self.scan_error, "maintenance scan error")
