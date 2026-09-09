"""Private capability values for process-bound disposition ownership."""

from __future__ import annotations

import hashlib
import secrets
from dataclasses import dataclass, field
from typing import Never

from .validated_work import ValidatedWorkState, require_positive, require_text


@dataclass(frozen=True, slots=True)
class ProcessIdentity:
    """Process identity; ``started_at`` may carry an OS incarnation token."""

    host: str
    pid: int
    started_at: str
    instance_id: str | None

    def __post_init__(self) -> None:
        require_text(self.host, "host")
        require_positive(self.pid, "pid")
        require_text(self.started_at, "started_at")
        if self.instance_id is not None:
            require_text(self.instance_id, "instance_id")


class ClaimSecret:
    """Random 256-bit capability. No plaintext accessor, repr, copying or pickle."""

    __slots__ = ("__value",)

    def __init__(self) -> None:
        self.__value = secrets.token_bytes(32)

    def digest(self) -> str:
        return hashlib.sha256(self.__value).hexdigest()

    def __repr__(self) -> str:
        return "ClaimSecret(<private>)"

    def __reduce__(self) -> Never:
        raise TypeError("claim secrets cannot be serialized or copied")


@dataclass(frozen=True, slots=True)
class ValidatedWorkClaim:
    record_id: str
    fence: int
    secret: ClaimSecret = field(repr=False)
    owner: ProcessIdentity

    def __post_init__(self) -> None:
        require_text(self.record_id, "record_id")
        require_positive(self.fence, "fence")
        if (
            type(self.secret) is not ClaimSecret
            or type(self.owner) is not ProcessIdentity
        ):
            raise ValueError("claim requires private secret and typed process identity")

    def __reduce__(self) -> Never:
        raise TypeError("claims cannot be serialized or copied")


@dataclass(frozen=True, slots=True)
class RetainedClaim:
    record_id: str
    evidence_id: str
    state: ValidatedWorkState
    owner: ProcessIdentity
