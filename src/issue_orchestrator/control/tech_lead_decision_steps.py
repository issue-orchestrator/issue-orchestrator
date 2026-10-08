"""Bind and execute an approved decision's steps beyond its item (#8691).

A decision's :class:`~..domain.decision_steps.DecisionFollowThrough` names what
approving it does beyond the decided ISSUE (porchpin#459's milestone moves and
body notes, #456's superseded proposal, #327's PR retarget and rework). This
module is their one owner, used by both decision executors
(``tech_lead_operator_decision`` for ``propose_decision``,
``tech_lead_block_resolution`` for ``resolve_block``):

* :func:`bind_follow_through` (planning): a ``request_pr_rework`` step is bound
  to the decided issue's PR exactly as the session observed it at launch, the
  same immutable target a ``request_rework`` gets. The agent names a PR, never
  a head.
* :meth:`DecisionStepsOwner.refusal` (approval time, before ANY write): every
  step not yet applied must still be applicable. One that is not refuses the
  whole decision, so it never half-applies. A step already in its end state
  (a milestone already set, a PR already ``Refs``) is applicable.
* :meth:`DecisionStepsOwner.apply`: each step runs once, in order, through the
  owner of its write (the applier for comments, closes and PR rework, the
  standing-rulings owner for body blocks, the applier's mutation-authority
  check before each repository write). A durable marker on the proposal
  records each applied step, so a replay never runs one twice. The PR rework
  runs only after the item is released: the engine refuses a blocked issue's
  rework. A step whose precondition breaks between the check and its write
  (a race) stops the phase unmarked and the item unreleased; the replay's
  check then closes the proposal stale, naming the step. Only a step after
  the release, when the item already moved, is recorded as refused instead.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from ..domain.host_rate_limit import rate_limit_cause
from ..domain.human_block import BlockOutcome, NeedsHumanCause
from ..domain.decision_steps import (
    DecisionFollowThrough,
    DecisionStep,
    DecisionStepKind,
    step_marker,
    step_started_marker,
)
from ..domain.pr_issue_reference import body_links_issue, refs_in_place_of_closes
from ..domain.scoped_rework import ReworkRequest, ReworkTarget
from ..domain.standing_ruling import RulingAuthority, RulingScope
from ..domain.tech_lead_approval import ProposalLabelState, proposal_state
from .action_results import ActionResultType
from .actions import Action, ActionResult, AddCommentAction, CloseIssueAction, RequestReworkAction
from .claim_gate import ClaimLostError
from .human_gates import merge_decision_request
from .reconciliation import ReconciliationRequired, build_expected_for_mutation
from .review_scope import extract_issue_number_from_pr
from .scoped_rework_eligibility import rework_target_stale_reason

if TYPE_CHECKING:
    from ..domain.tech_lead_artifacts import ProposedTechLeadAction
    from ..ports.issue import Issue
    from ..ports.pull_request_tracker import PRInfo
    from .needs_human_block import SharedNeedsHumanBlock
    from ..domain.standing_ruling import StandingRuling
    from .standing_rulings import StandingRulingsOwner


def bind_follow_through(
    proposed: "ProposedTechLeadAction", *, rework_targets: Sequence[ReworkTarget], report_text: str,
) -> DecisionFollowThrough:
    """The proposal's follow-through with each PR rework bound to its observed head."""
    follow_through = proposed.follow_through
    subject = proposed.target_number
    assert subject is not None  # a decision always names its item (validate())
    brief = decision_brief(proposed)
    steps: list[DecisionStep] = []
    for step in follow_through.steps:
        if step.kind is DecisionStepKind.REQUEST_PR_REWORK:
            target = next((t for t in rework_targets if t.pr_number == step.number), None)
            if target is None or target.issue_number != subject:
                raise ValueError(
                    f"request_pr_rework of PR #{step.number} has no launch-observed target for #{subject}"
                )
            step = replace(step, rework=ReworkRequest(
                target=target,
                evidence_identity=hashlib.sha256(
                    json.dumps([proposed.action_type, subject, brief], sort_keys=True).encode()
                ).hexdigest(),
                report=report_text.strip() or brief,
                feedback=brief,
            ))
        steps.append(step)
    bound = follow_through.with_steps(steps)
    bound.validate_for(subject)
    return bound


def decision_brief(proposed: "ProposedTechLeadAction") -> str:
    """The decision as a PR rework's brief: its title and body."""
    if proposed.resolution is not None:
        return f"## {proposed.resolution.title}\n\n{proposed.resolution.body}"
    return f"## {proposed.title or ''}\n\n{proposed.body or ''}"


@dataclass(frozen=True)
class DecisionStepContext:
    """One approved decision, as its steps need it."""

    #: The decision's own command: the authority every step write is checked as.
    action: Action
    subject: int
    proposal_issue_number: int
    anchor_issue_number: int
    proposal_id: str
    finding_ids: tuple[str, ...]
    #: The rulings the decision itself records on its item, preflighted with
    #: every ruling its steps add there (#8691 r3 F2).
    subject_rulings: tuple["StandingRuling", ...] = ()


@dataclass(frozen=True)
class StepRefusal:
    """Why a decision's steps cannot all be carried out, and which already were."""

    reason: str
    applied: tuple[int, ...] = ()

    @property
    def partial(self) -> bool:
        return bool(self.applied)

    def hand_back(self) -> str:
        """The operator's message for a partial refusal: nothing is closed as stale."""
        done = ", ".join(str(index) for index in self.applied)
        return (f"approved decision partly applied: step(s) {done} were carried out, but {self.reason}."
                " The proposal stays open: finish the rest by hand, or close it.")


@dataclass(frozen=True)
class StepsApplied:
    """What :meth:`DecisionStepsOwner.apply` did: a retryable failure, else the
    steps it applied and any refused at write time (a race after the check)."""

    failed: str | None = None
    applied: tuple[int, ...] = ()
    refused: tuple[str, ...] = ()


@dataclass(frozen=True)
class DecisionStepsOwner:
    """See the module docstring."""

    read_issue: Callable[[int], "Issue | None"]
    read_pr: Callable[[int], "PRInfo | None"]
    list_milestones: Callable[[], list[dict[str, Any]]]
    set_milestone: Callable[[int, int], None]
    #: The repository's body write: a PR's body is its issue body on GitHub.
    write_body: Callable[[int, str], None]
    comment_marker_present: Callable[[int, str], bool]
    apply_action: Callable[[Action], ActionResult]
    require_authority: Callable[[Action, int], None]
    rulings: "StandingRulingsOwner"
    #: The shared needs-human block's owner: the approved decision is the
    #: person's merge decision a PR's merge hold waits for (#7678).
    block: "SharedNeedsHumanBlock"
    repo_slug: str

    # -- approval-time check ------------------------------------------------------

    def refusal(self, context: DecisionStepContext, follow_through: DecisionFollowThrough) -> StepRefusal | None:
        """Why the steps cannot all be carried out now, or None. Reads only.

        A refusal after some step already applied (a race broke a later one) is
        PARTIAL: the decision changed things and cannot be closed as stale."""
        if not follow_through.steps:
            return None
        touched: list[int] = []
        rulings: dict[int, list["StandingRuling"]] = {context.subject: list(context.subject_rulings)}
        for index, step in enumerate(follow_through.steps, start=1):
            if self._applied(context, index):
                touched.append(index)
                continue
            if self._started(context, index):
                # Its write may have landed before its marker failed: changed, maybe.
                touched.append(index)
            why = self._precondition(context, index, step)
            if why is not None:
                return StepRefusal(
                    f"step {index} ({step.kind.value} #{step.number}) cannot be carried out: {why}",
                    applied=tuple(touched),
                )
            if step.kind is DecisionStepKind.RECORD_RULING:
                rulings.setdefault(step.number, []).append(self._step_ruling(context, index, step))
        for number, wanted in rulings.items():
            # Every ruling the decision adds to one body, together (#8691 r3 F2).
            stepped = any(s.kind is DecisionStepKind.RECORD_RULING and s.number == number
                          for s in follow_through.steps)
            why = self.rulings.unrecordable_all(number, tuple(wanted)) if stepped else None
            if why is not None:
                return StepRefusal(f"the rulings this decision records on #{number} cannot all be"
                                   f" recorded: {why}", applied=tuple(touched))
        return None

    def check_authority(self, context: DecisionStepContext, follow_through: DecisionFollowThrough) -> None:
        """The applier's mutation-authority check on every step's target, before
        the decision's first write; each write checks again. Raises."""
        for _index, step in enumerate(follow_through.steps, start=1):
            self.require_authority(context.action, step.number)

    def _precondition(self, context: DecisionStepContext, index: int, step: DecisionStep) -> str | None:
        if step.acts_on_pr:
            return self._pr_precondition(context, step)
        return self._issue_precondition(context, index, step)

    def _pr_precondition(self, context: DecisionStepContext, step: DecisionStep) -> str | None:
        pr = self.read_pr(step.number)
        if pr is None:
            return f"PR #{step.number} could not be read"
        if step.kind is DecisionStepKind.COMMENT:
            return None
        if step.kind is DecisionStepKind.RETARGET_PR:
            return _retarget_precondition(pr, context.subject, self.repo_slug)
        assert step.rework is not None  # bound at planning (bind_follow_through)
        return rework_target_stale_reason(step.rework, pr, self.read_issue(context.subject))

    def _issue_precondition(self, context: DecisionStepContext, index: int, step: DecisionStep) -> str | None:
        number = step.number
        issue = self.read_issue(number)
        if issue is None:
            return f"#{number} could not be read"
        if step.kind is DecisionStepKind.CLOSE_SUPERSEDED_PROPOSAL:
            if issue.state != "open":
                return None  # already closed: nothing left to do
            # Only a proposal nobody has approved: one carrying a maintainer's
            # `approved` (claimed or admitted) is the operator's, never closed here.
            if proposal_state(issue.labels, issue.body) is not ProposalLabelState.AWAITING:
                return f"#{number} is not a tech-lead proposal awaiting approval (unapproved)"
            return None
        if step.kind is DecisionStepKind.COMMENT:
            return None
        if issue.state != "open":
            return f"#{number} is {issue.state}"
        if step.kind is DecisionStepKind.SET_MILESTONE and self._milestone_number(step.milestone) is None:
            return f"no open milestone is named {step.milestone!r}"
        return None

    # -- execution ------------------------------------------------------------------

    def apply(
        self, context: DecisionStepContext, follow_through: DecisionFollowThrough, *, after_release: bool,
    ) -> StepsApplied:
        """Apply the steps of one phase not yet applied, in order."""
        phase = follow_through.after_release if after_release else follow_through.before_release
        applied: list[int] = []
        refused: list[str] = []
        for index, step in phase:
            if self._applied(context, index):
                continue
            try:
                refusal = self._apply_step(context, index, step)
            except (ReconciliationRequired, ClaimLostError):
                raise
            except Exception as error:  # a replay resumes at this step
                return StepsApplied(failed=f"step {index} ({step.kind.value} #{step.number}): {error}")
            if refusal is not None and not after_release:
                # Its precondition broke after the decision's check: stop before
                # this step, mark nothing and release nothing. The op's replay
                # re-checks every step and closes the proposal stale, naming it.
                return StepsApplied(failed=f"step {index} ({step.kind.value} #{step.number}) no longer"
                                           f" applies: {refusal}")
            outcome = (
                f"Step {index} applied: {step.describe(context.subject)}" if refusal is None
                else f"Step {index} not applied: {refusal}"
            )
            marked = self.apply_action(AddCommentAction(
                number=context.proposal_issue_number,
                comment=f"{outcome}\n\n{step_marker(str(context.proposal_issue_number), index)}",
                reason=f"decision step {index} of proposal #{context.proposal_issue_number}",
                expected=build_expected_for_mutation(),
            ))
            if not marked.success:
                return StepsApplied(failed=f"step {index} marker not posted: {marked.error}")
            if refusal is None:
                applied.append(index)
            else:
                refused.append(f"step {index}: {refusal}")
        return StepsApplied(applied=tuple(applied), refused=tuple(refused))

    def _start(self, context: DecisionStepContext, index: int) -> str | None:
        """Record, before its first write, that step *index* is under way, so a
        write that lands while its applied marker fails is never read as "no
        changes" (#8691 r3 F1). Every step's write is idempotent, so a replay
        that finds it started runs it again."""
        proposal = context.proposal_issue_number
        marker = step_started_marker(str(proposal), index)
        if self.comment_marker_present(proposal, marker):
            return None
        posted = self.apply_action(AddCommentAction(
            number=proposal, comment=f"Step {index} started.\n\n{marker}",
            reason=f"decision step {index} of proposal #{proposal} started",
            expected=build_expected_for_mutation(),
        ))
        return None if posted.success else f"step {index} start not recorded: {posted.error}"

    def _started(self, context: DecisionStepContext, index: int) -> bool:
        return self.comment_marker_present(
            context.proposal_issue_number, step_started_marker(str(context.proposal_issue_number), index)
        )

    def _applied(self, context: DecisionStepContext, index: int) -> bool:
        return self.comment_marker_present(
            context.proposal_issue_number, step_marker(str(context.proposal_issue_number), index)
        )

    def _apply_step(self, context: DecisionStepContext, index: int, step: DecisionStep) -> str | None:
        """Carry out one step; why it was refused at write time, else None. Raises on failure."""
        why = self._precondition(context, index, step)
        if why is not None:
            return why
        started = self._start(context, index)
        if started is not None:
            raise RuntimeError(started)
        writes: dict[DecisionStepKind, Callable[[DecisionStepContext, int, DecisionStep], str | None]] = {
            DecisionStepKind.SET_MILESTONE: self._set_milestone,
            DecisionStepKind.RECORD_RULING: self._record_ruling,
            DecisionStepKind.CLOSE_SUPERSEDED_PROPOSAL: self._close_proposal,
            DecisionStepKind.RETARGET_PR: self._retarget_pr,
            DecisionStepKind.REQUEST_PR_REWORK: self._request_rework,
            DecisionStepKind.COMMENT: self._comment,
        }
        return writes[step.kind](context, index, step)

    def _set_milestone(self, context: DecisionStepContext, index: int, step: DecisionStep) -> str | None:
        milestone = self._milestone_number(step.milestone)
        issue = self.read_issue(step.number)
        assert milestone is not None and issue is not None  # the precondition just held
        if issue.milestone_number != milestone:
            self.require_authority(context.action, step.number)
            self.set_milestone(step.number, milestone)
        return None

    def _record_ruling(self, context: DecisionStepContext, index: int, step: DecisionStep) -> str | None:
        self.require_authority(context.action, step.number)
        self.rulings.record(step.number, self._step_ruling(context, index, step))
        return None

    def _step_ruling(self, context: DecisionStepContext, index: int, step: DecisionStep) -> "StandingRuling":
        proposal = context.proposal_issue_number
        return self.rulings.ruling(
            ruling_id=f"ds-{proposal}-{index}",
            text=step.text,
            authority=RulingAuthority.APPROVED_DECISION,
            source=f"tech-lead decision on #{context.subject}, approved on proposal #{proposal} (step {index})",
            scope=RulingScope(),
        )

    def _close_proposal(self, context: DecisionStepContext, index: int, step: DecisionStep) -> str | None:
        issue = self.read_issue(step.number)
        if issue is not None and issue.state != "open":
            return None
        closed = self.apply_action(CloseIssueAction(
            issue_number=step.number,
            comment=(f"Superseded by the decision on #{context.subject} approved on proposal"
                     f" #{context.proposal_issue_number}; closed without acting."),
            reason=f"proposal #{context.proposal_issue_number} supersedes proposal #{step.number}",
            expected=build_expected_for_mutation(),
        ))
        if not closed.success:
            raise RuntimeError(f"proposal #{step.number} not closed: {closed.error}") from (
                rate_limit_cause(closed.host_rate_limit))
        return None

    def _retarget_pr(self, context: DecisionStepContext, index: int, step: DecisionStep) -> str | None:
        pr = self.read_pr(step.number)
        assert pr is not None  # the precondition just held
        body = refs_in_place_of_closes(pr.body or "", context.subject, repo_slug=self.repo_slug)
        if body != (pr.body or ""):
            self.require_authority(context.action, step.number)
            self.write_body(step.number, body)
        return None

    def _request_rework(self, context: DecisionStepContext, index: int, step: DecisionStep) -> str | None:
        assert step.rework is not None  # bound at planning
        result = self.apply_action(RequestReworkAction(
            request=step.rework, proposal_id=context.proposal_id, finding_ids=context.finding_ids,
            anchor_issue_number=context.anchor_issue_number,
            proposal_issue_number=context.proposal_issue_number,
            reason=f"decision approved on proposal #{context.proposal_issue_number}: rework PR #{step.number}",
            expected=build_expected_for_mutation(),
        ))
        if result.success:
            # Only once the rework is queued: a refused rework keeps the hold (#8691 r3 F3).
            self._release_merge_hold(context, step.number)
            return None
        if result.result_type is ActionResultType.SKIPPED:  # the rework owner refused a stale target
            return f"the rework owner refused PR #{step.number}: {result.details['skip_reason']}"
        raise RuntimeError(f"rework of PR #{step.number} did not finish: {result.error}") from (
            rate_limit_cause(result.host_rate_limit))

    def _release_merge_hold(self, context: DecisionStepContext, pr_number: int) -> None:
        """Release the PR's merge-decision hold (a forced completion's ``needs-human``
        on the PR): the operator just made the decision it waits for. Any other
        cause keeps the label, and the rework owner leaves it standing."""
        recorded = self.block.recorded_causes((pr_number,)).get(pr_number, frozenset())
        if NeedsHumanCause.MERGE_DECISION not in recorded:
            return
        self.require_authority(context.action, pr_number)
        outcome = self.block.release(merge_decision_request(
            pr_number, f"decided by the operator's approval of proposal #{context.proposal_issue_number}",
        ))
        if outcome in (BlockOutcome.FAILED, BlockOutcome.UNGOVERNED):
            raise RuntimeError(f"PR #{pr_number}'s merge hold did not come off ({outcome.value})")

    def _comment(self, context: DecisionStepContext, index: int, step: DecisionStep) -> str | None:
        marker = f"<!-- io:decision-step-comment:{context.proposal_issue_number}:{index} -->"
        if self.comment_marker_present(step.number, marker):
            return None
        posted = self.apply_action(AddCommentAction(
            number=step.number, is_pr=step.on_pr,
            comment=(f"{step.text}\n\n_From the tech-lead decision on #{context.subject}, approved on"
                     f" proposal #{context.proposal_issue_number}._\n{marker}"),
            reason=f"decision step {index} of proposal #{context.proposal_issue_number}",
            expected=build_expected_for_mutation(),
        ))
        if not posted.success:
            raise RuntimeError(f"comment on #{step.number} not posted: {posted.error}") from (
                rate_limit_cause(posted.host_rate_limit))
        return None

    def _milestone_number(self, title: str) -> int | None:
        wanted = title.strip().casefold()
        for milestone in self.list_milestones():
            if str(milestone.get("title", "")).strip().casefold() == wanted and isinstance(milestone.get("number"), int):
                return int(milestone["number"])
        return None


def _retarget_precondition(pr: "PRInfo", subject: int, repo_slug: str) -> str | None:
    if pr.state != "open":
        return f"PR #{pr.number} is {pr.state}"
    owner = extract_issue_number_from_pr(pr, repo_slug=repo_slug)
    if owner != subject:
        # The PR's own issue (its branch, else its first link) must be the
        # decided one: a later "Closes #N" in another issue's PR is not ours.
        return f"PR #{pr.number} belongs to #{owner}, not #{subject}"
    if not body_links_issue(pr.body or "", (subject,), repo_slug=repo_slug):
        return f"PR #{pr.number}'s body does not link #{subject}"
    return None
