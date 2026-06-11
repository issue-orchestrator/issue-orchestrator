"""Typed verdict payload for the review-exchange ``exchange-respond`` seam.

The agent's verdict crosses two process boundaries — the ``exchange-respond``
CLI and the ``/api/review-exchange/respond`` Control API endpoint — before it
reaches the orchestrator's authoritative parser
(:meth:`ReviewExchangeTurnResult.from_agent_dict`). This value object is the
typed contract for those hops, replacing raw ``dict`` plumbing at the seam.

It is deliberately a *transport* contract, not a second validator: it carries
the verdict fields faithfully and lets the orchestrator's parser remain the
single source of truth for verdict semantics (so an unknown ``response_type``
still becomes a named ``PROTOCOL_ERROR`` artifact, exactly as on the legacy
file channel — the endpoint does not pre-empt that with its own rejection).
The only structural requirement is that the payload is an object.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ExchangeVerdict:
    """One review-exchange turn verdict in transit to the orchestrator.

    Fields mirror the agent-response contract consumed downstream by
    ``ReviewExchangeTurnResult.from_agent_dict`` /
    ``ReviewDecision.from_agent_payload``. ``decision`` carries the reviewer's
    structured decision object verbatim (its schema is owned by
    ``ReviewDecision``); the coder omits it.
    """

    response_type: str | None
    response_text: str | None
    getting_closer: bool | None
    decision: Mapping[str, Any] | None

    @classmethod
    def from_wire(cls, raw: Mapping[str, Any]) -> "ExchangeVerdict":
        """Build from a decoded JSON object. Raises if ``raw`` is not a mapping.

        Field types are normalised (wrong-typed fields become ``None``) so the
        serialised form is exactly what the orchestrator parser expects to see;
        semantic validation (which ``response_type`` values are legal, whether
        ``response_text`` is present) stays downstream.
        """
        if not isinstance(raw, Mapping):
            raise ValueError("exchange verdict payload must be a JSON object")
        response_type = raw.get("response_type")
        response_text = raw.get("response_text")
        getting_closer = raw.get("getting_closer")
        decision = raw.get("decision")
        return cls(
            response_type=response_type if isinstance(response_type, str) else None,
            response_text=response_text if isinstance(response_text, str) else None,
            getting_closer=getting_closer if isinstance(getting_closer, bool) else None,
            decision=decision if isinstance(decision, Mapping) else None,
        )

    def to_wire(self) -> Mapping[str, Any]:
        """Render to the JSON-object shape the orchestrator parser consumes.

        Unset fields are omitted (matching how agents wrote the response file),
        so a missing ``response_type`` round-trips to ``missing_response_type``
        rather than a sentinel.
        """
        wire: dict[str, Any] = {}
        if self.response_type is not None:
            wire["response_type"] = self.response_type
        if self.response_text is not None:
            wire["response_text"] = self.response_text
        if self.getting_closer is not None:
            wire["getting_closer"] = self.getting_closer
        if self.decision is not None:
            wire["decision"] = dict(self.decision)
        return wire
