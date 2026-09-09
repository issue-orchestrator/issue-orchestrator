"""Exact object operations: no moving checkout or tracking-ref authority."""

from dataclasses import dataclass
from enum import StrEnum


class ExactPushAuthenticationError(RuntimeError):
    """Authentication could not supply the effective execution context."""


class ExactPushOutcome(StrEnum):
    PUSHED = "pushed"
    LEASE_REJECTED = "lease_rejected"
    NOT_FAST_FORWARD = "not_fast_forward"
    AUTH_FAILED = "auth_failed"
    TRANSIENT = "transient"


@dataclass(frozen=True, slots=True)
class ExactPushResult:
    outcome: ExactPushOutcome
    detail: str = ""


class RefPinOutcome(StrEnum):
    PINNED = "pinned"
    ALREADY_PINNED = "already_pinned"
    CONFLICT = "conflict"
    OBJECT_MISSING = "object_missing"


@dataclass(frozen=True, slots=True)
class RetainedRef:
    name: str
    sha: str


@dataclass(frozen=True, slots=True)
class ExactPushDestination:
    """Resolved transport endpoint; reused unchanged for one exact publication."""

    endpoint: str

    def __post_init__(self) -> None:
        if (
            type(self.endpoint) is not str
            or not self.endpoint
            or any(c in self.endpoint for c in "\n\r\0")
        ):
            raise ValueError("exact push destination must be nonempty single-line text")
