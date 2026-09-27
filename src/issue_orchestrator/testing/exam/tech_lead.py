"""What the engine did with each action a tech-lead run proposed.

The decision file says what the tech lead WANTED; the engine's trace events
say what happened to it. Three event families answer that, each scoped to the
run by its anchor issue (``payload.issue_number``):

* ``tech_lead.decision_rejected`` — the whole decision failed validation;
* ``tech_lead.action_executed`` — an effect reached GitHub. Act-level
  executors name the decision's action id (``action_id``); the decision
  receipt for comments/escalations/filings names the action TYPE
  (``action``) and, for comments, ``tech_lead_action_id``;
* ``tech_lead.action_proposed`` — surfaced for approval (propose authority,
  a stale downgrade, or a pattern record), by ``action_id``.

Matching is by action id wherever the event carries one, and by type only
for receipts that carry none. GitHub state, not this, is the ground truth
for destruction.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .observation import TechLeadActionDisposition, TechLeadReceipt

REJECTED = "tech_lead.decision_rejected"
EXECUTED = "tech_lead.action_executed"
PROPOSED = "tech_lead.action_proposed"


@dataclass(frozen=True)
class ProposedAction:
    action_id: str
    action_type: str


def _payload(event: Mapping[str, Any]) -> Mapping[str, Any]:
    payload = event.get("payload")
    return payload if isinstance(payload, Mapping) else {}


def _event_action_id(payload: Mapping[str, Any]) -> str | None:
    for key in ("action_id", "tech_lead_action_id"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _event_action_type(payload: Mapping[str, Any]) -> str | None:
    for key in ("proposal_type", "action"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _matches(
    payload: Mapping[str, Any], action: ProposedAction, same_type: int
) -> bool:
    """Whether an event is about ``action``.

    By action id when the event carries one. A receipt with only a TYPE
    identifies the action only when the decision has exactly one action of
    that type; with several (``same_type`` > 1) it could be any of them, so
    it matches none — fail closed rather than credit the wrong one.
    """
    event_id = _event_action_id(payload)
    if event_id is not None:
        return event_id == action.action_id
    return same_type == 1 and _event_action_type(payload) == action.action_type


def resolve_dispositions(
    events: Iterable[Mapping[str, Any]],
    *,
    anchor_issue_number: int,
    anchor_shared: bool,
    run_failed: bool,
    actions: Iterable[ProposedAction],
) -> tuple[TechLeadActionDisposition, ...]:
    """One disposition per action, in the decision's order.

    Events carry the anchor and the decision's action id, but no run id and
    no time: when several runs share an anchor (the stuck sweep can
    re-investigate an issue), an event cannot be attributed to one of them
    and every action resolves UNKNOWN — fail closed rather than hand one
    run's receipt, or rejection, to another. Destruction does not depend on
    this: it is judged from the raw receipts.
    """
    wanted = list(actions)
    if anchor_shared:
        return tuple(TechLeadActionDisposition.UNKNOWN for _ in wanted)
    scoped = [
        (str(event.get("type", "")), _payload(event))
        for event in events
        if _payload(event).get("issue_number") == anchor_issue_number
    ]
    rejected = run_failed or any(name == REJECTED for name, _ in scoped)
    type_counts = Counter(action.action_type for action in wanted)
    resolved: list[TechLeadActionDisposition] = []
    for action in wanted:
        same_type = type_counts[action.action_type]
        # An observed execution wins over the run's fate: a failed run can
        # still have executed an action (e.g. a kill) before it failed.
        if any(name == EXECUTED and _matches(p, action, same_type) for name, p in scoped):
            resolved.append(TechLeadActionDisposition.EXECUTED)
        elif rejected:
            resolved.append(TechLeadActionDisposition.REJECTED)
        elif any(name == PROPOSED and _matches(p, action, same_type) for name, p in scoped):
            resolved.append(TechLeadActionDisposition.PROPOSED)
        else:
            resolved.append(TechLeadActionDisposition.UNKNOWN)
    return tuple(resolved)


def executed_receipts(events: Iterable[Mapping[str, Any]]) -> tuple[TechLeadReceipt, ...]:
    """Every ``tech_lead.action_executed`` event, unattributed."""
    receipts: list[TechLeadReceipt] = []
    for event in events:
        if event.get("type") != EXECUTED:
            continue
        payload = _payload(event)
        anchor = payload.get("issue_number")
        action_type = _event_action_type(payload)
        if not isinstance(anchor, int) or isinstance(anchor, bool) or action_type is None:
            raise ValueError(f"tech-lead execution receipt without anchor/action: {event!r}")
        target = payload.get("target_number")
        receipts.append(
            TechLeadReceipt(
                action_type=action_type,
                target_number=target if isinstance(target, int) and not isinstance(target, bool) else None,
                anchor_issue_number=anchor,
            )
        )
    return tuple(receipts)
