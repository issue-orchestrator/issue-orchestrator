"""How a parked action becomes visible to a person, and stops being (#7350).

Three surfaces, from cheapest to loudest:

1. a timeline event, ``action.parked`` / ``action.released``, always, so the
   issue's history says what stopped and why;
2. the tech-lead board's "Held - waiting on you" section, read from the
   owner's durable rows (no write here);
3. when the key names an escalation issue: the shared needs-human block under
   its own :attr:`NeedsHumanCause.ACTION_LIVENESS`, plus one comment saying
   which action stopped, on what facts, and how to release it.

The block goes through the action applier like every other orchestrator write
(orchestrator-authoritative, and the shared-block owner records the cause), but
straight to the applier rather than through a plan, so an escalation is never
itself subject to the liveness gate it reports on.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..domain.action_liveness import LivenessRow
from ..domain.human_block import NeedsHumanCause
from ..events import EventName
from ..ports.event_sink import EventSink, make_trace_event
from .action_results import SupportsApplyAction
from .actions import AddCommentAction, AddLabelAction, RemoveLabelAction

logger = logging.getLogger(__name__)


def _payload(row: LivenessRow) -> dict[str, object]:
    key = row.key
    payload: dict[str, object] = {
        "subject": key.identity.subject,
        "action": key.identity.action,
        "fingerprint": key.fingerprint,
        "outcome": row.last_outcome.value,
        "reason": row.last_reason,
        "attempts": row.attempts,
        "first_failed_at": row.first_failed_at.isoformat(),
    }
    if key.escalation_issue is not None:
        payload["issue_number"] = key.escalation_issue
    return payload


def parked_comment(row: LivenessRow) -> str:
    """The one comment a park posts. Orchestrator-authored, deterministic."""
    key = row.key
    return "\n".join(
        [
            "**The orchestrator stopped retrying an action here.**",
            "",
            f"- Action: `{key.identity.action}` on `{key.identity.subject}`",
            f"- Outcome: `{row.last_outcome.value}` after {row.attempts} attempt(s)"
            f" since {row.first_failed_at.isoformat()}",
            f"- Reason: {row.last_reason}",
            f"- Facts fingerprint: `{key.fingerprint}`",
            "",
            "It is parked, not dropped: nothing it was protecting has been"
            " deleted. It is tried again when the facts it was derived from"
            " change, or when an operator retries or dismisses this issue.",
        ]
    )


@dataclass(frozen=True, slots=True)
class ActionLivenessEscalation:
    """The :class:`~..ports.action_liveness.LivenessEscalation` adapter."""

    events: EventSink
    applier: SupportsApplyAction
    needs_human_label: str

    def escalate(self, row: LivenessRow) -> bool:
        self.events.publish(make_trace_event(EventName.ACTION_PARKED, _payload(row)))
        issue = row.key.escalation_issue
        if issue is None:
            return False
        block = AddLabelAction(
            issue_number=issue,
            label=self.needs_human_label,
            needs_human_cause=NeedsHumanCause.ACTION_LIVENESS,
            reason=f"parked {row.key.identity.action}: {row.last_reason}",
        )
        if not self._apply(block):
            return False
        # The block is what a person acts on; the comment only explains it, so
        # a failed comment does not make the escalation uncommitted.
        self._apply(AddCommentAction(number=issue, comment=parked_comment(row)))
        return True

    def resolve(self, rows: tuple[LivenessRow, ...], *, release_issue: bool) -> None:
        for row in rows:
            self.events.publish(
                make_trace_event(EventName.ACTION_RELEASED, _payload(row))
            )
        if not release_issue:
            return
        issues = sorted(
            {
                row.key.escalation_issue
                for row in rows
                if row.escalated and row.key.escalation_issue is not None
            }
        )
        for issue in issues:
            self._apply(
                RemoveLabelAction(
                    issue_number=issue,
                    label=self.needs_human_label,
                    needs_human_cause=NeedsHumanCause.ACTION_LIVENESS,
                    reason="parked action made progress",
                )
            )

    def _apply(self, action: AddLabelAction | AddCommentAction | RemoveLabelAction) -> bool:
        # A failed escalation write must not abort the tick that parked the
        # action: the park itself is already durable and on the timeline.
        try:
            result = self.applier.apply(action)
        except Exception:
            logger.exception(
                "[LIVENESS] Escalation write %s failed", action.action_type.value
            )
            return False
        if not result.success:
            logger.warning(
                "[LIVENESS] Escalation write %s did not commit: %s",
                action.action_type.value,
                result.error,
            )
        return result.success


__all__ = ["ActionLivenessEscalation", "parked_comment"]
