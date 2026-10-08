"""Apply-time owner of the tech lead's ``resolve_block`` (#7658).

A ``resolve_block`` decides a ``needs-human`` WORK block in the operator's
stead (see :mod:`..domain.block_resolution` for the vocabulary). The decision
is untrusted intent: immediately before any write this owner re-verifies, from
the owners that already answer each question, that it may stand:

1. **the item is open**, read fresh;
2. **nothing runs or claims it** - no live or unverifiable runtime owner, no
   claim but the proposing run's own, and no work session ran on it since the
   tech lead observed it (a decision about an older block is stale);
3. **the block is a work block the tech lead may decide** - not the tech
   lead's own hand-over (its marker), and every cause the decision names is on
   record in the shared block's owner now;
4. **it was never resolved before** - a durable marker on the item (and the
   authority store's record, across a crash before the marker) names every
   cause a resolution discharged; one from an EARLIER decision means the block
   was put back after it, so that cause is the operator's from then on;
5. **no human-only work** is named in the item, the agent's question or the
   decision itself (:func:`~..domain.block_resolution.human_only_work`).

A failed check REFUSES the decision with a typed
:class:`BlockResolutionRefusal` and no write. Otherwise, in an order that
keeps the item blocked until everything its next session needs is on GitHub:

* a split's children are filed create-once (by body marker), WITHOUT their
  agent label, and each must show the dependency parser exactly its decided
  edge (the issue-dependency-stacking contract). Filing is ``create_issue``'s
  call: when the tech lead may not file unattended, each child is filed as a
  proposal awaiting a maintainer's approval (#7763);
* the pr-pending gate goes on when an open PR carries the item's published
  validated work, so no coder relaunches over that PR;
* the decision is posted on the item, create-once, and an answer (or the
  slice a narrowing split leaves) is recorded as a STANDING RULING in the
  item's body (#8141), create-once by the decision: the resumed session, its
  reviews and every later rework read the body, never the comments
  (porchpin#327's approved answer stayed in a comment);
* the named causes are discharged through
  :meth:`~.needs_human_block.SharedNeedsHumanBlock.resolve`, bracketed
  write-ahead (``ports/block_resolution_discharges``): other causes keep the
  label;
* only once the discharge COMMITTED: the children get their agent label, the
  discharge is recorded on the item (one durable marker per cause), and the
  item is closed (a full split) or requeued if nothing else blocks it.

A replay of a decision whose discharge committed only finishes those last
steps and never discharges again, so it cannot clear a block raised after it;
one interrupted mid-discharge is handed back to the operator; one that never
began decides afresh, finding its earlier writes by their markers.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from ..domain.block_resolution import (
    PARENT,
    ParentDisposition,
    ResolutionChild,
    ResolutionKind,
    child_marker,
    cause_marker,
    decision_marker,
    discharge_marker,
    human_only_work,
    prior_resolutions,
)
from ..domain.dependencies import DependencyMode, parse_dependency_edges
from ..domain.standing_ruling import RulingAuthority, RulingScope, resolution_ruling_id
from ..domain.host_rate_limit import rate_limit_cause
from ..domain.human_block import BlockOutcome, HumanHoldScope, NeedsHumanCause
from ..domain.operator_decision_retry import DecisionRetryState
from ..domain.tech_lead_approval import (
    GATED_PROPOSAL_LABELS,
    filed_proposal_numbers,
    with_proposal_marker,
)
from ..events import EventName
from ..infra.logging_config import issue_log
from ..ports import EventSink, make_trace_event
from ..ports.repository_host import host_rate_limit_of
from .actions import (
    Action,
    ActionResult,
    AddCommentAction,
    AddLabelAction,
    CloseIssueAction,
    ResolveBlockAction,
)
from .claim_gate import ClaimLostError
from .reconciliation import ReconciliationRequired, build_expected_for_mutation
from .tech_lead_decision_steps import DecisionStepContext
from .tech_lead_reset_retry import STALE_DOWNGRADE_MODE, publish_proposal_surfaced

if TYPE_CHECKING:
    from ..domain.models import SessionHistoryEntry
    from ..ports.issue import Issue
    from .issue_work_claims import IssueWorkClaim
    from .label_manager import LabelManager
    from ..ports.block_resolution_discharges import BlockResolutionDischarges
    from .human_gates import HumanGates
    from .needs_human_block import SharedNeedsHumanBlock
    from .published_review_custody import PublishedReviewHolds
    from .review_exchange_lifecycle import IssueRuntimeActivity
    from ..domain.standing_ruling import StandingRuling
    from .standing_rulings import StandingRulingsOwner
    from .tech_lead_decision_steps import DecisionStepsOwner

logger = logging.getLogger(__name__)

OP_TYPE = ResolveBlockAction.op_type
_COMMITTED, _BEGUN = DecisionRetryState.COMMITTED, DecisionRetryState.BEGUN
_RATIONALE_PREVIEW_CHARS = 500


class BlockResolutionRefusal(StrEnum):
    """Why a resolution was refused before any write. Stable codes."""

    ISSUE_UNREADABLE = "issue_unreadable"
    ISSUE_CLOSED = "issue_closed"
    LIVE_SESSION = "live_session"
    WORK_CLAIMED = "work_claimed"
    #: A session ran on the item since the tech lead observed it.
    NEWER_SESSION = "newer_session"
    #: The item carries no needs-human: nothing is left to decide.
    NOT_BLOCKED = "not_blocked"
    #: The tech lead's own hand-over holds it: that is the operator's, always.
    TECH_LEAD_HAND_OVER = "tech_lead_hand_over"
    #: The item's needs-human holds only a merge (#7678): the operator's.
    MERGE_HOLD = "merge_hold"
    #: A split that closes the item while a cause it does not name holds it.
    CLOSE_WHILE_HELD = "close_while_held"
    #: The engine stopped mid-discharge: what stands now may be newer.
    INTERRUPTED = "interrupted"
    #: A cause the decision names is not on record now.
    CAUSE_NOT_RECORDED = "cause_not_recorded"
    #: An earlier resolution discharged a cause the decision names, and the
    #: block came back: the operator's from then on.
    RESOLVED_BEFORE = "resolved_before"
    #: The item, the agent's question or the decision names human-only work.
    HUMAN_ONLY_WORK = "human_only_work"
    #: A step the decision executes beyond its item no longer applies (#8691).
    STEP_NOT_APPLICABLE = "step_not_applicable"


@dataclass(frozen=True, slots=True)
class RefusedResolution:
    code: BlockResolutionRefusal
    detail: str
    #: Set when the decision's steps were partly applied before a later one
    #: broke (#8691): the hand-back message; such a refusal never closes stale.
    partial: str = ""

    def describe(self) -> str:
        return f"{self.code.value}: {self.detail}"

    @property
    def goal_already_met(self) -> bool:
        """Nothing blocks the item on needs-human any more."""
        return self.code is BlockResolutionRefusal.NOT_BLOCKED


@dataclass(frozen=True, slots=True)
class ResolvableBlock:
    issue: "Issue"


@dataclass(frozen=True)
class TechLeadBlockResolutionExecutor:
    """Applies :class:`ResolveBlockAction` after re-verifying it (module docstring)."""

    events: EventSink
    labels: "LabelManager"
    block: "SharedNeedsHumanBlock"
    #: THE owner of what a blocking label holds (#7678): a resolution acts
    #: only where the item's work is held, never on a merge hold.
    gates: "HumanGates"
    #: Fresh issue read (labels, body, milestone, state).
    read_issue: Callable[[int], "Issue | None"]
    #: The item's comment bodies that carry a resolution marker, from a
    #: complete, uncached scan that raises rather than answer partially.
    read_comment_bodies: Callable[[int], Sequence[str]]
    #: Every question an agent put to a human about the item, from its whole
    #: timeline (the human-only screen reads them all; a read failure raises).
    agent_questions: Callable[[int], Sequence[str]]
    runtime_activity: Callable[[int], "IssueRuntimeActivity"]
    claims_on_issue: Callable[[int], Sequence["IssueWorkClaim"]]
    sessions_not_before: Callable[[int, datetime], Sequence["SessionHistoryEntry"]]
    published_review: "PublishedReviewHolds"
    find_issue_by_marker: Callable[..., int | None]
    create_issue: Callable[..., "dict[str, Any] | None"]
    #: The applier's own dispatch: comments, labels and the close are normal
    #: claim-verified, reconciliation-guarded writes.
    apply_action: Callable[[Action], ActionResult]
    #: The applier's mutation-authority check for a write about the item, run
    #: before each child is filed.
    require_authority: Callable[[Action, int], None]
    #: Put the item back in the planner's view (local retry gates and the
    #: cached copy, from fresh labels); the blocking labels it still carries.
    requeue: Callable[[int], tuple[str, ...]]
    #: The write-ahead record of each decision's discharge.
    discharges: "BlockResolutionDischarges"
    #: The approval owner's durable proposal index (#7763): a gated child
    #: joins it the moment it is filed, as every tech-lead proposal does.
    index_proposals: Callable[[list[int]], None]
    #: The owner the decided answer is recorded through as a standing ruling (#8141).
    rulings: "StandingRulingsOwner"
    #: The owner of the decision's steps beyond the item (#8691).
    steps: "DecisionStepsOwner"

    # -- preconditions --------------------------------------------------------

    def verify(self, action: ResolveBlockAction) -> RefusedResolution | ResolvableBlock:
        """Every precondition, cheapest first; the first that fails refuses."""
        local = self._local_refusal(action)
        if local is not None:
            return local
        issue = self.read_issue(action.issue_number)
        if issue is None:
            return RefusedResolution(BlockResolutionRefusal.ISSUE_UNREADABLE,
                                     f"issue #{action.issue_number} could not be read")
        if issue.state != "open":
            return RefusedResolution(BlockResolutionRefusal.ISSUE_CLOSED,
                                     f"issue #{action.issue_number} is {issue.state}")
        priors = prior_resolutions(self.read_comment_bodies(action.issue_number))
        for value, decisions in self.discharges.resolved_causes(issue_number=action.issue_number).items():
            cause = NeedsHumanCause(value)
            priors[cause] = priors.get(cause, frozenset()) | decisions
        refusal = self._block_refusal(action, issue, priors)
        if refusal is None and (step := self.steps.refusal(self._steps(action), action.follow_through)):
            refusal = RefusedResolution(BlockResolutionRefusal.STEP_NOT_APPLICABLE, step.reason,
                                        partial=step.hand_back() if step.partial else "")
        return refusal or ResolvableBlock(issue)

    def stale_reason(self, action: ResolveBlockAction) -> str | None:
        """Read-only applicability, for handing off to an existing proposal."""
        verdict = self.verify(action)
        return verdict.describe() if isinstance(verdict, RefusedResolution) else None

    def _local_refusal(self, action: ResolveBlockAction) -> RefusedResolution | None:
        number = action.issue_number
        activity = self.runtime_activity(number)
        if activity.busy:
            owners = sorted(kind.value for kind in activity.active | activity.unverifiable)
            return RefusedResolution(BlockResolutionRefusal.LIVE_SESSION,
                                     f"issue #{number} runtime owners active or unverifiable: {owners}")
        claimed = sorted(
            claim.describe() for claim in self.claims_on_issue(number)
            if not claim.held_by(action.source_session_name, action.observed_at)
        )
        if claimed:
            return RefusedResolution(BlockResolutionRefusal.WORK_CLAIMED,
                                     f"issue #{number} has claimed work: {claimed}")
        newer = self.sessions_not_before(number, datetime.fromisoformat(action.observed_at))
        if newer:
            seen = ", ".join(str(entry.status) for entry in newer)
            return RefusedResolution(BlockResolutionRefusal.NEWER_SESSION,
                                     f"issue #{number} ran since the tech lead observed it"
                                     f" at {action.observed_at}: {seen}")
        return None

    def _block_refusal(
        self,
        action: ResolveBlockAction,
        issue: "Issue",
        priors: dict[NeedsHumanCause, frozenset[str]],
    ) -> RefusedResolution | None:
        number = issue.number
        folded = {label.casefold() for label in issue.labels}
        handed_over = self.labels.tech_lead_needs_human.casefold() in folded
        if handed_over:
            return RefusedResolution(BlockResolutionRefusal.TECH_LEAD_HAND_OVER,
                                     f"#{number} carries the tech lead's own hand-over"
                                     f" ({self.labels.tech_lead_needs_human}); only the operator ends it")
        causes = action.resolution.causes
        earlier = sorted(
            cause.value for cause in causes
            if priors.get(cause, frozenset()) - {action.decision_id}
        )
        if earlier:
            return RefusedResolution(BlockResolutionRefusal.RESOLVED_BEFORE,
                                     f"#{number}'s {', '.join(earlier)} was resolved by the tech lead"
                                     " before and the block came back: it is the operator's now")
        screened = human_only_work((
            issue.title, issue.body, *self.agent_questions(number), *action.resolution.texts,
        ))
        if screened is not None:
            return RefusedResolution(BlockResolutionRefusal.HUMAN_ONLY_WORK,
                                     f"#{number} names human-only work, {screened.describe()}:"
                                     " it is handed over, never resolved")
        scope = self.gates.needs_human_scope(number, issue.labels)
        if scope is None:
            return RefusedResolution(BlockResolutionRefusal.NOT_BLOCKED,
                                     f"#{number} no longer carries {self.labels.needs_human}")
        work_held = scope is HumanHoldScope.WORK and self.gates.holds_work(number, issue.labels)
        if not work_held:
            return RefusedResolution(BlockResolutionRefusal.MERGE_HOLD,
                                     f"#{number}'s {self.labels.needs_human} holds only a merge:"
                                     " the operator merges, by design")
        recorded = self.block.recorded_causes((number,)).get(number, frozenset())
        missing = sorted(cause.value for cause in causes if cause not in recorded)
        if missing:
            on_record = sorted(cause.value for cause in recorded) or ["none (the operator's own label)"]
            return RefusedResolution(BlockResolutionRefusal.CAUSE_NOT_RECORDED,
                                     f"#{number}'s needs-human is not held by {', '.join(missing)}"
                                     f" (on record: {', '.join(on_record)})")
        others = sorted(cause.value for cause in recorded - causes)
        closes = action.resolution.parent is ParentDisposition.CLOSE
        if closes and others:
            return RefusedResolution(BlockResolutionRefusal.CLOSE_WHILE_HELD,
                                     f"the split closes #{number}, but {', '.join(others)} still"
                                     " holds it: closing would bury that cause")
        return None

    # -- apply ----------------------------------------------------------------

    def apply(self, action: ResolveBlockAction) -> ActionResult:
        """Decide afresh, finish a committed discharge, or hand back an interrupted one."""
        prior = self.discharges.block_resolution_state(decision_id=action.decision_id)
        committed, interrupted = (prior is state for state in (_COMMITTED, _BEGUN))
        if committed:
            return self._finish(action)
        if interrupted:
            return self._refuse(action, RefusedResolution(
                BlockResolutionRefusal.INTERRUPTED,
                f"the engine stopped while {action.decision_id} discharged #{action.issue_number}'s"
                " causes; whatever stands on it now may have been raised since, so it is the"
                " operator's",
            ))
        verdict = self._verified(action)
        if isinstance(verdict, ActionResult):
            return verdict
        issue = verdict.issue
        try:
            filed = self._file_children(action, issue)
        except (ReconciliationRequired, ClaimLostError):
            raise
        except Exception as error:  # the item stays blocked; a replay resumes by marker
            return ActionResult.fail_limited(
                action, f"split child not filed: {error}", host_rate_limit_of(error),
                issue_number=action.issue_number, proposal_id=action.proposal_id)
        for what, step in (
            (f"{self.labels.pr_pending} not put on", lambda: self._gate_published_review(action, issue)),
            ("decision not posted", lambda: self._post_decision(action)),
            ("standing ruling not recorded on", lambda: self._record_ruling(action)),
            ("decision step not applied for", lambda: self._apply_steps(action, after_release=False)),
        ):
            failed = step()
            if failed is not None:
                return ActionResult.fail_limited(
                    action, f"{what} #{action.issue_number}: {failed.error}", failed.host_rate_limit,
                    issue_number=action.issue_number, proposal_id=action.proposal_id)
        self.discharges.begin_block_resolution(
            decision_id=action.decision_id, issue_number=action.issue_number,
            causes=frozenset(cause.value for cause in action.resolution.causes),
        )
        discharged = self.block.resolve(
            action.issue_number, action.resolution.causes,
            f"tech lead {action.decision_id} resolved: {action.resolution.title}",
        )
        outcome = discharged.outcome
        if outcome not in (BlockOutcome.CLEARED, BlockOutcome.HELD_BY_ANOTHER_CAUSE):
            # A failed outcome after an attempted write may have taken the
            # label off anyway (the confirming read failed), and only the owner
            # knows whether it attempted one: then a replay is handed back
            # (INTERRUPTED); a failure before any write is decided afresh.
            if not discharged.mutation_attempted:
                self.discharges.abandon_block_resolution(decision_id=action.decision_id)
            return ActionResult.fail(
                action, f"needs-human on #{action.issue_number} did not settle ({outcome.value})",
                issue_number=action.issue_number, proposal_id=action.proposal_id)
        self.discharges.commit_block_resolution(decision_id=action.decision_id)
        return self._settle(action, issue, outcome, filed=filed)

    def _verified(self, action: ResolveBlockAction) -> ResolvableBlock | ActionResult:
        """The block to decide, its steps' targets authority-checked, or the outcome of a refusal."""
        verdict = self.verify(action)
        if isinstance(verdict, RefusedResolution):
            if verdict.partial:  # some steps already changed other items: the operator's, never stale
                return ActionResult.fail(action, verdict.partial, issue_number=action.issue_number,
                                         proposal_id=action.proposal_id)
            return self._refuse(action, verdict)
        self.steps.check_authority(self._steps(action), action.follow_through)
        return verdict

    def _finish(self, action: ResolveBlockAction) -> ActionResult:
        """The discharge committed before: never discharge again, only settle."""
        issue = self.read_issue(action.issue_number)
        if issue is None:
            raise RuntimeError(f"issue #{action.issue_number} could not be read to finish its resolution")
        held = any(label.casefold() == self.labels.needs_human.casefold() for label in issue.labels)
        outcome = BlockOutcome.HELD_BY_ANOTHER_CAUSE if held else BlockOutcome.CLEARED
        return self._settle(action, issue, outcome, filed=None)

    def _settle(
        self, action: ResolveBlockAction, parent: "Issue", outcome: BlockOutcome,
        *, filed: tuple[int, ...] | None,
    ) -> ActionResult:
        """What follows a COMMITTED discharge, each step create-once so a
        replay finishes it: the split's children become runnable, the
        discharge markers go on the item, then the item is closed (a full
        split) or requeued."""
        try:
            children = self._activate_children(action, parent, filed)
        except (ReconciliationRequired, ClaimLostError):
            raise
        except Exception as error:
            return ActionResult.fail_limited(
                action, f"split child not made runnable: {error}", host_rate_limit_of(error),
                issue_number=action.issue_number, proposal_id=action.proposal_id)
        marked = self._post_discharge(action)
        if marked is not None:
            return ActionResult.fail_limited(
                action, f"discharge not recorded on #{action.issue_number}: {marked.error}",
                marked.host_rate_limit, issue_number=action.issue_number, proposal_id=action.proposal_id)
        stepped = self._apply_steps(action, after_release=True)
        if stepped is not None:  # the discharge committed: a replay finishes the steps only
            return ActionResult.fail(action, f"decision step not applied for #{action.issue_number}: {stepped.error}",
                                     issue_number=action.issue_number, proposal_id=action.proposal_id)
        if parent.state != "open":
            return self._applied(action, children=children, outcome=outcome, still_blocked=())
        if action.resolution.parent is ParentDisposition.CLOSE and outcome is BlockOutcome.CLEARED:
            closed = self.apply_action(CloseIssueAction(
                issue_number=action.issue_number,
                reason=f"tech lead {action.decision_id}: split into its children",
                expected=build_expected_for_mutation(),
            ))
            if not closed.success:
                return ActionResult.fail_limited(
                    action, f"#{action.issue_number} not closed: {closed.error}",
                    closed.host_rate_limit,
                    issue_number=action.issue_number, proposal_id=action.proposal_id)
            still_blocked: tuple[str, ...] = ()
        elif outcome is BlockOutcome.CLEARED:
            still_blocked = self.requeue(action.issue_number)
        else:
            # Another cause holds the label: its own owner decides when the
            # item moves again, never this resolution.
            still_blocked = (self.labels.needs_human,)
        return self._applied(action, children=children, outcome=outcome, still_blocked=still_blocked)

    def _applied(
        self, action: ResolveBlockAction, *, children: tuple[int, ...],
        outcome: BlockOutcome, still_blocked: tuple[str, ...],
    ) -> ActionResult:
        causes = sorted(cause.value for cause in action.resolution.causes)
        self.events.publish(make_trace_event(EventName.TECH_LEAD_ACTION_EXECUTED, {
            "issue_number": action.anchor_issue_number,
            "action_id": action.proposal_id,
            "proposal_type": OP_TYPE,
            "target_number": action.issue_number,
            "finding_ids": list(action.finding_ids),
            "boundary": {
                "kind": action.resolution.kind.value,
                "causes": causes,
                "block": outcome.value,
                "children": list(children),
                "still_blocked_by": list(still_blocked),
            },
        }))
        logger.info(issue_log(action.issue_number,
            "Tech Lead %s %s resolved %s (%s): block %s, children %s, still blocked by %s"),
            OP_TYPE, action.proposal_id, causes, action.resolution.kind.value,
            outcome.value, list(children), list(still_blocked))
        return ActionResult.ok(
            action, issue_number=action.issue_number, proposal_id=action.proposal_id,
            causes=causes, block=outcome.value, children=[str(n) for n in children],
            still_blocked_by=list(still_blocked),
        )

    def _refuse(self, action: ResolveBlockAction, refused: RefusedResolution) -> ActionResult:
        stale = refused.describe()
        logger.warning(issue_log(action.issue_number, "Tech Lead %s %s refused: %s"),
                       OP_TYPE, action.proposal_id, stale)
        publish_proposal_surfaced(
            self.events,
            issue_number=action.anchor_issue_number,
            action_id=action.proposal_id,
            proposal_type=OP_TYPE,
            target_number=action.issue_number,
            target_is_pr=False,
            title=action.resolution.title,
            body_preview=action.rationale[:_RATIONALE_PREVIEW_CHARS],
            finding_ids=action.finding_ids,
            mode=STALE_DOWNGRADE_MODE,
            stale_reason=stale,
            boundary={"refusal": refused.code.value},
        )
        return ActionResult.skip(
            action, f"stale precondition: {stale}",
            refusal=refused.code.value,
            terminal_disposition_satisfied=refused.goal_already_met,
            mode=STALE_DOWNGRADE_MODE,
            issue_number=action.issue_number,
            proposal_id=action.proposal_id,
        )

    # -- writes ---------------------------------------------------------------

    def _file_children(self, action: ResolveBlockAction, issue: "Issue") -> tuple[int, ...]:
        """File each child create-once and verify its edge; none is runnable yet."""
        filed: list[int] = []
        for index, child in enumerate(action.resolution.children, start=1):
            filed.append(self._file_child(action, issue, index, child, tuple(filed)))
        return tuple(filed)

    def _file_child(
        self,
        action: ResolveBlockAction,
        parent: "Issue",
        index: int,
        child: ResolutionChild,
        earlier: tuple[int, ...],
    ) -> int:
        marker = child_marker(action.decision_id, index)
        number = self.find_issue_by_marker(title=child.title, marker=marker, authoritative=True)
        predecessor = _predecessor(child, parent, earlier)
        if number is None:
            self.require_authority(action, action.issue_number)
            edge = (
                f"\n\n{child.edge.directive}: #{predecessor}\n"
                if child.edge is not None and predecessor is not None
                else "\n"
            )
            labels = [*self._inherited_labels(parent), *(GATED_PROPOSAL_LABELS if action.children_gated else ())]
            body = (
                f"{child.body}{edge}\nRefs #{parent.number} — split out by the tech lead's"
                f" resolution {action.decision_id}.\n{marker}"
            )
            created = self.create_issue(
                title=child.title,
                # A gated child is a proposal (#7763): its body marker keeps it
                # one even if every approval label is stripped.
                body=with_proposal_marker(body) if action.children_gated else body,
                labels=labels,
                milestone=parent.milestone_number,
            )
            if not created or not isinstance(created.get("number"), int):
                raise RuntimeError(f"child {index} of #{parent.number}'s split was not created")
            number = int(created["number"])
            self.index_proposals(filed_proposal_numbers(labels, number))
        self._verify_edge(number, child, predecessor)
        return number

    def _activate_children(
        self, action: ResolveBlockAction, parent: "Issue", filed: tuple[int, ...] | None
    ) -> tuple[int, ...]:
        """After the discharge committed: label each filed child for pickup.

        Children are found by their markers (a replay never refiles one); a
        child missing now is a failure the replay retries, never skipped.
        """
        children: list[int] = []
        for index, child in enumerate(action.resolution.children, start=1):
            # The numbers this apply just filed; a replay finds them by marker
            # (GitHub's search index lags a fresh issue, so never right away).
            number = filed[index - 1] if filed is not None else self.find_issue_by_marker(
                title=child.title, marker=child_marker(action.decision_id, index), authoritative=True,
            )
            if number is None:
                raise RuntimeError(f"child {index} of #{parent.number}'s split is not on GitHub")
            # The graph again, as it stands NOW: a child closed or rewired since
            # it was filed is never made runnable (nor its parent closed).
            self._verify_edge(number, child, _predecessor(child, parent, tuple(children)))
            for label in (parent.agent_type,) if parent.agent_type else ():
                marked = self.apply_action(AddLabelAction(
                    issue_number=number, label=label,
                    reason=f"tech lead {action.decision_id}: child {index} ready for pickup",
                    expected=build_expected_for_mutation(),
                ))
                if not marked.success:
                    raise RuntimeError(
                        f"child #{number} not labelled {label}: {marked.error}"
                    ) from rate_limit_cause(marked.host_rate_limit)
            children.append(number)
        return tuple(children)

    def _verify_edge(self, number: int, child: ResolutionChild, predecessor: int | None) -> None:
        """The engine's own parser must see EXACTLY the decided graph (its one
        edge, or none) on the filed child before the child is runnable."""
        fresh = self.read_issue(number)
        if fresh is None or fresh.state != "open":
            raise RuntimeError(
                f"child #{number} is not open ({'unreadable' if fresh is None else fresh.state});"
                " the split's work would not be queued, so nothing is discharged"
            )
        # The complete identity: the same number in another repository, an
        # external id or a malformed line is a different graph.
        seen = [
            (edge.repository, edge.issue_number, edge.external_id, edge.mode, edge.problem)
            for edge in parse_dependency_edges(fresh.body or "")
        ]
        wanted = [] if child.edge is None else [(
            None, predecessor, None,
            DependencyMode.NORMAL if child.edge.directive == "Depends-on" else DependencyMode.STACK,
            None,
        )]
        if seen != wanted:
            raise RuntimeError(
                f"child #{number}'s dependency edges are {seen}, not the decided {wanted};"
                " it is left without its agent label"
            )

    def _inherited_labels(self, parent: "Issue") -> list[str]:
        """The parent's own labels (priority, area), never workflow state or its agent."""
        return [
            label for label in parent.labels
            if not self.labels.is_workflow_reserved(label) and label != parent.agent_type
        ]

    def _post_decision(self, action: ResolveBlockAction) -> ActionResult | None:
        """The decision on the item, create-once, BEFORE the discharge: the
        item is never unblocked without it. It carries no cause marker."""
        return self._comment_once(
            action, decision_marker(action.decision_id), _decision_comment(action),
            f"tech lead {action.decision_id}: the resolution of #{action.issue_number}'s block",
        )

    def _record_ruling(self, action: ResolveBlockAction) -> ActionResult | None:
        """The decision as a standing ruling in the item's body (create-once), before
        the discharge: the item is never unblocked without it."""
        ruling = self._resolution_ruling(action)
        if ruling is None:
            return None
        try:
            self.rulings.record(action.issue_number, ruling)
        except (ReconciliationRequired, ClaimLostError):
            raise
        except Exception as error:  # the item stays blocked; a replay records it
            return ActionResult.fail_limited(action, str(error), host_rate_limit_of(error),
                                             issue_number=action.issue_number, proposal_id=action.proposal_id)
        return None

    def _resolution_ruling(self, action: ResolveBlockAction) -> "StandingRuling | None":
        """The ruling the decision records on its item. A lift decides no design
        and a closing split leaves nothing to build, so neither records one."""
        resolution = action.resolution
        binds = resolution.kind is ResolutionKind.ANSWER or (
            resolution.kind is ResolutionKind.SPLIT and resolution.parent is ParentDisposition.NARROW
        )
        if not binds:
            return None
        approved = (f", approved on proposal #{action.proposal_issue_number}"
                    if action.proposal_issue_number else ", under the operator's resolve_block: execute")
        return self.rulings.ruling(
            ruling_id=resolution_ruling_id(action.decision_id),
            text=f"## {resolution.title}\n\n{resolution.body}",
            authority=RulingAuthority.APPROVED_RESOLUTION,
            source=f"tech-lead resolve_block {resolution.kind.value} {action.decision_id}{approved}",
            scope=RulingScope(),
        )

    def _steps(self, action: ResolveBlockAction) -> DecisionStepContext:
        """The decision as its steps see it, with the ruling it records on the item (#8691)."""
        ruling = self._resolution_ruling(action)
        return replace(_step_context(action), subject_rulings=() if ruling is None else (ruling,))

    def _post_discharge(self, action: ResolveBlockAction) -> ActionResult | None:
        """The durable record of what was discharged, once it COMMITTED."""
        return self._comment_once(
            action, discharge_marker(action.decision_id), _discharge_comment(action),
            f"tech lead {action.decision_id}: record the discharge on #{action.issue_number}",
        )

    def _apply_steps(self, action: ResolveBlockAction, *, after_release: bool) -> ActionResult | None:
        """One phase of the decision's steps (#8691); the failure, else None.
        A step refused by a race after the check is recorded on the proposal."""
        applied = self.steps.apply(_step_context(action), action.follow_through, after_release=after_release)
        return None if applied.failed is None else ActionResult.fail(action, applied.failed)

    def _comment_once(
        self, action: ResolveBlockAction, marker: str, comment: str, reason: str
    ) -> ActionResult | None:
        """Post *comment* unless *marker* is already on the item; the failed write, else None."""
        number = action.issue_number
        if any(marker in body for body in self.read_comment_bodies(number)):
            return None
        posted = self.apply_action(AddCommentAction(
            number=number, comment=comment, reason=reason, expected=build_expected_for_mutation(),
        ))
        return None if posted.success else posted

    def _gate_published_review(self, action: ResolveBlockAction, issue: "Issue") -> ActionResult | None:
        """pr-pending on first when an open PR carries the item's published work."""
        gate = self.labels.pr_pending
        gated = any(name.casefold() == gate.casefold() for name in issue.labels)
        held = bool(self.published_review.holds(issue.number))
        if gated or not held:
            return None
        written = self.apply_action(AddLabelAction(
            issue_number=issue.number, label=gate,
            reason=f"tech lead {action.decision_id}: its PR's review owns #{issue.number}",
            expected=build_expected_for_mutation(),
        ))
        return None if written.success else written


def _predecessor(child: ResolutionChild, parent: "Issue", earlier: tuple[int, ...]) -> int | None:
    """The issue a child waits on: the parent, an earlier child, or none."""
    if child.after is None:
        return None
    return parent.number if child.after == PARENT else earlier[int(child.after) - 1]


def _step_context(action: ResolveBlockAction) -> DecisionStepContext:
    return DecisionStepContext(
        action=action, subject=action.issue_number, proposal_issue_number=action.proposal_issue_number,
        anchor_issue_number=action.anchor_issue_number, proposal_id=action.proposal_id,
        finding_ids=action.finding_ids,
    )


def _decision_comment(action: ResolveBlockAction) -> str:
    resolution = action.resolution
    number = action.issue_number
    evidence = "\n".join(f"- {item}" for item in resolution.evidence)
    split = ""
    if resolution.children:
        children = "\n".join(f"- {child.title}" for child in resolution.children)
        rest = (f"#{number} closes: its children carry all of it."
                if resolution.parent is ParentDisposition.CLOSE
                else f"#{number} keeps the slice it has and is requeued to finish it.")
        split = f"\n\n### Split\n\nFiled as their own issues:\n{children}\n\n{rest}"
    return (
        f"## Tech lead resolved this block ({resolution.kind.value}): {resolution.title}\n\n"
        f"{resolution.body}\n\n### Evidence\n\n{evidence}{split}\n\n"
        f"The next session on #{number} works to this decision. If it is wrong, put"
        f" `needs-human` back: the tech lead never clears the same cause on #{number} again."
        f"\n\n{decision_marker(action.decision_id)}"
    )


def _discharge_comment(action: ResolveBlockAction) -> str:
    causes = sorted(action.resolution.causes, key=lambda cause: cause.value)
    markers = "\n".join(cause_marker(cause, action.decision_id) for cause in causes)
    return (
        f"Discharged by the tech lead's resolution {action.decision_id}:"
        f" {', '.join(cause.value for cause in causes)}.\n\n"
        f"{discharge_marker(action.decision_id)}\n{markers}"
    )
