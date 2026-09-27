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

from dataclasses import dataclass
from typing import Any, Iterable, Mapping

from .observation import TechLeadActionDisposition

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


def _matches(payload: Mapping[str, Any], action: ProposedAction) -> bool:
    event_id = _event_action_id(payload)
    if event_id is not None:
        return event_id == action.action_id
    return _event_action_type(payload) == action.action_type


def resolve_dispositions(
    events: Iterable[Mapping[str, Any]],
    *,
    anchor_issue_number: int,
    run_failed: bool,
    actions: Iterable[ProposedAction],
) -> tuple[TechLeadActionDisposition, ...]:
    """One disposition per action, in the decision's order."""
    scoped = [
        (str(event.get("type", "")), _payload(event))
        for event in events
        if _payload(event).get("issue_number") == anchor_issue_number
    ]
    wanted = list(actions)
    if run_failed or any(name == REJECTED for name, _ in scoped):
        return tuple(TechLeadActionDisposition.REJECTED for _ in wanted)
    resolved: list[TechLeadActionDisposition] = []
    for action in wanted:
        if any(name == EXECUTED and _matches(p, action) for name, p in scoped):
            resolved.append(TechLeadActionDisposition.EXECUTED)
        elif any(name == PROPOSED and _matches(p, action) for name, p in scoped):
            resolved.append(TechLeadActionDisposition.PROPOSED)
        else:
            resolved.append(TechLeadActionDisposition.UNKNOWN)
    return tuple(resolved)
