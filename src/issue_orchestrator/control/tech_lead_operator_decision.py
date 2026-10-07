"""Carry out a decision the operator approved (#7593).

A ``propose_decision`` is a tech lead turning an open question about a blocked
item (an agent asking whether to split its issue, a maintainer call about
scope) into one concrete proposal the operator can approve or decline. It is
always a gated proposal: a maintainer's verified approval is the operator's
answer (#7763). This owner is what that answer does, in an order that keeps the item
gated until everything the resumed session needs exists:

1. **Refuse what cannot be carried out, writing nothing.** An item that closed,
   is no longer blocked (nothing waits on the decision any more), or whose
   ``needs-human`` a cause the operator's retry may not override still holds
   (a claim quarantine, a tech-lead hand-over) makes the proposal close stale.
2. **File the drafted follow-up issues**, create-once by a body marker, each
   behind the applier's mutation-authority check on the item. They inherit the
   item's own non-workflow labels (agent, priority, area) and milestone.
3. **Post the decision on the item**, naming the follow-ups, create-once by a
   comment marker, through the applier's own comment write, and **record it as
   a standing ruling** on the item (#8141): the approved decision binds every
   later session on the issue, so it goes into the issue body's rulings block
   (create-once by the proposal), where every prompt and review reads it.
4. **Retry the item last**, through the operator's own retry command, the
   one owner of which labels a retry clears. The item stays blocked until the
   decision and its follow-ups are on GitHub, so no session resumes it
   without them.
5. **Bracket the retry durably** (``ports/operator_decision_retries``): begun
   before it, committed after, then mark the proposal applied with a comment.
   A replay of the op (a marker or finalize write that failed) finds the retry
   committed and never retries again, so a ``needs-human`` raised after the
   retry is never cleared; it only finishes the marker. A replay that finds it
   begun but unsettled (the engine stopped mid-retry) finishes only if the item
   is open and unblocked; otherwise it keeps the proposal for the operator and
   retries nothing.

A failure before the retry begins leaves the op in place; its replay finds the
earlier writes by their markers and continues.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..events import EventName
from ..infra.logging_config import issue_log
from ..ports import make_trace_event
from ..domain.operator_decision_retry import DecisionReplayStep, decision_replay_step
from ..domain.standing_ruling import RulingAuthority, RulingScope, decision_ruling_id
from ..ports.operator_issue_commands import OperatorCommandOutcome, OperatorCommandStatus
from .actions import Action, ActionResult, AddCommentAction
from .claim_gate import ClaimLostError
from .reconciliation import ReconciliationRequired, build_expected_for_mutation
from .tech_lead_op_actions import ApplyOperatorDecisionAction
from .tech_lead_reset_retry import STALE_DOWNGRADE_MODE

if TYPE_CHECKING:
    from ..domain.tech_lead_artifacts import DecisionFollowUp
    from ..ports import EventSink
    from ..ports.operator_decision_retries import DecisionRetryLedger
    from ..ports.issue import Issue
    from .label_manager import LabelManager
    from .standing_rulings import StandingRulingsOwner

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
    #: The write-ahead record of each proposal's retry (the authority store),
    #: so a replay of the op never retries the item a second time.
    retries: "DecisionRetryLedger"
    #: The owner the approved decision is recorded through as a standing ruling (#8141).
    rulings: "StandingRulingsOwner"

    def apply(self, action: ApplyOperatorDecisionAction) -> ActionResult:
        prior = self.retries.decision_retry_state(proposal_issue_number=action.proposal_issue_number)
        target = self.read_issue(action.issue_number)
        step = decision_replay_step(prior, item_open_and_unblocked=self._open_and_unblocked(target))
        handlers = {
            DecisionReplayStep.CARRY_OUT: self._carry_out,
            DecisionReplayStep.FINISH: self._finish_replay,
            DecisionReplayStep.HAND_BACK: self._hand_back,
        }
        return handlers[step](action, target)

    def _carry_out(self, action: ApplyOperatorDecisionAction, target: "Issue | None") -> ActionResult:
        proposal = action.proposal_issue_number
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
        unrecorded = self._record_ruling(action)
        if unrecorded is not None:
            return ActionResult.fail(action, unrecorded, issue_number=action.issue_number)
        self.retries.begin_decision_retry(proposal_issue_number=proposal)
        outcome = self.retry_issue(action.issue_number)
        unsettled = _UNSETTLED_RETRY.get(outcome.status)
        if unsettled is not None:
            self.retries.abandon_decision_retry(proposal_issue_number=proposal)
            return unsettled(action, outcome)
        self.retries.commit_decision_retry(proposal_issue_number=proposal)
        return self._finish(action, follow_ups=follow_ups, removed=outcome.removed, replayed=False)

    def _finish_replay(self, action: ApplyOperatorDecisionAction, target: "Issue | None") -> ActionResult:
        """The retry committed before (or was interrupted, and its item is
        unblocked): never retry again, only finish what is left (#7593 r2/r3)."""
        self.retries.commit_decision_retry(proposal_issue_number=action.proposal_issue_number)
        return self._finish(action, follow_ups=(), removed=(), replayed=True)

    def _hand_back(self, action: ApplyOperatorDecisionAction, target: "Issue | None") -> ActionResult:
        """The engine stopped mid-retry and the item waits again (#7593 review r3)."""
        number, proposal = action.issue_number, action.proposal_issue_number
        return ActionResult.fail(
            action,
            f"the engine stopped while retrying #{number} for proposal #{proposal}, and #{number}"
            " is blocked or closed now: it is not retried a second time. Retry it from the"
            " dashboard if it should resume, or close the proposal.",
            issue_number=number,
        )

    def _open_and_unblocked(self, target: "Issue | None") -> bool:
        return (
            target is not None
            and target.state == "open"
            and not self.labels.get_blocking(list(target.labels))
        )

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

    def _record_ruling(self, action: ApplyOperatorDecisionAction) -> str | None:
        """The approved decision as a standing ruling on the item (create-once); the failure, else None."""
        decision = action.decision
        try:
            self.rulings.record(action.issue_number, self.rulings.ruling(
                ruling_id=decision_ruling_id(action.proposal_issue_number),
                text=f"## {decision.title}\n\n{decision.body}",
                authority=RulingAuthority.APPROVED_DECISION,
                source=f"tech-lead decision approved on proposal #{action.proposal_issue_number}",
                scope=RulingScope(),
            ))
        except Exception as error:  # the item stays blocked; a replay records it
            return f"standing ruling not recorded on #{action.issue_number}: {error}"
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
