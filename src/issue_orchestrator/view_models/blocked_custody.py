"""Project blocked-item custody (#7331) onto the dashboard payload.

Presentation only. The custody owner (:mod:`..control.blocked_item_custody`)
decides each item's state, reason, clock and staleness; this module turns the
decided value into the typed payload the board renders — words, a tone class,
an age label — and computes nothing about custody itself. Every string is
rendered verbatim, so status never depends on colour.

Two payloads:

* ``custody`` on every blocked card and row
  (``BlockedItemCustodyPayload`` in ``docs/api/ui-openapi.json``);
* ``custody_summary`` on the Blocked column
  (``BlockedCustodySummaryPayload``): the "is it under control?" number.
"""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING, Any, Mapping, MutableMapping, Sequence

from ..contracts.ui_openapi_models import (
    BlockedCustodySummaryPayload,
    BlockedItemCustodyPayload,
    CustodyCharterPayload,
    CustodyStateCountPayload,
)
from ..domain.blocked_item_custody import CustodyState, age_of
from .dashboard_freshness import format_age_seconds

if TYPE_CHECKING:
    from ..domain.blocked_item_custody import (
        BlockedCustodyBoard,
        BlockedItemCustody,
        CustodyCharterBasis,
    )

#: Styling bucket per state. A CLASS name, never a colour: the stylesheet owns
#: the palette for both themes, and the state word is always rendered too.
_TONES: Mapping[CustodyState, str] = {
    CustodyState.UNOWNED: "attention",
    CustodyState.QUEUED_FOR_TECH_LEAD: "pending",
    CustodyState.INVESTIGATING: "active",
    CustodyState.WAITING_ON_YOU: "you",
    CustodyState.BEING_FIXED: "active",
    CustodyState.WAITING_ON_WORLD: "world",
    CustodyState.HELD: "held",
    CustodyState.VERIFY: "verify",
}

#: What each recorded charter outcome means, in words.
_OUTCOME_LABELS: Mapping[str, str] = {
    "executed": "Executed",
    "proposed": "Proposed, awaiting approval",
    "advice_only": "Advice only",
    "refused_destructive": "Refused as destructive; needs approval",
}

_LIFECYCLE_LABELS: Mapping[str, str] = {
    "awaiting_approval": "awaiting approval",
    "approved_applied": "approved and applied",
    "approved_stale": "approved, but no longer applicable",
    "declined": "declined",
}


def custody_payload(custody: "BlockedItemCustody", now: datetime) -> BlockedItemCustodyPayload:
    """One item's custody, as its card and row render it (the generated contract)."""
    clock = custody.clock
    return BlockedItemCustodyPayload.model_validate(
        {
            "state": custody.state.value,
            "label": custody.state.label,
            "owner": custody.state.owner,
            "reason": custody.reason,
            "tone": "attention" if custody.needs_attention else _TONES[custody.state],
            "since": clock.since.isoformat() if clock else "",
            "since_basis": clock.basis if clock else "",
            "age_label": _age_label(custody, now),
            "stale": custody.stale,
            "stale_after_label": (
                format_age_seconds(custody.stale_after.total_seconds())
                if custody.stale_after is not None
                else ""
            ),
            "needs_attention": custody.needs_attention,
            "attention_text": _attention_text(custody),
            "charter": _charter_payload(custody.charter) if custody.charter else None,
        }
    )


def custody_signal(custody: "BlockedItemCustody") -> str:
    """The part of a custody payload that changes what a card SAYS.

    Feeds the compact-card fingerprint. The age label is left out on purpose:
    it ticks every minute, and a changed clock alone must not rebuild a card.
    """
    charter = custody.charter.decision_id if custody.charter else ""
    clock = custody.clock
    # The clock's ENTRY point is part of what the card says (a new
    # investigation on the same issue restarts it); only its age ticks.
    entered = (
        f"{clock.since.isoformat()}~{clock.basis}~{int(clock.lower_bound)}" if clock else ""
    )
    return "|".join(
        (custody.state.value, "stale" if custody.stale else "", custody.reason, charter, entered)
    )


def attach_blocked_custody(
    items: Sequence[MutableMapping[str, Any]],
    board: "BlockedCustodyBoard",
    now: datetime,
) -> None:
    """Stamp every blocked item with its custody; the only place that does."""
    for item in items:
        custody = board.for_issue(int(item["issue_number"]))
        item["custody"] = custody_payload(custody, now).model_dump(mode="json")
        item["custody_signal"] = custody_signal(custody)


def custody_summary_payload(board: "BlockedCustodyBoard") -> BlockedCustodySummaryPayload:
    """The Blocked column's "is it under control?" line."""
    total = len(board.items)
    attention = board.needs_attention_count
    return BlockedCustodySummaryPayload(
        total=total,
        needs_attention=attention,
        unowned=board.unowned_count,
        stale=board.stale_count,
        headline=_headline(total, attention, board.unowned_count, board.stale_count),
        by_state=[
            CustodyStateCountPayload.model_validate(
                {"state": state.value, "label": state.label, "count": board.count(state)}
            )
            for state in CustodyState
            if board.count(state)
        ],
    )


def _headline(total: int, attention: int, unowned: int, stale: int) -> str:
    if total == 0:
        return "Nothing is blocked."
    if attention == 0:
        if total == 1:
            return "The 1 blocked item is owned and within its time limit."
        return f"All {total} blocked items are owned and within their time limits."
    parts = [part for part in (
        f"{unowned} unowned" if unowned else "",
        f"{stale} stale" if stale else "",
    ) if part]
    return f"{attention} of {total} blocked items need attention ({', '.join(parts)})."


def _age_label(custody: "BlockedItemCustody", now: datetime) -> str:
    clock = custody.clock
    if clock is None:
        return "age unknown"
    age = format_age_seconds(age_of(clock, now).total_seconds())
    return f"at least {age}" if clock.lower_bound else age


def _attention_text(custody: "BlockedItemCustody") -> str:
    if custody.state is CustodyState.UNOWNED:
        return "Needs attention: nobody owns it."
    if custody.stale and custody.stale_after is not None:
        limit = format_age_seconds(custody.stale_after.total_seconds())
        return f"Needs attention: {custody.state.label.lower()} longer than {limit}."
    return ""


def _charter_payload(basis: "CustodyCharterBasis") -> CustodyCharterPayload:
    lifecycle = basis.lifecycle
    return CustodyCharterPayload.model_validate(
        {
            "decision_id": basis.decision_id,
            "role": basis.role,
            "action": basis.action_kind.replace("_", " "),
            "required_depth": basis.required_depth,
            "role_enabled": basis.role_enabled,
            "role_depth": basis.role_depth,
            "role_authority": basis.role_authority,
            "action_ceiling": basis.action_ceiling,
            "ceiling_source": basis.ceiling_source,
            "outcome": basis.outcome,
            "outcome_label": _OUTCOME_LABELS[basis.outcome],
            "lifecycle_label": _LIFECYCLE_LABELS[lifecycle] if lifecycle else "",
            "reason": basis.reason,
            "decided_at": basis.decided_at,
            "proposal_issue_number": basis.proposal_issue_number or 0,
        }
    )


__all__ = [
    "attach_blocked_custody",
    "custody_payload",
    "custody_signal",
    "custody_summary_payload",
]
