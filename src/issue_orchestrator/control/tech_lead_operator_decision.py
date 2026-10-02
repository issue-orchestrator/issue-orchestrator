"""Carry out a decision the operator approved (#7593).

A ``propose_decision`` is a tech lead turning an open question about a blocked
item (an agent asking whether to split its issue, a maintainer call about
scope) into one concrete proposal the operator can approve or decline. It is
always a gated proposal: removing ``proposed-tech-lead`` is the operator's
answer. This owner is what that answer does, in an order that keeps the item
gated until everything the resumed session needs exists:

1. **Refuse what cannot be carried out, writing nothing.** An item that closed,
   is no longer blocked (nothing waits on the decision any more), or whose
   ``needs-human`` a cause the operator's retry may not override still holds
   (a claim quarantine, a tech-lead hand-over) makes the proposal close stale.
2. **File the drafted follow-up issues**, create-once by a body marker, each
   behind the applier's mutation-authority check on the item. They inherit the
   item's own non-workflow labels (agent, priority, area) and milestone.
3. **Post the decision on the item**, naming the follow-ups, create-once by a
   comment marker, through the applier's own comment write.
4. **Retry the item last**, through the operator's own retry command, the
   one owner of which labels a retry clears. The item stays blocked until the
   decision and its follow-ups are on GitHub, so no session resumes it
   without them.
5. **Record that the retry committed**, durably in the authority store, then
   mark the proposal applied with a comment. A replay of the op (a marker or
   finalize write that failed) finds the receipt and never retries again, so a
   ``needs-human`` raised after the retry is never cleared; it only finishes
   the marker.

A failure before the retry commits leaves the op in place; its replay finds
the earlier writes by their markers and retries.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..events import EventName
from ..infra.logging_config import issue_log
from ..ports import make_trace_event
from ..ports.operator_issue_commands import OperatorCommandOutcome, OperatorCommandStatus
from .actions import Action, ActionResult, AddCommentAction
from .claim_gate import ClaimLostError
from .reconciliation import ReconciliationRequired, build_expected_for_mutation
from .tech_lead_op_actions import ApplyOperatorDecisionAction
from .tech_lead_reset_retry import STALE_DOWNGRADE_MODE

if TYPE_CHECKING:
    from ..domain.tech_lead_artifacts import DecisionFollowUp
    from ..ports import EventSink
    from ..ports.issue import Issue
    from .label_manager import LabelManager

logger = logging.getLogger(__name__)

OP_TYPE = ApplyOperatorDecisionAction.op_type


def follow_up_marker(proposal_issue_number: int, index: int) -> str:
    return f"<!-- io:operator-decision:{proposal_issue_number}:follow-up:{index} -->"


def decision_marker(proposal_issue_number: int) -> str:
    return f"<!-- io:operator-decision:{proposal_issue_number}:decision -->"


def applied_marker(proposal_issue_number: int) -> str:
    return f"<!-- io:operator-decision:{proposal_issue_number}:applied -->"


@dataclass(frozen=True)
class OperatorDecisionExecutor:
    """Applies :class:`ApplyOperatorDecisionAction` (see the module docstring)."""

    events: "EventSink"
    labels: "LabelManager"
    read_issue: Callable[[int], "Issue | None"]
    retry_issue: Callable[[int], OperatorCommandOutcome]
    #: Causes a force-clear may not settle that still hold an issue's
    #: ``needs-human`` (the shared block's owner answers).
    unsettleable_holders: Callable[[int], tuple[str, ...]]
    find_issue_by_marker: Callable[..., int | None]
    create_issue: Callable[..., "dict[str, Any] | None"]
    comment_marker_present: Callable[[int, str], bool]
    #: The applier's own dispatch: comments are normal claim-verified,
    #: reconciliation-guarded writes.
    apply_action: Callable[[Action], ActionResult]
    #: The applier's mutation-authority check (expectations and claim) for a
    #: write about an issue, run before each follow-up is filed.
    require_authority: Callable[[Action, int], None]
    #: The durable receipt that a proposal's retry committed (authority store),
    #: so a replay of the op never retries the item a second time.
    record_decision_retry: Callable[[int], None]
    decision_retried: Callable[[int], bool]

    def apply(self, action: ApplyOperatorDecisionAction) -> ActionResult:
        proposal = action.proposal_issue_number
        if self.decision_retried(proposal):
            # A replay after the retry committed: never retry again, only
            # finish what is left (#7593 review r2).
            return self._finish(action, follow_ups=(), removed=(), replayed=True)
        target = self.read_issue(action.issue_number)
        refusal = self._refusal(action, target)
        if refusal is not None:
            return _stale(action, refusal)
        assert target is not None  # a missing issue is a refusal
        try:
            follow_ups = tuple(
                self._file_follow_up(action, target, index, follow_up)
                for index, follow_up in enumerate(action.decision.follow_ups, start=1)
            )
        except (ReconciliationRequired, ClaimLostError):
            raise
        except Exception as error:  # the item stays blocked; a replay resumes by marker
            return ActionResult.fail(
                action, f"follow-up issue not filed: {error}", issue_number=action.issue_number
            )
        posted = self._comment_once(
            action.issue_number,
            decision_marker(proposal),
            _decision_comment(action, follow_ups, decision_marker(proposal)),
            reason=f"operator approved decision {action.proposal_id} (proposal #{proposal})",
        )
        if posted is not None:
            return ActionResult.fail(action, posted, issue_number=action.issue_number)
        outcome = self.retry_issue(action.issue_number)
        unsettled = _UNSETTLED_RETRY.get(outcome.status)
        if unsettled is not None:
            return unsettled(action, outcome)
        self.record_decision_retry(proposal)
        return self._finish(action, follow_ups=follow_ups, removed=outcome.removed, replayed=False)

    def _finish(
        self,
        action: ApplyOperatorDecisionAction,
        *,
        follow_ups: tuple[int, ...],
        removed: tuple[str, ...],
        replayed: bool,
    ) -> ActionResult:
        """Mark the proposal applied once the retry committed, and report it."""
        proposal = action.proposal_issue_number
        marked = self._comment_once(
            proposal, applied_marker(proposal),
            f"Applied: #{action.issue_number} was retried with the decision posted on it."
            f"\n\n{applied_marker(proposal)}",
            reason=f"operator decision {action.proposal_id} applied",
        )
        if marked is not None:
            return ActionResult.fail(action, marked, issue_number=action.issue_number)
        self.events.publish(make_trace_event(EventName.TECH_LEAD_ACTION_EXECUTED, {
            "issue_number": action.anchor_issue_number,
            "action_id": action.proposal_id,
            "proposal_type": OP_TYPE,
            "target_number": action.issue_number,
            "finding_ids": list(action.finding_ids),
            "boundary": {"retried": list(removed), "follow_ups": list(follow_ups), "replayed": replayed},
        }))
        logger.info(issue_log(action.issue_number,
            "Operator approved decision %s (proposal #%d): retried, follow-ups %s"),
            action.proposal_id, action.proposal_issue_number, list(follow_ups))
        return ActionResult.ok(
            action, issue_number=action.issue_number, replayed=replayed,
            follow_up_issues=[str(number) for number in follow_ups],
        )

    def _refusal(self, action: ApplyOperatorDecisionAction, target: "Issue | None") -> str | None:
        """Why the approved decision cannot be carried out now, or None."""
        if target is None or target.state != "open":
            return f"issue #{action.issue_number} is no longer open"
        if not self.labels.get_blocking(list(target.labels)):
            return f"#{action.issue_number} is no longer blocked: nothing waits on this decision"
        needs_human = self.labels.needs_human.casefold()
        if any(label.casefold() == needs_human for label in target.labels):
            held = self.unsettleable_holders(action.issue_number)
            if held:
                return (
                    f"#{action.issue_number}'s {self.labels.needs_human} is held by"
                    f" {', '.join(held)}, which the operator's retry may not override"
                )
        return None

    def _comment_once(self, number: int, marker: str, body: str, *, reason: str) -> str | None:
        """Post *body* on *number* unless *marker* is already there; the failure, else None."""
        if self.comment_marker_present(number, marker):
            return None
        posted = self.apply_action(AddCommentAction(
            number=number, comment=body, reason=reason, expected=build_expected_for_mutation(),
        ))
        return None if posted.success else f"comment on #{number} not posted: {posted.error}"

    def _file_follow_up(
        self,
        action: ApplyOperatorDecisionAction,
        target: "Issue",
        index: int,
        follow_up: "DecisionFollowUp",
    ) -> int:
        marker = follow_up_marker(action.proposal_issue_number, index)
        existing = self.find_issue_by_marker(
            title=follow_up.title, marker=marker, authoritative=True
        )
        if existing is not None:
            return existing
        self.require_authority(action, action.issue_number)
        created = self.create_issue(
            title=follow_up.title,
            body=(
                f"{follow_up.body}\n\n"
                f"Refs #{action.issue_number} — split out by the decision approved on"
                f" proposal #{action.proposal_issue_number}.\n{marker}"
            ),
            labels=self._inherited_labels(target.labels),
            milestone=target.milestone_number,
        )
        if not created or not isinstance(created.get("number"), int):
            raise RuntimeError(f"follow-up {index} of proposal #{action.proposal_issue_number} was not created")
        return int(created["number"])

    def _inherited_labels(self, labels: Sequence[str]) -> list[str]:
        """The item's own labels (agent, priority, area), never workflow state."""
        return [label for label in labels if not self.labels.is_workflow_reserved(label)]

def _stale(action: ApplyOperatorDecisionAction, why: str) -> ActionResult:
    logger.warning(issue_log(action.issue_number, "Approved decision %s not applied: %s"),
                   action.proposal_id, why)
    return ActionResult.skip(
        action, f"stale precondition: {why}",
        mode=STALE_DOWNGRADE_MODE, skip_reason=why,
        issue_number=action.issue_number, proposal_id=action.proposal_id,
    )


def _still_blocked(action: ApplyOperatorDecisionAction, outcome: OperatorCommandOutcome) -> ActionResult:
    """A cause the retry may not override appeared: close the proposal stale."""
    held = ", ".join(outcome.held_by) or "another lifecycle"
    return _stale(
        action,
        f"{outcome.blocked} is still required by {held}; the decision is posted on"
        f" #{action.issue_number} but the item was not retried",
    )


def _incomplete(action: ApplyOperatorDecisionAction, outcome: OperatorCommandOutcome) -> ActionResult:
    """GitHub kept a label: a failure the op's replay retries."""
    return ActionResult.fail(
        action,
        f"retry of issue #{action.issue_number} did not settle:"
        f" GitHub kept {', '.join(outcome.failed)}",
        issue_number=action.issue_number,
    )


#: What an unsettled operator retry means for the approved decision.
_UNSETTLED_RETRY: dict[
    OperatorCommandStatus,
    Callable[[ApplyOperatorDecisionAction, OperatorCommandOutcome], ActionResult],
] = {
    OperatorCommandStatus.STILL_BLOCKED: _still_blocked,
    OperatorCommandStatus.INCOMPLETE: _incomplete,
}


def _decision_comment(
    action: ApplyOperatorDecisionAction, follow_ups: Sequence[int], marker: str
) -> str:
    filed = (
        "\n\nFiled for the rest: " + ", ".join(f"#{number}" for number in follow_ups)
        if follow_ups
        else ""
    )
    return (
        f"## Decision approved: {action.decision.title}\n\n"
        f"The operator approved this decision on proposal #{action.proposal_issue_number}."
        f" The issue is retried after this comment, so the next session works to it.\n\n"
        f"{action.decision.body}{filed}\n\n{marker}"
    )
