"""Carry out a decision the operator approved (#7593).

A ``propose_decision`` is a tech lead turning an open question about a blocked
item (an agent asking whether to split its issue, a maintainer call about
scope) into one concrete proposal the operator can approve or decline. It is
always a gated proposal: removing ``proposed-tech-lead`` is the operator's
answer. This owner is what that answer does, and nothing runs before it:

1. **Retry the item**, through the operator's own retry command. That is the
   one owner of which labels a retry clears and of the local state it settles;
   approving a decision means "go ahead", which is what an operator retry is.
   If a cause the retry may not override still holds the item (a claim
   quarantine, a tech-lead hand-over), nothing else happens: the proposal
   closes as stale and says what still holds it.
2. **File the drafted follow-up issues**, create-once by a body marker, so a
   crash between two writes never files one twice. They inherit the item's own
   non-workflow labels (its agent, priority, area) and milestone, so a split
   remainder lands where the item was.
3. **Post the decision on the item**, create-once by a comment marker, naming
   the follow-ups, through the applier's own comment write. The coding agent
   that resumes the item reads it there.

A failed write after the retry leaves the op in place; the next attempt finds
the retry already settled and the earlier writes by their markers.
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
from .reconciliation import build_expected_for_mutation
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


@dataclass(frozen=True)
class OperatorDecisionExecutor:
    """Applies :class:`ApplyOperatorDecisionAction` (see the module docstring)."""

    events: "EventSink"
    labels: "LabelManager"
    read_issue: Callable[[int], "Issue | None"]
    retry_issue: Callable[[int], OperatorCommandOutcome]
    find_issue_by_marker: Callable[..., int | None]
    create_issue: Callable[..., "dict[str, Any] | None"]
    comment_marker_present: Callable[[int, str], bool]
    #: The applier's own dispatch: the decision comment is a normal
    #: claim-verified, reconciliation-guarded comment write.
    apply_action: Callable[[Action], ActionResult]

    def apply(self, action: ApplyOperatorDecisionAction) -> ActionResult:
        target = self.read_issue(action.issue_number)
        if target is None or target.state != "open":
            return self._stale(action, f"issue #{action.issue_number} is no longer open")
        outcome = self.retry_issue(action.issue_number)
        if outcome.status is OperatorCommandStatus.STILL_BLOCKED:
            held = ", ".join(outcome.held_by) or "another lifecycle"
            return self._stale(
                action, f"{outcome.blocked} is still required by {held}; the item was not retried"
            )
        if outcome.status is not OperatorCommandStatus.COMMITTED:
            return ActionResult.fail(
                action,
                f"retry of issue #{action.issue_number} did not settle:"
                f" GitHub kept {', '.join(outcome.failed)}",
                issue_number=action.issue_number,
            )
        follow_ups = tuple(
            self._file_follow_up(action, target, index, follow_up)
            for index, follow_up in enumerate(action.decision.follow_ups, start=1)
        )
        marker = decision_marker(action.proposal_issue_number)
        if not self.comment_marker_present(action.issue_number, marker):
            posted = self.apply_action(AddCommentAction(
                number=action.issue_number,
                comment=_decision_comment(action, follow_ups, marker),
                reason=f"operator approved decision {action.proposal_id} (proposal #{action.proposal_issue_number})",
                expected=build_expected_for_mutation(),
            ))
            if not posted.success:
                return ActionResult.fail(
                    action, f"decision comment on #{action.issue_number} not posted: {posted.error}",
                    issue_number=action.issue_number,
                )
        self.events.publish(make_trace_event(EventName.TECH_LEAD_ACTION_EXECUTED, {
            "issue_number": action.anchor_issue_number,
            "action_id": action.proposal_id,
            "proposal_type": OP_TYPE,
            "target_number": action.issue_number,
            "finding_ids": list(action.finding_ids),
            "boundary": {"retried": list(outcome.removed), "follow_ups": list(follow_ups)},
        }))
        logger.info(issue_log(action.issue_number,
            "Operator approved decision %s (proposal #%d): retried, follow-ups %s"),
            action.proposal_id, action.proposal_issue_number, list(follow_ups))
        return ActionResult.ok(
            action, issue_number=action.issue_number,
            follow_up_issues=[str(number) for number in follow_ups],
        )

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

    def _stale(self, action: ApplyOperatorDecisionAction, why: str) -> ActionResult:
        logger.warning(issue_log(action.issue_number, "Approved decision %s not applied: %s"),
                       action.proposal_id, why)
        return ActionResult.skip(
            action, f"stale precondition: {why}",
            mode=STALE_DOWNGRADE_MODE, skip_reason=why,
            issue_number=action.issue_number, proposal_id=action.proposal_id,
        )


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
        f" The issue has been retried, so the next session works to it.\n\n"
        f"{action.decision.body}{filed}\n\n{marker}"
    )
