"""Exact object operations: no moving checkout or tracking-ref authority."""

from dataclasses import dataclass
from enum import StrEnum


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
