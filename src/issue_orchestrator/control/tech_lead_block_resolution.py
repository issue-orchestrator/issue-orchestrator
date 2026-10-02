"""Apply-time owner of the tech lead's ``resolve_block`` (#7658).

A ``resolve_block`` decides a ``needs-human`` WORK block in the operator's
stead (see :mod:`..domain.block_resolution` for the vocabulary). The decision
is untrusted intent: immediately before any write this owner re-verifies, from
the owners that already answer each question, that it may stand:

1. **the item is open**, read fresh;
2. **nothing runs or claims it** - no live or unverifiable runtime owner, no
   claim but the proposing run's own, and no session ran on it since the tech
   lead observed it. Every new generation of a resolvable cause (an agent's
   question, the engine giving up) is a session's, so this is also what keeps a
   replay of an applied decision off a block raised after it;
3. **the block is a work block the tech lead may decide** - not the tech
   lead's own hand-over (its marker), and every cause the decision names is on
   record in the shared block's owner now;
4. **it was never resolved before** - a durable marker on the item records
   every cause a resolution discharged; one from an EARLIER decision means the
   block was put back after it, so that cause is the operator's from then on;
5. **no human-only work** is named in the item, the agent's question or the
   decision itself (:func:`~..domain.block_resolution.human_only_work`).

A failed check REFUSES the decision with a typed
:class:`BlockResolutionRefusal` and no write. Otherwise, in an order that keeps
the item blocked until everything its next session needs is on GitHub:

* a split's children are filed create-once (by body marker), each first
  without its agent label, its dependency line verified by the engine's own
  parser, and only then labelled for pickup (the issue-dependency-stacking
  contract). Filing is ``create_issue``'s call: when the tech lead may not file
  unattended, each child is filed behind the ``proposed-tech-lead`` gate;
* the decision is posted on the item, create-once, carrying one marker per
  cause it discharges;
* the pr-pending gate goes on first when an open PR carries the item's
  published validated work, so no coder relaunches over that PR;
* the named causes are discharged through
  :meth:`~.needs_human_block.SharedNeedsHumanBlock.resolve`: other causes keep
  the label;
* a split that closes its parent closes it; otherwise the item is requeued if
  nothing else blocks it.

The discharge is bracketed write-ahead (``ports/block_resolution_discharges``):
a replay of a decision whose discharge committed only finishes (requeue or
close) and never discharges again, so it cannot clear a block raised after
it; one interrupted mid-discharge hands the item back to the operator; one
that never began decides afresh, finding its earlier writes by their markers.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from ..domain.block_resolution import (
    PARENT,
    ParentDisposition,
    ResolutionChild,
    child_marker,
    cause_marker,
    decision_marker,
    human_only_work,
    prior_resolutions,
)
from ..domain.dependencies import DependencyMode, parse_dependency_edges
from ..domain.host_rate_limit import rate_limit_cause
from ..domain.human_block import BlockOutcome, NeedsHumanCause
from ..domain.operator_decision_retry import DecisionRetryState
from ..domain.tech_lead_session import PROPOSED_TECH_LEAD_LABEL
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
from .tech_lead_reset_retry import STALE_DOWNGRADE_MODE, publish_proposal_surfaced

if TYPE_CHECKING:
    from ..domain.models import SessionHistoryEntry
    from ..ports.issue import Issue
    from .issue_work_claims import IssueWorkClaim
    from .label_manager import LabelManager
    from ..ports.block_resolution_discharges import BlockResolutionDischarges
    from .needs_human_block import SharedNeedsHumanBlock
    from .published_review_custody import PublishedReviewHolds
    from .review_exchange_lifecycle import IssueRuntimeActivity

logger = logging.getLogger(__name__)

OP_TYPE = ResolveBlockAction.op_type
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


@dataclass(frozen=True, slots=True)
class RefusedResolution:
    code: BlockResolutionRefusal
    detail: str

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
        refusal = self._block_refusal(action, issue, priors)
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
        blocked = self.labels.needs_human.casefold() in folded
        if not blocked:
            return RefusedResolution(BlockResolutionRefusal.NOT_BLOCKED,
                                     f"#{number} no longer carries {self.labels.needs_human}")
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
        if prior is DecisionRetryState.COMMITTED:
            return self._finish(action)
        if prior is DecisionRetryState.BEGUN:
            return self._refuse(action, RefusedResolution(
                BlockResolutionRefusal.INTERRUPTED,
                f"the engine stopped while {action.decision_id} discharged #{action.issue_number}'s"
                " causes; whatever stands on it now may have been raised since, so it is the"
                " operator's",
            ))
        verdict = self.verify(action)
        if isinstance(verdict, RefusedResolution):
            return self._refuse(action, verdict)
        issue = verdict.issue
        try:
            children = self._file_children(action, issue)
        except (ReconciliationRequired, ClaimLostError):
            raise
        except Exception as error:  # the item stays blocked; a replay resumes by marker
            return ActionResult.fail_limited(
                action, f"split child not filed: {error}", host_rate_limit_of(error),
                issue_number=action.issue_number, proposal_id=action.proposal_id)
        for what, step in (
            ("decision not posted", lambda: self._post_decision(action, children)),
            (f"{self.labels.pr_pending} not put on", lambda: self._gate_published_review(action, issue)),
        ):
            failed = step()
            if failed is not None:
                return ActionResult.fail_limited(
                    action, f"{what} #{action.issue_number}: {failed.error}", failed.host_rate_limit,
                    issue_number=action.issue_number, proposal_id=action.proposal_id)
        self.discharges.begin_block_resolution(decision_id=action.decision_id)
        outcome = self.block.resolve(
            action.issue_number, action.resolution.causes,
            f"tech lead {action.decision_id} resolved: {action.resolution.title}",
        )
        if outcome not in (BlockOutcome.CLEARED, BlockOutcome.HELD_BY_ANOTHER_CAUSE):
            # Nothing was discharged (the owner withdraws a cause only on a
            # committed outcome): a replay decides afresh.
            self.discharges.abandon_block_resolution(decision_id=action.decision_id)
            return ActionResult.fail(
                action, f"needs-human on #{action.issue_number} did not settle ({outcome.value})",
                issue_number=action.issue_number, proposal_id=action.proposal_id)
        self.discharges.commit_block_resolution(decision_id=action.decision_id)
        return self._progress(action, children=children, outcome=outcome)

    def _finish(self, action: ResolveBlockAction) -> ActionResult:
        """The discharge committed before: never discharge again, only move the item."""
        issue = self.read_issue(action.issue_number)
        if issue is None or issue.state != "open":
            return self._applied(action, children=(), outcome=BlockOutcome.CLEARED, still_blocked=())
        held = any(label.casefold() == self.labels.needs_human.casefold() for label in issue.labels)
        outcome = BlockOutcome.HELD_BY_ANOTHER_CAUSE if held else BlockOutcome.CLEARED
        return self._progress(action, children=(), outcome=outcome)

    def _progress(
        self, action: ResolveBlockAction, *, children: tuple[int, ...], outcome: BlockOutcome
    ) -> ActionResult:
        """Close a fully split item, or requeue it, once its block is gone."""
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
        """File one child create-once, wire its edge, THEN label it for pickup."""
        marker = child_marker(action.decision_id, index)
        number = self.find_issue_by_marker(title=child.title, marker=marker, authoritative=True)
        predecessor = (
            None if child.after is None
            else parent.number if child.after == PARENT
            else earlier[int(child.after) - 1]
        )
        if number is None:
            self.require_authority(action, action.issue_number)
            edge = (
                f"\n\n{child.edge.directive}: #{predecessor}\n"
                if child.edge is not None and predecessor is not None
                else "\n"
            )
            gate = (PROPOSED_TECH_LEAD_LABEL,) if action.children_gated else ()
            created = self.create_issue(
                title=child.title,
                body=(
                    f"{child.body}{edge}\nRefs #{parent.number} — split out by the tech lead's"
                    f" resolution {action.decision_id}.\n{marker}"
                ),
                labels=[*self._inherited_labels(parent), *gate],
                milestone=parent.milestone_number,
            )
            if not created or not isinstance(created.get("number"), int):
                raise RuntimeError(f"child {index} of #{parent.number}'s split was not created")
            number = int(created["number"])
        self._verify_edge(number, child, predecessor)
        for label in (parent.agent_type,) if parent.agent_type else ():
            marked = self.apply_action(AddLabelAction(
                issue_number=number, label=label,
                reason=f"tech lead {action.decision_id}: child {index} wired, ready for pickup",
                expected=build_expected_for_mutation(),
            ))
            if not marked.success:
                raise RuntimeError(
                    f"child #{number} not labelled {label}: {marked.error}"
                ) from rate_limit_cause(marked.host_rate_limit)
        return number

    def _verify_edge(self, number: int, child: ResolutionChild, predecessor: int | None) -> None:
        """The engine's own parser must see the edge before the child is runnable."""
        if child.edge is None:
            return
        fresh = self.read_issue(number)
        mode = DependencyMode.NORMAL if child.edge.directive == "Depends-on" else DependencyMode.STACK
        edges = parse_dependency_edges(fresh.body or "") if fresh is not None else []
        if not any(edge.issue_number == predecessor and edge.mode is mode for edge in edges):
            raise RuntimeError(
                f"child #{number}'s {child.edge.directive}: #{predecessor} is not visible to the"
                " dependency parser; it is left without its agent label"
            )

    def _inherited_labels(self, parent: "Issue") -> list[str]:
        """The parent's own labels (priority, area), never workflow state or its agent."""
        return [
            label for label in parent.labels
            if not self.labels.is_workflow_reserved(label) and label != parent.agent_type
        ]

    def _post_decision(
        self, action: ResolveBlockAction, children: tuple[int, ...]
    ) -> ActionResult | None:
        """The decision on the item, create-once; the failed write, else None."""
        number = action.issue_number
        if any(decision_marker(action.decision_id) in body for body in self.read_comment_bodies(number)):
            return None
        posted = self.apply_action(AddCommentAction(
            number=number,
            comment=_decision_comment(action, children),
            reason=f"tech lead {action.decision_id}: the resolution of #{number}'s block",
            expected=build_expected_for_mutation(),
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


def _decision_comment(action: ResolveBlockAction, children: tuple[int, ...]) -> str:
    resolution = action.resolution
    number = action.issue_number
    evidence = "\n".join(f"- {item}" for item in resolution.evidence)
    filed = (
        "\n\n### Split\n\n" + "\n".join(f"- #{child}" for child in children)
        + (f"\n\n#{number} closes: its children carry all of it."
           if resolution.parent is ParentDisposition.CLOSE
           else f"\n\n#{number} keeps the slice it has and is requeued to finish it.")
        if children else ""
    )
    causes = ", ".join(sorted(cause.value for cause in resolution.causes))
    markers = "\n".join(cause_marker(cause, action.decision_id) for cause in sorted(
        resolution.causes, key=lambda cause: cause.value))
    return (
        f"## Tech lead resolved this block ({resolution.kind.value}): {resolution.title}\n\n"
        f"{resolution.body}\n\n### Evidence\n\n{evidence}{filed}\n\n"
        f"Discharged: {causes}. The next session on #{number} works to this decision."
        f" If it is wrong, put `needs-human` back: the tech lead never clears these"
        f" causes on #{number} again.\n\n{decision_marker(action.decision_id)}\n{markers}"
    )
