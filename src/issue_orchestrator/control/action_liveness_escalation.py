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

The same adapter lands the reconciliation pause observed drift calls for: one
owner (the liveness owner) for every GitHub write the orchestrator owes. Every
write reports a typed :class:`~..domain.owed_write.EffectResult`, keeping the
host's rate limit so the owner waits for its reset instead of spending.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ..domain.action_liveness import LivenessRow
from ..domain.human_block import NeedsHumanCause
from ..domain.owed_write import EffectResult
from ..events import EventName
from ..ports.event_sink import EventSink, make_trace_event
from ..ports.repository_host import host_rate_limit_of
from .action_results import SupportsApplyAction
from .actions import AddCommentAction, AddLabelAction, RemoveLabelAction
from .reconciliation import get_pause_label

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
            " change, or when an operator retries or dismisses this issue."
            " Once the cause is fixed (for example `io:needs-reconcile`"
            " removed after reconciling), **Retry** this issue to release it.",
        ]
    )


@dataclass(frozen=True, slots=True)
class ActionLivenessEscalation:
    """The :class:`~..ports.action_liveness.LivenessEscalation` adapter."""

    events: EventSink
    applier: SupportsApplyAction
    needs_human_label: str

    def announce_parked(self, row: LivenessRow) -> None:
        self.events.publish(make_trace_event(EventName.ACTION_PARKED, _payload(row)))

    def announce_released(self, rows: tuple[LivenessRow, ...]) -> None:
        for row in rows:
            self.events.publish(make_trace_event(EventName.ACTION_RELEASED, _payload(row)))

    def block(self, row: LivenessRow) -> EffectResult:
        issue = row.key.escalation_issue
        if issue is None:
            raise ValueError("a block needs the row's escalation issue")
        block = AddLabelAction(
            issue_number=issue,
            label=self.needs_human_label,
            needs_human_cause=NeedsHumanCause.ACTION_LIVENESS,
            reason=f"parked {row.key.identity.action}: {row.last_reason}",
        )
        return self._apply(block)

    def explain(self, row: LivenessRow) -> EffectResult:
        issue = row.key.escalation_issue
        if issue is None:
            raise ValueError("an explanation needs the row's escalation issue")
        return self._apply(AddCommentAction(number=issue, comment=parked_comment(row)))

    def unblock(self, issue_number: int) -> EffectResult:
        return self._apply(
            RemoveLabelAction(
                issue_number=issue_number,
                label=self.needs_human_label,
                needs_human_cause=NeedsHumanCause.ACTION_LIVENESS,
                reason="parked action made progress",
            )
        )

    def pause(self, issue_number: int, reason: str) -> EffectResult:
        pause_label = get_pause_label()
        result = self._apply(
            AddLabelAction(
                issue_number=issue_number,
                label=pause_label,
                reason="reconciliation drift detected",
            )
        )
        if result.committed:
            logger.warning(
                "[RECONCILIATION] Paused issue #%d with label '%s': %s",
                issue_number, pause_label, reason,
            )
            self.events.publish(make_trace_event(
                EventName.ISSUE_PAUSED_RECONCILE,
                {"issue_number": issue_number, "pause_label": pause_label, "reason": reason},
            ))
        return result

    def _apply(
        self, action: AddLabelAction | AddCommentAction | RemoveLabelAction
    ) -> EffectResult:
        # A failed write must not abort the tick that owes it: the debt itself
        # is already durable, and the owner retries it.
        try:
            result = self.applier.apply(action)
        except Exception as error:
            logger.exception("[LIVENESS] Owed write %s failed", action.action_type.value)
            return EffectResult.refused(
                f"{type(error).__name__}: {error}", host_rate_limit_of(error)
            )
        if result.success:
            return EffectResult.landed()
        logger.warning(
            "[LIVENESS] Owed write %s did not commit: %s",
            action.action_type.value,
            result.error,
        )
        return EffectResult.refused(result.error or "unknown error", result.host_rate_limit)


__all__ = ["ActionLivenessEscalation", "parked_comment"]
