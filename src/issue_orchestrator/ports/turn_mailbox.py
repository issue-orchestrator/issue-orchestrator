"""Port: orchestrator-owned rendezvous for review-exchange turn verdicts.

The control layer and entrypoints depend on this protocol; the concrete
in-process implementation lives in ``execution`` and is injected at the
composition root. See ``execution.review_exchange_turn_mailbox`` for the
behavioural contract and the rationale (turn-correlation decided
server-side, fail-safe over fail-silent).
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol


class DeliveryStatus(enum.Enum):
    """Outcome of an attempt to deliver a verdict into a turn slot."""

    ACCEPTED = "accepted"
    NO_OPEN_SLOT = "no_open_slot"
    ALREADY_DELIVERED = "already_delivered"


@dataclass(frozen=True)
class DeliveryResult:
    """Result of :meth:`TurnMailbox.deliver`.

    ``turn_id`` is the orchestrator-side identity of the slot the delivery
    matched (``None`` when no slot was open). For logging/diagnostics only —
    the agent never sees or supplies it.
    """

    status: DeliveryStatus
    turn_id: str | None = None

    @property
    def accepted(self) -> bool:
        return self.status is DeliveryStatus.ACCEPTED


class TurnMailbox(Protocol):
    """One-shot, key-scoped rendezvous between the exchange worker thread and
    the agent-facing ``exchange-respond`` Control API handler thread.
    """

    def open(self, key: str, *, turn_id: str) -> None:
        """Open a fresh, empty slot for ``key``, superseding any prior slot."""
        ...

    def deliver(self, key: str, payload: Mapping[str, Any]) -> DeliveryResult:
        """Deliver a verdict into the open slot for ``key``."""
        ...

    def try_take(self, key: str) -> dict[str, Any] | None:
        """Return the delivered verdict for ``key`` once, else ``None``."""
        ...

    def close(self, key: str) -> None:
        """Discard the slot for ``key`` (idempotent)."""
        ...
