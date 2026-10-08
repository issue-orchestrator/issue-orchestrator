"""The tech lead's ``resolve_block`` owner (#7658).

Driven through its injected owner seams over the REAL shared-block owner and a
real cause store, so "only the cause it resolved comes off" is the production
rule, not a fake's. Each porchpin shape is here: #262 (an agent's split
question on an item the stuck sweep also gave up on), #326 (a stale block the
engine gave up on), #364 (an agent question beside its published PR) and #179
(provisioning, which must still be handed over).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

from tests.unit.control.decision_steps_fakes import unused_decision_steps
from issue_orchestrator.control.tech_lead_decision_steps import DecisionStepsOwner
from issue_orchestrator.domain.decision_steps import DecisionFollowThrough, DecisionStep, DecisionStepKind
from issue_orchestrator.control.standing_rulings import StandingRulingsOwner
from issue_orchestrator.domain.standing_ruling import (
    RulingAuthority,
    StandingRuling,
    parse_rulings_block,
    resolution_ruling_id,
)
from tests.standing_ruling_helpers import InMemoryStandingRulingsIndex
from issue_orchestrator.control.action_results import ActionResult
from issue_orchestrator.control.actions import (
    Action,
    AddCommentAction,
    AddLabelAction,
    CloseIssueAction,
    ResolveBlockAction,
)
from issue_orchestrator.control.human_gates import HumanGates
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.needs_human_block import (
    ResolutionOutcome,
    BlockOutcome,
    HumanBlockRequest,
    NeedsHumanBlock,
    NeedsHumanCause,
)
from issue_orchestrator.control.published_review_custody import PublishedReviewHold
from issue_orchestrator.control.review_exchange_lifecycle import (
    IssueRuntimeActivity,
    IssueRuntimeOwnerKind,
)
from issue_orchestrator.control.tech_lead_block_resolution import (
    BlockResolutionRefusal,
    TechLeadBlockResolutionExecutor,
)
from issue_orchestrator.domain.block_resolution import (
    BlockResolution,
    ChildEdge,
    ParentDisposition,
    ResolutionChild,
    ResolutionKind,
    cause_marker,
    prior_resolutions,
)
from issue_orchestrator.domain.models import Issue, SessionHistoryEntry
from issue_orchestrator.events import EventName
from issue_orchestrator.execution.pending_work_claim_store import SqlitePendingWorkClaimStore
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.event_sink import TraceEvent
from issue_orchestrator.ports.tech_lead_authority import InMemoryTechLeadAuthorityStore

ITEM = 262
OBSERVED = "2026-10-02T06:00:00+00:00"
AGENT = "agent:backend"
SPLIT_QUESTION = (
    "#262 is more than one session. Should I split it: land this branch under"
    " 'Refs #262' and move the live index into its own issue?"
)

_AGENT, _SWEEP = NeedsHumanCause.AGENT_COMPLETION, NeedsHumanCause.SESSION_LIFECYCLE


@dataclass
class GitHub:
    """Labels, bodies and comments as GitHub holds them, per issue."""

    labels: dict[int, set[str]] = field(default_factory=dict)
    bodies: dict[int, str] = field(default_factory=dict)
    titles: dict[int, str] = field(default_factory=dict)
    states: dict[int, str] = field(default_factory=dict)
    comments: dict[int, list[str]] = field(default_factory=dict)
    created: list[dict[str, Any]] = field(default_factory=list)
    next_number: int = 900
    #: GitHub keeps a body the dependency parser cannot read (a mangled line).
    mangle: bool = False
    #: GitHub's search index has not caught up with issues created just now.
    search_lags: bool = False
    unindexed: set[int] = field(default_factory=set)

    def read(self, number: int) -> list[str]:
        return sorted(self.labels.get(number, set()))

    def add_label(self, number: int, label: str) -> None:
        self.labels.setdefault(number, set()).add(label)

    def remove_label(self, number: int, label: str) -> None:
        self.labels.setdefault(number, set()).discard(label)

    def issue(self, number: int) -> Issue | None:
        if number not in self.labels:
            return None
        return Issue(
            number=number, title=self.titles.get(number, f"Issue {number}"),
            labels=self.read(number), state=self.states.get(number, "open"),
            body=self.bodies.get(number, ""), milestone_number=3,
        )

    def create_issue(self, *, title: str, body: str, labels: list[str], milestone: int | None) -> dict[str, Any]:
        number = self.next_number
        self.next_number += 1
        self.labels[number] = set(labels)
        self.bodies[number] = body.replace("Depends-on: #", "Depends on #") if self.mangle else body
        self.titles[number] = title
        if self.search_lags:
            self.unindexed.add(number)
        self.created.append({"number": number, "title": title, "body": body, "labels": list(labels),
                             "milestone": milestone})
        return {"number": number}

    def find_by_marker(self, *, title: str, marker: str, authoritative: bool) -> int | None:
        assert authoritative
        return next(
            (n for n, body in self.bodies.items() if marker in body and n not in self.unindexed), None
        )


@dataclass
class Applier:
    """The applier's dispatch for the owner's ordinary writes."""

    github: GitHub
    applied: list[Action] = field(default_factory=list)
    fail: type[Action] | None = None

    def apply(self, action: Action) -> ActionResult:
        self.applied.append(action)
        if self.fail is not None and isinstance(action, self.fail):
            return ActionResult.fail(action, "GitHub said no")
        if isinstance(action, AddCommentAction):
            self.github.comments.setdefault(action.number, []).append(action.comment)
        elif isinstance(action, AddLabelAction):
            self.github.add_label(action.issue_number, action.label)
        elif isinstance(action, CloseIssueAction):
            self.github.states[action.issue_number] = "closed"
        return ActionResult.ok(action)


@dataclass
class World:
    tmp: Path
    github: GitHub = field(default_factory=GitHub)
    question: str | None = SPLIT_QUESTION
    activity: IssueRuntimeActivity = field(
        default_factory=lambda: IssueRuntimeActivity(frozenset(), frozenset())
    )
    holds: tuple[PublishedReviewHold, ...] = ()
    history: list[SessionHistoryEntry] = field(default_factory=list)
    requeued: list[int] = field(default_factory=list)
    indexed: list[int] = field(default_factory=list)
    events: list[TraceEvent] = field(default_factory=list)
    #: GitHub refuses the body write that records the standing ruling (#8141).
    body_write_fails: bool = False
    bodies_written: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.store = SqlitePendingWorkClaimStore(self.tmp / "causes.sqlite")
        self.block = NeedsHumanBlock(
            "needs-human", "tech-lead-needs-human", self.github, self.github.read,
            frozenset, self.store,
        )
        self.applier = Applier(self.github)
        self.discharges = InMemoryTechLeadAuthorityStore()

    def blocked_by(self, number: int, *causes: NeedsHumanCause, labels: tuple[str, ...] = (AGENT,)) -> None:
        self.github.labels.setdefault(number, set()).update(labels)
        for cause in causes:
            assert self.block.acquire(HumanBlockRequest(number, cause, "planted")) is BlockOutcome.HELD

    def write_body(self, number: int, body: str) -> None:
        if self.body_write_fails:
            raise RuntimeError("GitHub kept the old body")
        self.bodies_written.append(number)
        self.github.bodies[number] = body

    def executor(self, steps: DecisionStepsOwner | None = None) -> TechLeadBlockResolutionExecutor:
        labels = LabelManager(Config())

        def requeue(number: int) -> tuple[str, ...]:
            self.requeued.append(number)
            return tuple(labels.get_blocking(self.github.read(number)))

        return TechLeadBlockResolutionExecutor(
            events=_Sink(self.events),
            labels=labels,
            block=self.block,
            gates=HumanGates.over(self.block, labels),
            read_issue=self.github.issue,
            read_comment_bodies=lambda number: list(self.github.comments.get(number, [])),
            agent_questions=lambda number: (self.question,) if self.question else (),
            runtime_activity=lambda number: self.activity,
            claims_on_issue=lambda number: (),
            sessions_not_before=lambda number, instant: tuple(
                entry for entry in self.history
                if entry.completed_at is not None and entry.completed_at >= instant
            ),
            published_review=_Holds(self.holds),
            find_issue_by_marker=self.github.find_by_marker,
            create_issue=self.github.create_issue,
            apply_action=self.applier.apply,
            require_authority=lambda action, number: None,
            requeue=requeue,
            discharges=self.discharges,
            index_proposals=self.indexed.extend,
            rulings=StandingRulingsOwner(
                read_issue=self.github.issue, write_body=self.write_body, index=InMemoryStandingRulingsIndex(),
            ),
            steps=steps or unused_decision_steps(),
        )


@dataclass
class _Sink:
    events: list[TraceEvent]

    def publish(self, event: TraceEvent) -> None:
        self.events.append(event)


@dataclass
class _Holds:
    items: tuple[PublishedReviewHold, ...]

    def holds(self, issue_number: int) -> tuple[PublishedReviewHold, ...]:
        return tuple(hold for hold in self.items if hold.issue_number == issue_number)


def _resolution(kind: ResolutionKind = ResolutionKind.ANSWER, *causes: NeedsHumanCause, **extra: Any) -> BlockResolution:
    return BlockResolution(
        kind=kind,
        causes=frozenset(causes or (_AGENT,)),
        title=extra.pop("title", "Land the slice; the index is its own issue"),
        body=extra.pop("body", "The spec's acceptance list has two independent halves."),
        evidence=extra.pop("evidence", ("docs/spec.md#S1", "ADR-0011")),
        **extra,
    )


def _action(resolution: BlockResolution, *, number: int = ITEM, action_id: str = "A1",
            run: str = "run-1", children_gated: bool = False) -> ResolveBlockAction:
    return ResolveBlockAction(
        issue_number=number, resolution=resolution, rationale="decided from the spec",
        proposal_id=action_id, anchor_issue_number=1, observed_at=OBSERVED,
        source_session_name="tech-lead-1", source_run_id=run, children_gated=children_gated,
    )


def _split() -> BlockResolution:
    return _resolution(
        ResolutionKind.SPLIT, _AGENT, _SWEEP,
        children=(ResolutionChild(
            title="Live D1 seller pickup index", body="The rest of #262's acceptance list.",
            edge=ChildEdge.DEPENDS_ON, after="parent",
        ),),
        parent=ParentDisposition.NARROW,
    )


# -- the porchpin shapes ------------------------------------------------------


def test_262_split_files_the_child_wired_then_labelled_and_clears_both_work_causes(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT, _SWEEP, labels=(AGENT, "priority:high", "v1"))

    result = world.executor().apply(_action(_split()))

    assert result.success, result.error
    [child] = world.github.created
    assert "Depends-on: #262" in child["body"].splitlines()
    assert child["labels"] == ["priority:high", "v1"], "created WITHOUT its agent label"
    assert child["milestone"] == 3
    # The agent label goes on only after the edge was written (and verified).
    assert AGENT in world.github.labels[child["number"]]
    decision, discharge = world.github.comments[ITEM]
    assert "Live D1 seller pickup index" in decision
    assert prior_resolutions([decision]) == {}, "no cause marker before the discharge"
    assert set(prior_resolutions([discharge])) == {_AGENT, _SWEEP}
    assert "needs-human" not in world.github.labels[ITEM]
    assert world.store.needs_human_causes(ITEM) == frozenset()
    assert world.requeued == [ITEM]
    assert result.details["still_blocked_by"] == []
    [executed] = [e for e in world.events if e.event_type is EventName.TECH_LEAD_ACTION_EXECUTED]
    assert executed.data["boundary"]["causes"] == ["agent_completion", "session_lifecycle"]


def test_326_lift_of_a_stale_block_clears_only_the_engine_giving_up(tmp_path: Path) -> None:
    world = World(tmp_path, question=None)
    world.blocked_by(326, _SWEEP, labels=(AGENT, "blocked-cross-milestone"))

    result = world.executor().apply(_action(
        _resolution(ResolutionKind.LIFT, _SWEEP, title="The cross-milestone block is gone",
                    evidence=("blocked-item-triage.json: blocked-cross-milestone removed",)),
        number=326,
    ))

    assert result.success, result.error
    assert "needs-human" not in world.github.labels[326]
    # Requeued from fresh labels: the stale cross-milestone label is its own
    # owner's to reconcile once the planner sees the item again (#7333).
    assert world.requeued == [326]
    assert result.details["still_blocked_by"] == ["blocked-cross-milestone"]
    assert "blocked-cross-milestone" in world.github.labels[326], "never this owner's to remove"


def test_364_answer_beside_published_work_gates_the_pr_first(tmp_path: Path) -> None:
    world = World(tmp_path, question="A1 still needs the maintainer: hold or rule it unholdable?")
    world.blocked_by(364, _AGENT)
    world.holds = (PublishedReviewHold(364, 379, "364-branch", "r1", "a" * 40),)

    result = world.executor().apply(_action(
        _resolution(title="Rule it unholdable", body="ADR-0010 already rules it."), number=364,
    ))

    assert result.success, result.error
    labels = [a for a in world.applier.applied if isinstance(a, AddLabelAction)]
    assert [a.label for a in labels] == ["pr-pending"]
    assert world.github.labels[364] == {AGENT, "pr-pending"}
    # Eligible again exactly as the operator's retry leaves it: pr-pending is
    # the scheduler's gate that keeps a coder off the PR's branch.
    assert world.requeued == [364]


def test_179_provisioning_is_never_resolved(tmp_path: Path) -> None:
    world = World(tmp_path, question=(
        "The cloud-test deploy needs a Cloudflare account and an API token with"
        " Workers permissions; I cannot provision those."
    ))
    world.blocked_by(179, _AGENT, _SWEEP)

    result = world.executor().apply(_action(
        _resolution(ResolutionKind.LIFT, _AGENT, _SWEEP, title="Lift it"), number=179,
    ))

    assert result.details["refusal"] == BlockResolutionRefusal.HUMAN_ONLY_WORK.value
    assert world.github.labels[179] == {AGENT, "needs-human"}
    assert world.applier.applied == [] and world.github.created == []


def test_human_only_work_named_in_the_item_body_also_refuses(tmp_path: Path) -> None:
    world = World(tmp_path, question=None)
    world.blocked_by(179, _SWEEP)
    world.github.bodies[179] = "> **Provisioning checklist (human; each is an account action)**"

    result = world.executor().apply(_action(_resolution(ResolutionKind.LIFT, _SWEEP), number=179))

    assert result.details["refusal"] == BlockResolutionRefusal.HUMAN_ONLY_WORK.value
    assert "needs-human" in world.github.labels[179]


# -- only the cause it resolved ----------------------------------------------


def test_another_cause_keeps_the_label(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT, NeedsHumanCause.ACTION_LIVENESS)

    result = world.executor().apply(_action(_resolution()))

    assert result.success, result.error
    assert result.details["block"] == BlockOutcome.HELD_BY_ANOTHER_CAUSE.value
    assert "needs-human" in world.github.labels[ITEM]
    assert world.store.needs_human_causes(ITEM) == frozenset({"action_liveness"})
    assert world.requeued == []


def test_a_cause_not_on_record_is_refused_and_an_operator_label_never_cleared(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.github.labels[ITEM] = {AGENT, "needs-human"}  # the operator's own label

    result = world.executor().apply(_action(_resolution()))

    assert result.details["refusal"] == BlockResolutionRefusal.CAUSE_NOT_RECORDED.value
    assert "needs-human" in world.github.labels[ITEM]
    assert world.applier.applied == []


def test_the_tech_leads_own_hand_over_is_never_resolved(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT, labels=(AGENT, "tech-lead-needs-human"))

    result = world.executor().apply(_action(_resolution()))

    assert result.details["refusal"] == BlockResolutionRefusal.TECH_LEAD_HAND_OVER.value
    assert "needs-human" in world.github.labels[ITEM]


def test_owner_refuses_to_resolve_a_cause_that_is_not_a_work_block(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.blocked_by(ITEM, NeedsHumanCause.MERGE_ESCALATION)
    with pytest.raises(ValueError, match="work-block"):
        world.block.resolve(ITEM, frozenset({NeedsHumanCause.MERGE_ESCALATION}), "no")
    assert "needs-human" in world.github.labels[ITEM]


# -- reversibility -------------------------------------------------------------


def test_a_block_put_back_after_a_resolve_is_never_resolved_again(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT)
    assert world.executor().apply(_action(_resolution())).success
    # The agent (or the operator) puts the block back on.
    world.blocked_by(ITEM, _AGENT)

    again = world.executor().apply(_action(_resolution(title="Another answer"), action_id="A2", run="run-2"))

    assert again.details["refusal"] == BlockResolutionRefusal.RESOLVED_BEFORE.value
    assert "needs-human" in world.github.labels[ITEM]


def _committed(world: World, *causes: NeedsHumanCause, decision: str = "run-1/A1") -> None:
    world.discharges.begin_block_resolution(
        decision_id=decision, issue_number=ITEM, causes=frozenset(cause.value for cause in causes))
    world.discharges.commit_block_resolution(decision_id=decision)


def test_a_replay_after_the_discharge_committed_only_finishes(tmp_path: Path) -> None:
    """The requeue failed after the discharge committed; the replay finishes it."""
    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT)
    world.github.comments[ITEM] = [cause_marker(_AGENT, "run-1/A1")]
    assert world.block.resolve(ITEM, frozenset({_AGENT}), "earlier attempt").outcome is BlockOutcome.CLEARED
    _committed(world, _AGENT)

    replay = world.executor().apply(_action(_resolution()))

    assert replay.success, replay.error
    assert world.requeued == [ITEM]


def test_a_replay_never_clears_a_label_the_operator_put_back(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT)
    world.github.comments[ITEM] = [cause_marker(_AGENT, "run-1/A1")]
    world.block.resolve(ITEM, frozenset({_AGENT}), "earlier attempt")
    _committed(world, _AGENT)
    world.github.add_label(ITEM, "needs-human")  # the operator, after the resolve

    replay = world.executor().apply(_action(_resolution()))

    assert "needs-human" in world.github.labels[ITEM]
    assert replay.details.get("block") == BlockOutcome.HELD_BY_ANOTHER_CAUSE.value
    assert world.requeued == []


def test_a_replay_never_discharges_a_block_raised_after_the_first_discharge(tmp_path: Path) -> None:
    """r2 F2: A1 lifted session_lifecycle, then failed finishing; the stuck
    sweep gives up on the item again (a NEW session_lifecycle, no session
    ran). The replay of A1 must leave the new block alone."""
    world = World(tmp_path, question=None)
    world.blocked_by(ITEM, _SWEEP)
    lift = _resolution(ResolutionKind.LIFT, _SWEEP)
    assert world.executor().apply(_action(lift)).success
    world.blocked_by(ITEM, _SWEEP)  # the sweep's exhaustion escalation

    replay = world.executor().apply(_action(lift))

    assert replay.success, replay.error
    assert "needs-human" in world.github.labels[ITEM]
    assert world.store.needs_human_causes(ITEM) == frozenset({"session_lifecycle"})


def test_an_interrupted_discharge_is_handed_back_untouched(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT)
    world.discharges.begin_block_resolution(
        decision_id="run-1/A1", issue_number=ITEM, causes=frozenset({"agent_completion"}))

    result = world.executor().apply(_action(_resolution()))

    assert result.details["refusal"] == BlockResolutionRefusal.INTERRUPTED.value
    assert "needs-human" in world.github.labels[ITEM]
    assert world.applier.applied == []


def test_a_failed_discharge_is_abandoned_so_a_replay_decides_afresh(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT)
    executor = world.executor()
    object.__setattr__(executor, "block", _RacedBlock(
        world.block, lambda t, c, r: ResolutionOutcome(BlockOutcome.FAILED, mutation_attempted=False)))

    assert not executor.apply(_action(_resolution())).success
    assert world.discharges.block_resolution_state(decision_id="run-1/A1") is None

    again = world.executor().apply(_action(_resolution()))

    assert again.success, again.error
    assert "needs-human" not in world.github.labels[ITEM]


def test_any_question_the_agent_ever_asked_is_screened(tmp_path: Path) -> None:
    """r2 F3: a credential request is not screened out by a later, benign
    question or by the events after it."""
    world = World(tmp_path, question=None)
    world.blocked_by(ITEM, _AGENT)
    executor = world.executor()
    object.__setattr__(executor, "agent_questions", lambda number: (
        "Please add the CLOUDFLARE_API_TOKEN secret; I cannot create it.",
        "Is the share page done?",
    ))

    result = executor.apply(_action(_resolution()))

    assert result.details["refusal"] == BlockResolutionRefusal.HUMAN_ONLY_WORK.value


# -- nothing runs, nothing newer ---------------------------------------------


def test_a_live_session_refuses(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT)
    world.activity = IssueRuntimeActivity(frozenset({IssueRuntimeOwnerKind.SESSIONS}), frozenset())

    result = world.executor().apply(_action(_resolution()))

    assert result.details["refusal"] == BlockResolutionRefusal.LIVE_SESSION.value


def test_a_session_newer_than_the_observation_refuses(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT)
    world.history = [SessionHistoryEntry(
        issue_number=ITEM, title="t", agent_type=AGENT, status="failed", runtime_minutes=1,  # type: ignore[arg-type]
        completed_at=datetime(2026, 10, 2, 7, tzinfo=timezone.utc),
    )]

    result = world.executor().apply(_action(_resolution()))

    assert result.details["refusal"] == BlockResolutionRefusal.NEWER_SESSION.value


def test_a_closing_split_closes_the_parent(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT)
    resolution = _resolution(
        ResolutionKind.SPLIT, _AGENT,
        children=(
            ResolutionChild(title="First half", body="One."),
            ResolutionChild(title="Second half", body="Two.", edge=ChildEdge.STACK_AFTER, after=1),
        ),
        parent=ParentDisposition.CLOSE,
    )

    result = world.executor().apply(_action(resolution))

    assert result.success, result.error
    first, second = world.github.created
    assert f"Stack-after: #{first['number']}" in second["body"].splitlines()
    assert world.github.states[ITEM] == "closed"


def test_a_failed_decision_comment_leaves_the_block_in_place(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT)
    world.applier.fail = AddCommentAction

    result = world.executor().apply(_action(_resolution()))

    assert not result.success
    assert "needs-human" in world.github.labels[ITEM]
    assert world.store.needs_human_causes(ITEM) == frozenset({"agent_completion"})


def test_a_child_whose_edge_the_parser_cannot_see_is_never_made_runnable(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT, _SWEEP)
    world.github.mangle = True

    result = world.executor().apply(_action(_split()))

    assert not result.success and "not the decided" in (result.error or "")
    [child] = world.github.created
    assert AGENT not in world.github.labels[child["number"]], "left without its agent label"
    assert "needs-human" in world.github.labels[ITEM]


def _newer_session(world: World, status: str = "completed") -> None:
    world.history.append(SessionHistoryEntry(
        issue_number=ITEM, title="t", agent_type=AGENT, status=status, runtime_minutes=1,  # type: ignore[arg-type]
        completed_at=datetime(2026, 10, 2, 7, tzinfo=timezone.utc),
    ))


def test_a_replay_of_an_applied_decision_never_clears_a_question_asked_after_it(tmp_path: Path) -> None:
    """r1 F2: A1 applied; the requeued agent publishes and asks again beside its
    new PR (a COMPLETED session, the pr-label route). A replay of A1 finds its
    own markers, but a session ran since it observed the item: refused."""
    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT)
    assert world.executor().apply(_action(_resolution())).success
    _newer_session(world, "completed")
    world.blocked_by(ITEM, _AGENT)

    replay = world.executor().apply(_action(_resolution()))

    # The discharge committed once; a replay only finishes and never discharges.
    assert replay.details["block"] == BlockOutcome.HELD_BY_ANOTHER_CAUSE.value
    assert "needs-human" in world.github.labels[ITEM]
    assert world.store.needs_human_causes(ITEM) == frozenset({"agent_completion"})


def test_a_new_decision_after_a_session_ran_since_its_observation_is_refused(tmp_path: Path) -> None:
    """A proposal approved after the item ran again is stale: the block it
    decided may not be the block the item carries now."""
    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT)
    _newer_session(world, "completed")

    result = world.executor().apply(_action(_resolution()))

    assert result.details["refusal"] == BlockResolutionRefusal.NEWER_SESSION.value
    assert "needs-human" in world.github.labels[ITEM]


def test_a_closing_split_is_refused_while_another_cause_holds_the_item(tmp_path: Path) -> None:
    """r1 F3: closing would bury the cause the split does not name."""
    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT, NeedsHumanCause.ACTION_LIVENESS)
    resolution = _resolution(
        ResolutionKind.SPLIT, _AGENT,
        children=(ResolutionChild(title="All of it", body="Everything."),),
        parent=ParentDisposition.CLOSE,
    )

    result = world.executor().apply(_action(resolution))

    assert result.details["refusal"] == BlockResolutionRefusal.CLOSE_WHILE_HELD.value
    assert world.github.states.get(ITEM, "open") == "open"
    assert world.applier.applied == [] and world.github.created == []


def test_a_closing_split_never_closes_an_item_another_cause_took_meanwhile(tmp_path: Path) -> None:
    """r1 F3: a holder that arrives after the check keeps the item open."""
    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT)
    executor = world.executor()
    resolve = world.block.resolve

    def raced(target, causes, reason):  # type: ignore[no-untyped-def]
        world.blocked_by(ITEM, NeedsHumanCause.ACTION_LIVENESS)
        return resolve(target, causes, reason)

    object.__setattr__(executor, "block", _RacedBlock(world.block, raced))
    resolution = _resolution(
        ResolutionKind.SPLIT, _AGENT,
        children=(ResolutionChild(title="All of it", body="Everything."),),
        parent=ParentDisposition.CLOSE,
    )

    result = executor.apply(_action(resolution))

    assert result.success, result.error
    assert result.details["block"] == BlockOutcome.HELD_BY_ANOTHER_CAUSE.value
    assert not any(isinstance(a, CloseIssueAction) for a in world.applier.applied)
    assert world.github.states.get(ITEM, "open") == "open"


@dataclass
class _RacedBlock:
    inner: Any
    racing_resolve: Any

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def resolve(self, target: int, causes: frozenset[NeedsHumanCause], reason: str) -> ResolutionOutcome:
        return self.racing_resolve(target, causes, reason)


def test_a_split_child_that_files_human_only_work_is_refused(tmp_path: Path) -> None:
    """r1 F4: the screen reads every word the decision files, children too."""
    world = World(tmp_path, question=None)
    world.blocked_by(ITEM, _AGENT)
    resolution = _resolution(
        ResolutionKind.SPLIT, _AGENT,
        children=(ResolutionChild(title="Deploy foundation",
                                  body="Create a Cloudflare account for the cloud-test Worker."),),
        parent=ParentDisposition.NARROW,
    )

    result = world.executor().apply(_action(resolution))

    assert result.details["refusal"] == BlockResolutionRefusal.HUMAN_ONLY_WORK.value
    assert world.github.created == [] and world.applier.applied == []


def test_children_are_filed_behind_the_gate_when_filing_needs_approval(tmp_path: Path) -> None:
    """Filing is create_issue's call: under its propose authority each child
    waits behind proposed-tech-lead, while the decision itself took effect."""
    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT, _SWEEP)

    result = world.executor().apply(_action(_split(), children_gated=True))

    assert result.success, result.error
    [child] = world.github.created
    # Filed as a proposal awaiting a maintainer's approval (#7763): its
    # labels, its body marker, and the approval owner's durable index.
    assert {"tech-lead-proposal", "awaiting-approval"} <= set(child["labels"])
    assert "<!-- issue-orchestrator:tech-lead-proposal -->" in child["body"]
    assert world.indexed == [child["number"]]
    assert "needs-human" not in world.github.labels[ITEM]


@dataclass
class _ConfirmFails:
    """GitHub removes the label, then the read confirming it fails once."""

    github: GitHub
    fail_next_read: bool = False
    #: An operator puts the label back right after the removal landed.
    operator_puts_it_back: bool = False

    def read(self, number: int) -> list[str]:
        if self.fail_next_read:
            self.fail_next_read = False
            raise OSError("read after removal timed out")
        return self.github.read(number)

    def add_label(self, number: int, label: str) -> None:
        self.github.add_label(number, label)

    def remove_label(self, number: int, label: str) -> None:
        self.github.remove_label(number, label)
        self.fail_next_read = True
        if self.operator_puts_it_back:
            self.github.add_label(number, label)


def test_a_failed_outcome_whose_removal_landed_is_never_decided_again(tmp_path: Path) -> None:
    """r3 F1: the label came off but the confirming read failed (FAILED). The
    discharge stays begun, so a replay after the sweep raised a NEW block is
    handed back instead of clearing it."""
    world = World(tmp_path, question=None)
    flaky = _ConfirmFails(world.github)
    world.block = NeedsHumanBlock(
        "needs-human", "tech-lead-needs-human", flaky, flaky.read, frozenset, world.store,
    )
    world.blocked_by(ITEM, _SWEEP)
    lift = _resolution(ResolutionKind.LIFT, _SWEEP)

    first = world.executor().apply(_action(lift))

    assert not first.success
    assert "needs-human" not in world.github.labels[ITEM], "the removal landed"
    world.blocked_by(ITEM, _SWEEP)  # the stuck sweep gives up on the item again

    replay = world.executor().apply(_action(lift))

    assert replay.details["refusal"] == BlockResolutionRefusal.INTERRUPTED.value
    assert "needs-human" in world.github.labels[ITEM]
    assert world.store.needs_human_causes(ITEM) == frozenset({"session_lifecycle"})


def test_a_failed_attempted_discharge_stays_begun_even_if_the_label_is_back(tmp_path: Path) -> None:
    """r4 F1: the removal landed, its confirming read failed, and the operator
    put the label back at once. Only the owner knows a write was attempted, so
    the discharge stays begun and a replay is handed back."""
    world = World(tmp_path, question=None)
    flaky = _ConfirmFails(world.github, operator_puts_it_back=True)
    world.block = NeedsHumanBlock(
        "needs-human", "tech-lead-needs-human", flaky, flaky.read, frozenset, world.store,
    )
    world.blocked_by(ITEM, _SWEEP)
    lift = _resolution(ResolutionKind.LIFT, _SWEEP)

    assert not world.executor().apply(_action(lift)).success
    assert world.discharges.block_resolution_state(decision_id="run-1/A1") is not None

    replay = world.executor().apply(_action(lift))

    assert replay.details["refusal"] == BlockResolutionRefusal.INTERRUPTED.value
    assert "needs-human" in world.github.labels[ITEM]


def test_a_child_whose_filed_body_carries_an_extra_edge_is_never_made_runnable(tmp_path: Path) -> None:
    """r4 F2: the filed graph must be exactly the decided one."""
    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT, _SWEEP)
    original = world.github.create_issue

    def with_extra_edge(**kwargs: Any) -> dict[str, Any]:
        created = original(**kwargs)
        world.github.bodies[created["number"]] += "\nDepends-on: #999999\n"
        return created

    world.github.create_issue = with_extra_edge  # type: ignore[method-assign]

    result = world.executor().apply(_action(_split()))

    assert not result.success
    [child] = world.github.created
    assert AGENT not in world.github.labels[child["number"]]
    assert "needs-human" in world.github.labels[ITEM]


def _replay_with_child(world: World, mutate_child: Any) -> ActionResult:
    """File the split's child, fail before the discharge, mutate the child, replay."""
    world.blocked_by(ITEM, _AGENT, _SWEEP)
    world.applier.fail = AddCommentAction
    assert not world.executor().apply(_action(_split())).success
    [child] = world.github.created
    world.github.labels[child["number"]].discard(AGENT)
    mutate_child(child["number"])
    world.applier.fail = None
    return world.executor().apply(_action(_split()))


def test_a_replayed_child_moved_to_another_repository_is_never_made_runnable(tmp_path: Path) -> None:
    """r5 F1: the same number in another repository is a different edge."""
    world = World(tmp_path)

    def cross_repo(number: int) -> None:
        world.github.bodies[number] = world.github.bodies[number].replace(
            "Depends-on: #262", "Depends-on: other/repo#262")

    result = _replay_with_child(world, cross_repo)

    assert not result.success
    assert AGENT not in world.github.labels[world.github.created[0]["number"]]
    assert "needs-human" in world.github.labels[ITEM]


def test_a_replayed_child_that_was_closed_stops_the_resolution(tmp_path: Path) -> None:
    """r5 F2: a closed child queues none of the split's work."""
    world = World(tmp_path)

    result = _replay_with_child(world, lambda number: world.github.states.__setitem__(number, "closed"))

    assert not result.success
    assert "needs-human" in world.github.labels[ITEM]
    assert world.store.needs_human_causes(ITEM) == frozenset({"agent_completion", "session_lifecycle"})


def test_a_failed_review_gate_posts_no_cause_marker(tmp_path: Path) -> None:
    """r5 F3: markers never stand for a discharge the failed gate prevented."""
    world = World(tmp_path)
    world.blocked_by(364, _AGENT)
    world.holds = (PublishedReviewHold(364, 379, "364-branch", "r1", "a" * 40),)
    world.applier.fail = AddLabelAction

    result = world.executor().apply(_action(_resolution(), number=364))

    assert not result.success
    assert world.github.comments.get(364, []) == []
    assert world.store.needs_human_causes(364) == frozenset({"agent_completion"})


def test_no_cause_marker_stands_for_a_discharge_that_never_happened(tmp_path: Path) -> None:
    """r7 F1: the discharge failed before any write; a DIFFERENT decision for
    the same cause is still decided, since nothing recorded a discharge."""
    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT)
    executor = world.executor()
    object.__setattr__(executor, "block", _RacedBlock(
        world.block, lambda t, c, r: ResolutionOutcome(BlockOutcome.FAILED, mutation_attempted=False)))
    assert not executor.apply(_action(_resolution())).success
    assert prior_resolutions(world.github.comments[ITEM]) == {}

    other = world.executor().apply(_action(_resolution(title="Another answer"), action_id="A2", run="run-2"))

    assert other.success, other.error
    assert "needs-human" not in world.github.labels[ITEM]


def test_the_local_record_refuses_a_second_resolve_when_the_marker_was_never_posted(tmp_path: Path) -> None:
    """r7 F1: a crash between the discharge and its marker comment still
    leaves the reversibility rule standing, from the authority store."""
    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT)
    _committed(world, _AGENT, decision="run-0/A9")  # discharged, marker never posted

    result = world.executor().apply(_action(_resolution()))

    assert result.details["refusal"] == BlockResolutionRefusal.RESOLVED_BEFORE.value
    assert "needs-human" in world.github.labels[ITEM]


def test_children_are_not_runnable_until_the_discharge_commits(tmp_path: Path) -> None:
    """r7 F2: a split whose decision comment failed leaves its child filed
    but not labelled for pickup; the replay makes it runnable exactly once."""
    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT, _SWEEP)
    world.applier.fail = AddCommentAction

    assert not world.executor().apply(_action(_split())).success
    [child] = world.github.created
    assert AGENT not in world.github.labels[child["number"]]
    assert "needs-human" in world.github.labels[ITEM]

    world.applier.fail = None
    assert world.executor().apply(_action(_split())).success

    assert AGENT in world.github.labels[child["number"]]
    assert len(world.github.created) == 1
    assert "needs-human" not in world.github.labels[ITEM]


def test_a_failed_activation_is_finished_by_the_replay(tmp_path: Path) -> None:
    """After the discharge committed, a failed child label is retried by the
    replay, which never discharges again."""
    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT, _SWEEP)
    world.applier.fail = AddLabelAction

    assert not world.executor().apply(_action(_split())).success
    assert "needs-human" not in world.github.labels[ITEM]
    world.applier.fail = None

    replay = world.executor().apply(_action(_split()))

    assert replay.success, replay.error
    [child] = world.github.created
    assert AGENT in world.github.labels[child["number"]]
    assert set(prior_resolutions(world.github.comments[ITEM])) == {_AGENT, _SWEEP}
    assert world.requeued == [ITEM]


def test_a_child_changed_after_the_discharge_is_never_activated(tmp_path: Path) -> None:
    """r8 F1: activation re-checks the graph as it stands then."""
    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT, _SWEEP)
    world.applier.fail = AddLabelAction
    assert not world.executor().apply(_action(_split())).success  # discharge committed, label failed
    [child] = world.github.created
    world.github.states[child["number"]] = "closed"
    world.applier.fail = None

    replay = world.executor().apply(_action(_split()))

    assert not replay.success
    assert AGENT not in world.github.labels[child["number"]]


def test_a_cause_gone_before_the_discharge_discharges_nothing(tmp_path: Path) -> None:
    """r8 F2: all of the decision or none of it. One named cause stopped
    standing after verify while another cause keeps the label: nothing is
    withdrawn, nothing recorded, and a later decision is not refused."""
    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT, _SWEEP, NeedsHumanCause.ACTION_LIVENESS)
    executor = world.executor()
    resolve = world.block.resolve

    def sweep_gone_first(target, causes, reason):  # type: ignore[no-untyped-def]
        world.store.withdraw_needs_human_cause(ITEM, _SWEEP.value)
        return resolve(target, causes, reason)

    object.__setattr__(executor, "block", _RacedBlock(world.block, sweep_gone_first))
    split = _split()

    result = executor.apply(_action(split))

    assert not result.success
    assert world.store.needs_human_causes(ITEM) == frozenset({"agent_completion", "action_liveness"})
    assert world.discharges.block_resolution_state(decision_id="run-1/A1") is None
    assert prior_resolutions(world.github.comments.get(ITEM, [])) == {}
    later = world.executor().apply(_action(_resolution(), action_id="A2", run="run-2"))
    assert later.details.get("refusal") != BlockResolutionRefusal.RESOLVED_BEFORE.value


def test_a_label_cleared_before_the_discharge_records_no_discharge(tmp_path: Path) -> None:
    """r9 F1: the operator cleared needs-human between verify and the
    discharge. Nothing was discharged, so nothing is recorded, no child is
    activated, the item is not requeued, and a later decision is not refused."""
    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT)
    executor = world.executor()
    resolve = world.block.resolve

    def operator_clears_first(target, causes, reason):  # type: ignore[no-untyped-def]
        world.github.remove_label(ITEM, "needs-human")
        return resolve(target, causes, reason)

    object.__setattr__(executor, "block", _RacedBlock(world.block, operator_clears_first))
    split = _split()
    split = BlockResolution(kind=split.kind, causes=frozenset({_AGENT}), title=split.title, body=split.body,
                            evidence=split.evidence, children=split.children, parent=split.parent)

    result = executor.apply(_action(split))

    assert not result.success
    assert world.discharges.block_resolution_state(decision_id="run-1/A1") is None
    assert prior_resolutions(world.github.comments.get(ITEM, [])) == {}
    [child] = world.github.created
    assert AGENT not in world.github.labels[child["number"]]
    assert world.requeued == []
    world.blocked_by(ITEM, _AGENT)
    later = world.executor().apply(_action(_resolution(), action_id="A2", run="run-2"))
    assert later.success, later.error


def test_a_merge_hold_is_never_resolved(tmp_path: Path) -> None:
    """#7678: a person asked to decide before a merge holds only the merge;
    the operator merges, by design, so a resolution never touches it."""
    world = World(tmp_path)
    world.blocked_by(ITEM, NeedsHumanCause.MERGE_DECISION)

    result = world.executor().apply(_action(_resolution()))

    assert result.details["refusal"] == BlockResolutionRefusal.MERGE_HOLD.value
    assert "needs-human" in world.github.labels[ITEM]
    assert world.applier.applied == []


def test_a_split_child_just_filed_is_activated_though_search_has_not_indexed_it(tmp_path: Path) -> None:
    """Exam F at 4a8011c: the marker search could not see the child filed a
    moment earlier, so the split failed. The apply that filed it uses its number."""
    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT, _SWEEP)
    world.github.search_lags = True

    result = world.executor().apply(_action(_split()))

    assert result.success, result.error
    [child] = world.github.created
    assert AGENT in world.github.labels[child["number"]]


# -- the decided answer is a standing ruling in the body (#8141) ---------------


def test_327_an_answer_lands_in_the_item_body_before_the_block_is_discharged(tmp_path: Path) -> None:
    """porchpin#327/#501: the approved answer stayed in a comment and the
    resumed session never read it. It is the item's standing ruling now."""
    world = World(tmp_path)
    world.github.bodies[ITEM] = "## Outcome\n\nThe buyer index."
    world.blocked_by(ITEM, _AGENT)
    action = _action(_resolution(title="Build option A", body="A buyer join-key map of its own."))

    result = world.executor().apply(action)

    assert result.success, result.error
    [ruling] = parse_rulings_block(world.github.bodies[ITEM])
    assert ruling.ruling_id == resolution_ruling_id(action.decision_id)
    assert ruling.authority is RulingAuthority.APPROVED_RESOLUTION
    assert ruling.text == "## Build option A\n\nA buyer join-key map of its own."
    assert world.github.bodies[ITEM].endswith("The buyer index.")
    assert world.bodies_written == [ITEM]


def test_a_replay_records_the_ruling_once(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT)
    action = _action(_resolution())
    assert world.executor().apply(action).success

    world.executor().apply(action)

    assert len(parse_rulings_block(world.github.bodies[ITEM])) == 1 and world.bodies_written == [ITEM]


def test_a_ruling_github_would_not_keep_leaves_the_block_in_place(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT)
    world.body_write_fails = True

    result = world.executor().apply(_action(_resolution()))

    assert not result.success and "standing ruling not recorded" in (result.error or "")
    assert "needs-human" in world.github.labels[ITEM]
    assert world.store.needs_human_causes(ITEM) == frozenset({"agent_completion"})
    assert world.requeued == []


def test_a_narrowing_split_binds_the_parent_and_a_lift_binds_nothing(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT, _SWEEP, labels=(AGENT,))
    world.blocked_by(326, _SWEEP)
    world.question = None

    assert world.executor().apply(_action(_split())).success
    assert world.executor().apply(_action(_resolution(ResolutionKind.LIFT, _SWEEP), number=326,
                                          action_id="A2")).success

    assert len(parse_rulings_block(world.github.bodies[ITEM])) == 1
    assert parse_rulings_block(world.github.bodies.get(326, "")) == ()


# -- steps beyond the item (#8691) ---------------------------------------------


def _steps_owner(world: World) -> DecisionStepsOwner:
    rulings = StandingRulingsOwner(
        read_issue=world.github.issue, write_body=world.write_body, index=InMemoryStandingRulingsIndex(),
    )
    return DecisionStepsOwner(
        read_issue=world.github.issue, read_pr=lambda number: None, list_milestones=lambda: [],
        set_milestone=lambda number, milestone: None, write_body=world.write_body,
        comment_marker_present=lambda number, marker: any(
            marker in body for body in world.github.comments.get(number, [])),
        apply_action=world.applier.apply, require_authority=lambda action, number: None,
        rulings=rulings, block=world.block, repo_slug="porchpin/porchpin",
    )


def _approved_with_steps(*steps: DecisionStep) -> ResolveBlockAction:
    from dataclasses import replace

    return replace(_action(_resolution(title="Build the D1 index", body="ADR-0009 rules it.")),
                   proposal_issue_number=501, follow_through=DecisionFollowThrough(steps=steps))


def test_an_approved_answer_carries_out_its_steps_beyond_the_item_once(tmp_path: Path) -> None:
    """porchpin#327/#501: the approved answer also notes its consequence in
    another issue's body; it lands before the block comes off, once."""
    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT)
    world.github.labels[290] = {AGENT}
    action = _approved_with_steps(
        DecisionStep(DecisionStepKind.RECORD_RULING, 290, text="#262's index is the one buyer registry."),
        DecisionStep(DecisionStepKind.COMMENT, 290, text="Decided on #262."),
    )
    executor = world.executor(_steps_owner(world))

    first = executor.apply(action)
    applied = len(world.applier.applied)
    again = executor.apply(action)

    assert first.success and again.success, (first.error, again.error)
    [ruling] = parse_rulings_block(world.github.bodies[290])
    assert ruling.text == "#262's index is the one buyer registry."
    assert sum("Decided on #262." in body for body in world.github.comments[290]) == 1
    markers = [body for body in world.github.comments[501] if "io:decision-step:" in body]
    assert len(markers) == 2
    assert len([a for a in world.applier.applied[applied:] if not isinstance(a, AddCommentAction)]) == 0


def test_an_answer_whose_step_no_longer_applies_is_refused_untouched(tmp_path: Path) -> None:
    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT)
    action = _approved_with_steps(DecisionStep(DecisionStepKind.RECORD_RULING, 404, text="note"))

    result = world.executor(_steps_owner(world)).apply(action)

    assert result.details["refusal"] == BlockResolutionRefusal.STEP_NOT_APPLICABLE.value
    assert world.applier.applied == [] and "needs-human" in world.github.labels[ITEM]


def test_an_answer_whose_later_step_broke_after_an_earlier_applied_is_handed_back(tmp_path: Path) -> None:
    """#8691 r2 F2 on resolve_block: step 1 already applied (its marker is on the
    proposal) and step 2's target closed: never closed as stale, the operator's."""
    from issue_orchestrator.domain.decision_steps import step_marker

    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT)
    world.github.labels[290] = {AGENT}
    world.github.labels[291] = {AGENT}
    world.github.states[291] = "closed"
    world.github.comments[501] = [f"Step 1 applied: x\n\n{step_marker('501', 1)}"]
    action = _approved_with_steps(
        DecisionStep(DecisionStepKind.COMMENT, 290, text="one"),
        DecisionStep(DecisionStepKind.RECORD_RULING, 291, text="two"),
    )

    result = world.executor(_steps_owner(world)).apply(action)

    assert result.result_type.value == "failure" and "and step(s) 1" in (result.error or "")
    assert "needs-human" in world.github.labels[ITEM]



def test_a_resolution_reports_a_step_refused_after_its_discharge(tmp_path: Path) -> None:
    """#8691 r5 F2 on resolve_block: a step refused at write time after the
    discharge is in the result and the event, never a silent success."""
    from issue_orchestrator.domain.decision_steps import step_marker, step_refused_marker

    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT)
    world.github.comments[501] = [f"Step 1 not applied: moved\n\n{step_marker('501', 1)}\n{step_refused_marker('501', 1)}"]
    world.github.labels[290] = {AGENT}
    action = _approved_with_steps(DecisionStep(DecisionStepKind.COMMENT, 290, text="one"))

    result = world.executor(_steps_owner(world)).apply(action)

    assert result.success and result.details["steps_refused"] == ["step 1 (comment #290)"]
    [executed] = [e for e in world.events if e.event_type == EventName.TECH_LEAD_ACTION_EXECUTED]
    assert executed.data["boundary"]["steps_refused"] == ["step 1 (comment #290)"]


def test_an_interrupted_discharge_of_a_begun_decision_is_handed_back(tmp_path: Path) -> None:
    """#8691 r7 F2: the decision began (ruling, steps), its discharge was
    interrupted: the replay hands it back, never closes it as stale."""
    from issue_orchestrator.domain.decision_steps import decision_begun_marker

    world = World(tmp_path)
    world.blocked_by(ITEM, _AGENT)
    world.github.labels[290] = {AGENT}
    action = _approved_with_steps(DecisionStep(DecisionStepKind.COMMENT, 290, text="one"))
    world.github.comments[501] = [f"Applying.\n\n{decision_begun_marker('501')}"]
    world.discharges.begin_block_resolution(
        decision_id=action.decision_id, issue_number=ITEM, causes=frozenset({_AGENT.value}))

    result = world.executor(_steps_owner(world)).apply(action)

    assert result.result_type.value == "failure" and "partly applied" in (result.error or "")
