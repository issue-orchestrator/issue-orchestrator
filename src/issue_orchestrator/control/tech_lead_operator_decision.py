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
   Then **carry out the decision's steps beyond the item** (#8691: another
   issue's milestone or body, a superseded proposal, the item's PR reference
   line) through ``tech_lead_decision_steps``, each once, in order. Every
   step's precondition is checked in step 1, so a decision whose step no
   longer applies writes nothing.
4. **Retry the item last**, through the operator's own retry command, the
   one owner of which labels a retry clears. The item stays blocked until the
   decision and its follow-ups are on GitHub, so no session resumes it
   without them.
   A PR rework step follows the retry: the engine refuses a blocked issue's
   rework.
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
from dataclasses import dataclass, replace
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
from .tech_lead_decision_steps import DecisionStepContext
from .tech_lead_op_actions import ApplyOperatorDecisionAction
from .tech_lead_reset_retry import STALE_DOWNGRADE_MODE

if TYPE_CHECKING:
    from ..domain.tech_lead_artifacts import DecisionFollowUp
    from ..ports import EventSink
    from ..ports.operator_decision_retries import DecisionRetryLedger
    from ..ports.issue import Issue
    from .label_manager import LabelManager
    from ..domain.standing_ruling import StandingRuling
    from .standing_rulings import StandingRulingsOwner
    from .tech_lead_decision_steps import DecisionStepsOwner

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
    #: The owner of the decision's steps beyond the item (#8691).
    steps: "DecisionStepsOwner"

    def apply(self, action: ApplyOperatorDecisionAction) -> ActionResult:
        prior = self.retries.decision_retry_state(proposal_issue_number=action.proposal_issue_number)
        target = self.read_issue(action.issue_number)
        step = decision_replay_step(prior, item_open_and_unblocked=self._open_and_unblocked(target))
        handlers = {
            DecisionReplayStep.CARRY_OUT: self._carry_out,
            DecisionReplayStep.FINISH: self._finish_replay,
            DecisionReplayStep.HAND_BACK: self._hand_back,
        }
        result = handlers[step](action, target)
        return self._never_stale_once_begun(action, result)

    def _never_stale_once_begun(self, action: ApplyOperatorDecisionAction, result: ActionResult) -> ActionResult:
        """Every exit, one rule (#8691): a stale refusal of a decision that has
        begun writing (its follow-ups, ruling or steps) is handed back partial,
        so its proposal stays open and never says "No changes were made"."""
        if result.details.get("mode") != STALE_DOWNGRADE_MODE:
            return result
        reason = str(result.details.get("skip_reason", "its preconditions no longer hold"))
        classified = self.steps.classify(self._steps(action), action.follow_through, reason)
        if not classified.partial:
            return result
        return ActionResult.fail(action, classified.hand_back(), issue_number=action.issue_number)

    def _carry_out(self, action: ApplyOperatorDecisionAction, target: "Issue | None") -> ActionResult:
        proposal = action.proposal_issue_number
        refused = self._refused(action, target)
        if refused is not None:
            return refused
        assert target is not None  # a missing issue is a refusal
        self.steps.check_authority(self._steps(action), action.follow_through)
        begun = self.steps.begin(self._steps(action), action.follow_through)
        if begun is not None:
            return ActionResult.fail(action, begun, issue_number=action.issue_number)
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
        before = self.steps.apply(_step_context(action), action.follow_through, after_release=False)
        if before.failed is not None:  # the item stays blocked; a replay resumes at the step
            return ActionResult.fail(action, f"decision step not applied: {before.failed}",
                                     issue_number=action.issue_number)
        self.retries.begin_decision_retry(proposal_issue_number=proposal)
        outcome = self.retry_issue(action.issue_number)
        unsettled = _UNSETTLED_RETRY.get(outcome.status)
        if unsettled is not None:
            self.retries.abandon_decision_retry(proposal_issue_number=proposal)
            return unsettled(action, outcome)
        self.retries.commit_decision_retry(proposal_issue_number=proposal)
        return self._finish(action, follow_ups=follow_ups, removed=outcome.removed, replayed=False)

    def _refused(self, action: ApplyOperatorDecisionAction, target: "Issue | None") -> ActionResult | None:
        """The item's own refusal, else its steps'. Once the decision has begun
        writing, any refusal is handed back partial, never closed as stale."""
        item = self._refusal(action, target)
        if item is not None:
            return _stale(action, item)  # apply() hands it back if the decision has begun
        refusal = self.steps.refusal(self._steps(action), action.follow_through)
        if refusal is None:
            return None
        if refusal.partial:
            return ActionResult.fail(action, refusal.hand_back(), issue_number=action.issue_number)
        return _stale(action, refusal.reason)

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
        """Run the steps that follow the release, mark the proposal applied
        once the retry committed, and report it."""
        proposal = action.proposal_issue_number
        after = self.steps.apply(_step_context(action), action.follow_through, after_release=True)
        if after.failed is not None:  # the retry committed; a replay finishes the steps only
            return ActionResult.fail(action, f"decision step not applied: {after.failed}",
                                     issue_number=action.issue_number)
        steps_refused = self.steps.refused_steps(_step_context(action), action.follow_through)
        refused = "".join(f"\n- Refused at write time: {item}" for item in steps_refused)
        marked = self._comment_once(
            proposal, applied_marker(proposal),
            f"Applied: #{action.issue_number} was retried with the decision posted on it."
            f"{refused}\n\n{applied_marker(proposal)}",
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
            "boundary": {"retried": list(removed), "follow_ups": list(follow_ups), "replayed": replayed,
                         "steps": len(action.follow_through.steps), "steps_refused": list(steps_refused)},
        }))
        logger.info(issue_log(action.issue_number,
            "Operator approved decision %s (proposal #%d): retried, follow-ups %s"),
            action.proposal_id, action.proposal_issue_number, list(follow_ups))
        return ActionResult.ok(
            action, issue_number=action.issue_number, replayed=replayed,
            follow_up_issues=[str(number) for number in follow_ups],
            steps_refused=list(steps_refused),
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
        try:
            self.rulings.record(action.issue_number, self._decision_ruling(action))
        except Exception as error:  # the item stays blocked; a replay records it
            return f"standing ruling not recorded on #{action.issue_number}: {error}"
        return None

    def _decision_ruling(self, action: ApplyOperatorDecisionAction) -> "StandingRuling":
        decision = action.decision
        return self.rulings.ruling(
            ruling_id=decision_ruling_id(action.proposal_issue_number),
            text=f"## {decision.title}\n\n{decision.body}",
            authority=RulingAuthority.APPROVED_DECISION,
            source=f"tech-lead decision approved on proposal #{action.proposal_issue_number}",
            scope=RulingScope(),
        )

    def _steps(self, action: ApplyOperatorDecisionAction) -> DecisionStepContext:
        """The decision as its steps see it, with the ruling it records on the item,
        so the steps' rulings are preflighted together with it (#8691)."""
        return replace(_step_context(action), subject_rulings=(self._decision_ruling(action),))

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

def _step_context(action: ApplyOperatorDecisionAction) -> DecisionStepContext:
    return DecisionStepContext(
        action=action, subject=action.issue_number,
        proposal_issue_number=action.proposal_issue_number,
        anchor_issue_number=action.anchor_issue_number,
        proposal_id=action.proposal_id, finding_ids=action.finding_ids,
    )


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
