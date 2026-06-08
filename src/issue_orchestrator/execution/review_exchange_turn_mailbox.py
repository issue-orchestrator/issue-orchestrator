"""Orchestrator-owned rendezvous for review-exchange turn verdicts.

Replaces the freehand, reused response-file channel (and the per-turn
nonce that tried to police it) with an orchestrator-authoritative
mailbox. The exchange worker thread opens a one-shot slot when it starts
a turn and waits on it; the agent's ``exchange-respond`` command — routed
through the in-process control API — delivers the verdict into the slot
from a different thread.

Why this is the trust boundary (and why correlation lives here, not in
the agent):

- **Turn-correlation is server-decided.** The agent submits only its
  intent (the verdict). It never carries, echoes, or transcribes the
  turn's identity. A slot is open only while the orchestrator is actively
  waiting for *that* turn, so the orchestrator — not the agent — decides
  whether a delivery belongs to the current turn.
- **Fail-safe, never fail-silent.** Every degenerate case resolves to a
  rejected delivery (and an upstream timeout → retry), never to a
  silently-accepted wrong verdict:
    - A delivery with no open slot (a bootstrap acknowledgement, a
      straggler after the turn closed) → :attr:`DeliveryStatus.NO_OPEN_SLOT`.
    - A second delivery into a slot that already holds (or already handed
      out) a verdict → :attr:`DeliveryStatus.ALREADY_DELIVERED`.
- **No filesystem in the loop**, so the torn-read / stale-file / clear-then-
  poll races of the response-file channel cannot occur.

Keying: the routing ``key`` is the per-role response-file path. It is
already distinct per role and already known to both the orchestrator and
the agent (``$ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE``), so no new
identifier has to be minted or transcribed. It is used purely as an
opaque string; nothing is written to or read from that path.

Residual (documented, not silently ignored): because the key is per-role
(stable across a role's turns), a late delivery from a *timed-out but not
yet terminated* previous turn of the same role could fill the current
turn's slot. In practice a timed-out turn's process is respawned (killed)
before the next turn opens, which closes that window; fully eliminating it
would require a per-process credential bound to the open turn (future
hardening). This is still strictly safer than the response-file channel,
which had this same window plus the bootstrap/late-write/torn-read ones.
"""

from __future__ import annotations

import enum
import logging
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


class DeliveryStatus(enum.Enum):
    """Outcome of an attempt to deliver a verdict into a turn slot."""

    ACCEPTED = "accepted"
    NO_OPEN_SLOT = "no_open_slot"
    ALREADY_DELIVERED = "already_delivered"


@dataclass(frozen=True)
class DeliveryResult:
    """Result of :meth:`TurnMailbox.deliver`.

    ``turn_id`` is the orchestrator-side identity of the slot the delivery
    was matched against (``None`` when no slot was open). It is for logging
    and diagnostics only — the agent never sees or supplies it.
    """

    status: DeliveryStatus
    turn_id: str | None = None

    @property
    def accepted(self) -> bool:
        return self.status is DeliveryStatus.ACCEPTED


@dataclass
class _OpenSlot:
    turn_id: str
    payload: dict[str, Any] | None = None
    taken: bool = False


class TurnMailbox:
    """Thread-safe, one-shot-per-turn rendezvous keyed by an opaque string.

    The exchange worker thread calls :meth:`open` then polls
    :meth:`try_take`; the control-API handler thread calls :meth:`deliver`.
    All state transitions are guarded by a single lock — the operations are
    O(1), so coarse locking is both correct and cheap.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._slots: dict[str, _OpenSlot] = {}

    def open(self, key: str, *, turn_id: str) -> None:
        """Open a fresh, empty slot for ``key``, superseding any prior slot.

        Called by the exchange worker immediately before it prompts the
        agent for a turn. Superseding is deliberate: a new turn for the same
        role discards any undelivered/untaken remnant of the previous turn,
        so a verdict can only ever be taken for the turn currently in flight.
        """
        if not key:
            raise ValueError("turn mailbox key must be non-empty")
        if not turn_id:
            raise ValueError("turn mailbox turn_id must be non-empty")
        with self._lock:
            self._slots[key] = _OpenSlot(turn_id=turn_id)

    def deliver(self, key: str, payload: Mapping[str, Any]) -> DeliveryResult:
        """Deliver a verdict into the open slot for ``key``.

        Rejects (without mutating anything) when no slot is open or when the
        open slot already holds — or has already handed out — a verdict.
        """
        with self._lock:
            slot = self._slots.get(key)
            if slot is None:
                return DeliveryResult(status=DeliveryStatus.NO_OPEN_SLOT)
            if slot.payload is not None or slot.taken:
                return DeliveryResult(
                    status=DeliveryStatus.ALREADY_DELIVERED,
                    turn_id=slot.turn_id,
                )
            slot.payload = dict(payload)
            return DeliveryResult(
                status=DeliveryStatus.ACCEPTED,
                turn_id=slot.turn_id,
            )

    def try_take(self, key: str) -> dict[str, Any] | None:
        """Return the delivered verdict for ``key`` once, else ``None``.

        Non-blocking; the exchange worker calls this each poll iteration in
        place of reading the response file. The verdict is handed out exactly
        once: a subsequent :meth:`deliver` for the same (still-open) slot is
        rejected as :attr:`DeliveryStatus.ALREADY_DELIVERED`.
        """
        with self._lock:
            slot = self._slots.get(key)
            if slot is None or slot.payload is None or slot.taken:
                return None
            slot.taken = True
            return slot.payload

    def close(self, key: str) -> None:
        """Discard the slot for ``key`` (idempotent).

        Called by the exchange worker when a turn ends — whether the verdict
        was taken, the turn timed out, or the round bailed out — so a later
        delivery for a closed turn is rejected rather than silently buffered.
        """
        with self._lock:
            self._slots.pop(key, None)
