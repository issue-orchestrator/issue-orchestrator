"""The planner's liveness gate, driven through the real planning cycle (#7350).

These tests run ``run_planning_cycle`` with the real gate and the real
``OrchestratorSupport.apply_plan``. The planner is a fake that re-derives the
same action every tick - the census loop shape - and the action applier is
faked at its port, so what is under test is exactly the path between them.
"""

from __future__ import annotations

import dataclasses
import functools
import importlib
import inspect
import pkgutil
import time
import types
import typing
from datetime import datetime, timedelta
from pathlib import PurePath
from unittest.mock import MagicMock

import pytest

import issue_orchestrator.control as control_package
from issue_orchestrator.control.action_base import Action
from issue_orchestrator.control.actions import (
    ActionResult,
    AddLabelAction,
    LaunchSessionAction,
    RemoveLabelAction,
    ReportPromotedFindingEvidenceAction,
    SessionType,
    SettleTechLeadPromotionAction,
)
from issue_orchestrator.control.issue_fetch_resilience import IssueFetchResilience
from issue_orchestrator.control.orchestrator_support import (
    OrchestratorSupport,
    run_planning_cycle,
)
from issue_orchestrator.control.planned_action_liveness import (
    PlannedActionLiveness,
    planned_action_key,
)
from issue_orchestrator.control.planner_types import OrchestratorSnapshot, Plan
from issue_orchestrator.control.reconciliation import (
    ExternalSnapshot,
    ReconciliationRequired,
    build_expected_for_mutation,
    get_pause_label,
)
from issue_orchestrator.domain.action_liveness import LivenessPolicy
from issue_orchestrator.domain.models import Issue, OrchestratorState
from issue_orchestrator.infra.config import Config
from tests.unit.control.liveness_doubles import (
    InMemoryActionLivenessStore,
    ManualClock,
    RecordingEscalation,
    liveness_owner,
)

POLICY = LivenessPolicy(max_attempts=3)
PAUSE = get_pause_label()
NEEDS_HUMAN = "needs-human"


def _settle() -> SettleTechLeadPromotionAction:
    return SettleTechLeadPromotionAction(
        signature="tech-lead-batch-manifest-diff-fetch-blocked-by-gh-guard",
        case_file_issue_number=229,
        target_repo="issue-orchestrator/issue-orchestrator",
        target_issue_number=7289,
        shipped=False,
        reason="promoted finding closed (tick-varying prose: %s)",
        expected=build_expected_for_mutation(),
    )


class _Engine:
    """Ticks the real planning cycle; the planner and applier are port fakes."""

    def __init__(self, sample_config: Config, planned, apply, *, labels=None, policy=POLICY):
        self.clock = ManualClock()
        self.escalation = RecordingEscalation()
        self.store = InMemoryActionLivenessStore()
        self.owner = liveness_owner(
            store=self.store, escalation=self.escalation, clock=self.clock, policy=policy
        )
        self.labels = dict(labels or {})
        self.planned = planned
        self.config = sample_config
        self.config.fetch_layer_network_sync_seconds = 10**9
        self.state = OrchestratorState()
        self.applier = MagicMock()
        self.applier.apply.side_effect = apply
        self.pauses: list[int] = []
        self.support = OrchestratorSupport(
            config=self.config,
            events=MagicMock(),
            repository_host=MagicMock(),
            state=self.state,
            event_context=MagicMock(enrich=lambda payload: payload),
            session_manager=MagicMock(),
            action_applier=self.applier,
            fact_gatherer=MagicMock(),
            planner=MagicMock(),
            worktree_manager=MagicMock(),
            state_machine_manager=MagicMock(),
            cleanup_manager=MagicMock(),
            get_review_machine=MagicMock(),
            kill_session=MagicMock(),
        )
        self.tick_count = 0

    def _snapshot(self) -> OrchestratorSnapshot:
        return OrchestratorSnapshot(
            issues=tuple(
                Issue(number=n, title=f"#{n}", labels=list(labels))
                for n, labels in self.labels.items()
            ),
            active_sessions=(),
            pending_reviews=(),
            pending_reworks=(),
            pending_tech_lead=(),
            paused=False,
        )

    def tick(self, *, advance: timedelta = timedelta(hours=1)) -> Plan:
        plans: list[Plan] = []
        fact_gatherer = MagicMock()
        fact_gatherer.create_snapshot.side_effect = lambda *a, **k: self._snapshot()
        planner = MagicMock()
        planner.plan.side_effect = lambda snapshot: Plan(
            actions=tuple(self.planned()), skipped=()
        )

        def apply(plan: Plan) -> None:
            plans.append(plan)
            self.support.apply_plan(plan, lambda number, _reason: self.pauses.append(number))

        run_planning_cycle(
            config=self.config,
            events=MagicMock(),
            event_context=MagicMock(enrich=lambda payload: payload),
            state=self.state,
            fact_gatherer=fact_gatherer,
            planner=planner,
            repository_host=MagicMock(),
            scheduler=MagicMock(),
            github_workflow=MagicMock(),
            apply_plan_fn=apply,
            clear_discovered_facts_fn=MagicMock(),
            last_network_sync=time.time(),
            refresh_requested=False,
            inflight_stable_ids={},
            issue_fetch_resilience=IssueFetchResilience("owner/repo"),
            action_liveness=PlannedActionLiveness(self.owner, escalation_label=NEEDS_HUMAN),
        )
        self.clock.advance(advance)
        self.tick_count += 1
        return plans[0]

    def attempts_of(self, action_type) -> int:
        return sum(
            1 for call in self.applier.apply.call_args_list
            if call.args[0].action_type is action_type
        )


# --- The invariant --------------------------------------------------------


@pytest.mark.parametrize(
    "action",
    [
        # Census loop #2 and the promotion appliers the #7345 fix listed as
        # still unbounded: each returns ActionResult.fail and is replanned.
        _settle(),
        ReportPromotedFindingEvidenceAction(
            signature="sig", case_file_issue_number=229, target_repo="o/r",
            target_issue_number=7289, observation_count=4, comment="evidence",
            reason="report", expected=build_expected_for_mutation(),
        ),
        RemoveLabelAction(issue_number=410, label="in-progress", reason="stale"),
    ],
    ids=["settle_promotion", "report_promoted_evidence", "stale_label_removal"],
)
def test_an_unchanged_failing_action_is_attempted_at_most_max_attempts_times(
    sample_config, action
) -> None:
    """INVARIANT: no (subject, action) pair is attempted more than N times
    with an unchanged fact fingerprint without a success. It parks, escalates
    once, and every later tick reports it held instead of trying again."""
    engine = _Engine(
        sample_config,
        planned=lambda: [action],
        apply=lambda a: ActionResult.fail(a, "raised every tick"),
    )

    plans = [engine.tick() for _ in range(40)]

    assert engine.attempts_of(action.action_type) == POLICY.max_attempts
    assert len(engine.escalation.parked) == 1
    held = plans[-1].skipped[-1]
    assert held.item_type == f"action:{action.action_type.value}"
    assert held.reason.startswith("parked after transient: 3 attempts failed")


def test_the_same_holds_for_an_applier_that_raises(sample_config) -> None:
    def explode(action):
        raise RuntimeError("AmbiguousPatternPublicationError: mid-publish")

    engine = _Engine(sample_config, planned=lambda: [_settle()], apply=explode)
    for _ in range(20):
        engine.tick()

    assert engine.attempts_of(_settle().action_type) == POLICY.max_attempts


def test_backoff_holds_the_action_between_attempts(sample_config) -> None:
    engine = _Engine(
        sample_config,
        planned=lambda: [_settle()],
        apply=lambda a: ActionResult.fail(a, "503"),
    )
    for _ in range(10):
        engine.tick(advance=timedelta(seconds=10))

    # 10 ticks, 100 s: attempt at 0 s, backoff 60 s -> attempt at 60 s, then
    # backoff 120 s runs past the window.
    assert engine.attempts_of(_settle().action_type) == 2


def test_the_free_text_reason_is_not_a_fact(sample_config) -> None:
    ticks = iter(range(1000))
    engine = _Engine(
        sample_config,
        planned=lambda: [dataclasses.replace(_settle(), reason=f"tick {next(ticks)}")],
        apply=lambda a: ActionResult.fail(a, "boom"),
    )
    for _ in range(10):
        engine.tick()

    assert engine.attempts_of(_settle().action_type) == POLICY.max_attempts


# --- Census loop #4: a subject paused behind io:needs-reconcile ------------


def _refused_for_pause(action):
    if isinstance(action, RemoveLabelAction):
        raise ReconciliationRequired(
            "issue", 410,
            ExternalSnapshot.for_issue(410, {"in-progress"}),
            ExternalSnapshot.for_issue(410, {"in-progress", PAUSE}),
            reason="Has forbidden labels",
        )
    return ActionResult.ok(action)


def test_a_paused_subject_parks_its_mutations_and_stops_halting_the_plan(sample_config) -> None:
    """Census #4: every tick planned the stale-label removal first, it was
    refused for the pause label, and the refusal halted the rest of the plan -
    133 ticks of starved review launches. Now it is refused ONCE, parks as
    needs-human, and the actions planned after it run on the very next tick."""
    stale = RemoveLabelAction(issue_number=410, label="in-progress", reason="stale")
    review = AddLabelAction(issue_number=381, label="code-reviewed", reason="unrelated work")
    engine = _Engine(
        sample_config,
        planned=lambda: [stale, review],
        apply=_refused_for_pause,
        labels={410: ("in-progress", PAUSE), 381: ()},
    )

    engine.tick()
    for _ in range(5):
        engine.tick()

    assert engine.attempts_of(stale.action_type) == 1
    assert engine.pauses == [410]
    assert engine.attempts_of(review.action_type) == 5
    [parked] = engine.escalation.parked
    assert parked.last_outcome.value == "needs_human"
    assert parked.key.escalation_issue == 410


def test_a_person_removing_the_pause_label_releases_the_park(sample_config) -> None:
    stale = RemoveLabelAction(issue_number=410, label="in-progress", reason="stale")
    refuse = {"on": True}

    def apply(action):
        if refuse["on"]:
            return _refused_for_pause(action)
        return ActionResult.ok(action)

    engine = _Engine(
        sample_config, planned=lambda: [stale], apply=apply,
        labels={410: ("in-progress", PAUSE)},
    )
    engine.tick()
    engine.tick()
    assert engine.attempts_of(stale.action_type) == 1

    refuse["on"] = False
    engine.labels[410] = ("in-progress",)
    engine.tick()

    assert engine.attempts_of(stale.action_type) == 2
    # The new fingerprint succeeded: the identity is clean and the block
    # this owner put on #410 is released.
    assert engine.store.rows == {}
    assert engine.escalation.unblocks == [(410, True)]


# --- Scope boundaries ------------------------------------------------------


def test_launches_stay_with_their_own_settlement(sample_config) -> None:
    """A launch's ActionResult flattens provider deferral, host rate limits and
    retryable failures into one "failed"; LaunchSettlement owns those."""
    launch = LaunchSessionAction(session_type=SessionType.ISSUE, number=7)
    engine = _Engine(
        sample_config,
        planned=lambda: [launch],
        apply=lambda a: ActionResult.fail(a, "provider deferred"),
    )
    for _ in range(10):
        engine.tick()

    assert engine.attempts_of(launch.action_type) == 10
    assert engine.store.rows == {}


def test_an_ungated_plan_cannot_be_applied(sample_config) -> None:
    engine = _Engine(sample_config, planned=lambda: [], apply=lambda a: ActionResult.ok(a))
    with pytest.raises(ValueError, match="ungated"):
        engine.support.apply_plan(
            Plan(actions=(_settle(),), skipped=()), lambda *_: None
        )


def test_subjects_and_escalation_issues() -> None:
    settle_key = planned_action_key(_settle(), {}, escalation_label=NEEDS_HUMAN)
    assert settle_key.identity.subject == "issue:229"
    assert settle_key.escalation_issue == 229
    label_key = planned_action_key(
        RemoveLabelAction(issue_number=410, label="x"), {410: ("a",)},
        escalation_label=NEEDS_HUMAN,
    )
    assert label_key.identity.subject == "issue:410"
    assert label_key.fingerprint != planned_action_key(
        RemoveLabelAction(issue_number=410, label="x"), {410: ("a", PAUSE)},
        escalation_label=NEEDS_HUMAN,
    ).fingerprint
    # The owner's own escalation label is not a fact: its park must not look
    # like progress.
    assert label_key.fingerprint == planned_action_key(
        RemoveLabelAction(issue_number=410, label="x"), {410: ("a", NEEDS_HUMAN)},
        escalation_label=NEEDS_HUMAN,
    ).fingerprint


# --- Every action type can be fingerprinted --------------------------------

_LEAVES = (str, int, float, bool, type(None), datetime, PurePath)


def _all_action_classes() -> set[type]:
    for module in pkgutil.walk_packages(control_package.__path__, "issue_orchestrator.control."):
        importlib.import_module(module.name)
    found: set[type] = set()
    pending = [Action]
    while pending:
        cls = pending.pop()
        for sub in cls.__subclasses__():
            if sub not in found:
                found.add(sub)
                pending.append(sub)
    return found


@functools.cache
def _type_namespace() -> dict[str, object]:
    """Every class in the domain and control packages, by name.

    Action modules import some field types only under TYPE_CHECKING, so the
    annotations have to be resolved against the packages, not the module.
    """
    import issue_orchestrator.domain as domain_package

    namespace: dict[str, object] = {}
    for package in (domain_package, control_package):
        for module in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
            for name, value in vars(importlib.import_module(module.name)).items():
                if inspect.isclass(value):
                    namespace.setdefault(name, value)
    return namespace


def _field_hints(cls: type) -> dict[str, object]:
    """Each dataclass field's annotation, evaluated against the packages."""
    import annotationlib

    hints: dict[str, object] = {}
    for owner in reversed(cls.__mro__):
        if not dataclasses.is_dataclass(owner):
            continue
        namespace = {**_type_namespace(), **vars(importlib.import_module(owner.__module__))}
        raw = annotationlib.get_annotations(owner, format=annotationlib.Format.STRING)
        for name, text in raw.items():
            hints[name] = eval(text, namespace)
    names = {field.name for field in dataclasses.fields(cls)}
    return {name: hint for name, hint in hints.items() if name in names}


def _check_type(hint, where: str, seen: set) -> None:
    if isinstance(hint, typing.ForwardRef):
        hint = hint.__forward_arg__
    if isinstance(hint, str):
        hint = eval(hint, dict(_type_namespace()))
    if hint in seen:
        return
    origin = typing.get_origin(hint)
    if origin is typing.Literal:
        return
    if origin in (typing.Union, types.UnionType, tuple, frozenset, set, list, dict):
        for arg in typing.get_args(hint):
            if arg is not Ellipsis:
                _check_type(arg, where, seen)
        return
    if hint is typing.Any or not inspect.isclass(hint):
        raise AssertionError(f"{where}: un-fingerprintable annotation {hint!r}")
    if issubclass(hint, _LEAVES) or issubclass(hint, __import__("enum").Enum):
        return
    if dataclasses.is_dataclass(hint):
        seen.add(hint)
        for name, field_hint in _field_hints(hint).items():
            _check_type(field_hint, f"{hint.__name__}.{name}", seen)
        return
    raise AssertionError(f"{where}: {hint!r} is neither a leaf nor a dataclass")


def test_every_planned_action_type_can_be_fingerprinted() -> None:
    """The gate fingerprints every field of every planned action. A field type
    it cannot canonicalize would raise in the tick, so prove statically that
    none exists - including action types added after this test."""
    classes = _all_action_classes()
    assert len(classes) > 40
    seen: set = set()
    for cls in classes:
        _check_type(cls, cls.__name__, seen)


def test_a_plan_cannot_spend_more_than_one_attempt_of_a_budget(sample_config) -> None:
    """Identical actions in one plan are one question, asked once (review r4)."""
    engine = _Engine(
        sample_config,
        planned=lambda: [_settle()] * (POLICY.max_attempts + 1),
        apply=lambda a: ActionResult.fail(a, "boom"),
    )
    engine.tick()
    assert engine.attempts_of(_settle().action_type) == 1
    for _ in range(10):
        engine.tick()
    assert engine.attempts_of(_settle().action_type) == POLICY.max_attempts
    assert len(engine.escalation.parked) == 1


# --- Round 1 review: facts that move without the problem moving -------------


def test_a_provider_impact_write_resampled_every_tick_is_still_bounded(sample_config) -> None:
    """The assessment is re-sampled each tick (a new ``assessed_at`` and a
    shrinking countdown) while the circuit is exactly as it was."""
    from datetime import datetime, timezone

    from issue_orchestrator.control.provider_impact import (
        ApplyProviderImpactAction,
        ProviderImpactAssessment,
        ProviderImpactTransition,
    )

    ticks = iter(range(1000))

    def planned():
        tick = next(ticks)
        return [
            ApplyProviderImpactAction(
                issue_number=410,
                transition=ProviderImpactTransition.BLOCKED,
                label="blocked:provider-unavailable",
                assessment=ProviderImpactAssessment(
                    assessed_at=datetime(2026, 9, 27, tzinfo=timezone.utc)
                    + timedelta(seconds=tick),
                    open_providers=("claude",),
                    next_retry_at="2026-09-27T13:00:00+00:00",
                    cooldown_remaining_seconds=3600 - tick,
                ),
            )
        ]

    engine = _Engine(sample_config, planned=planned, apply=lambda a: ActionResult.fail(a, "403"))
    for _ in range(20):
        engine.tick()

    assert engine.attempts_of(planned()[0].action_type) == POLICY.max_attempts


def test_the_owners_own_block_does_not_look_like_progress(sample_config) -> None:
    """When the park's needs-human label lands, the next snapshot shows it on
    the issue. That is the owner's own write, not a fact the refused mutation
    depends on, so the mutation stays parked: one attempt, one park."""
    stale = RemoveLabelAction(issue_number=410, label="in-progress", reason="stale")
    engine = _Engine(
        sample_config, planned=lambda: [stale], apply=_refused_for_pause,
        labels={410: ("in-progress", PAUSE)},
    )
    block = engine.escalation.block

    def block_and_label(row):
        engine.labels[410] = (*engine.labels[410], NEEDS_HUMAN)
        return block(row)

    engine.escalation.block = block_and_label
    for _ in range(6):
        engine.tick()

    assert engine.attempts_of(stale.action_type) == 1
    assert len(engine.escalation.parked) == 1


def test_a_request_for_a_human_is_never_parked(sample_config) -> None:
    """The stuck sweep re-emits its needs-human write until the label is
    observed. If GitHub refuses it for longer than any budget, the write must
    still land once GitHub recovers."""
    from issue_orchestrator.control.stuck_sweep import build_stuck_sweep_escalation_actions

    [escalate] = build_stuck_sweep_escalation_actions((410,), NEEDS_HUMAN)
    refusing = {"on": True}
    engine = _Engine(
        sample_config,
        planned=lambda: [escalate],
        apply=lambda a: ActionResult.fail(a, "502") if refusing["on"] else ActionResult.ok(a),
    )
    for _ in range(POLICY.max_attempts * 3):
        engine.tick()
    refusing["on"] = False
    engine.tick()

    assert engine.attempts_of(escalate.action_type) == POLICY.max_attempts * 3 + 1
    assert engine.store.rows == {}


def test_each_planning_cycle_retries_a_block_that_did_not_land(sample_config) -> None:
    engine = _Engine(
        sample_config, planned=lambda: [_settle()],
        apply=lambda a: ActionResult.fail(a, "boom"),
    )
    engine.escalation.commits = False
    for _ in range(POLICY.max_attempts):
        engine.tick()
    assert engine.escalation.committed_blocks == []

    engine.escalation.commits = True
    engine.tick()

    [blocked] = engine.escalation.committed_blocks
    assert blocked.key.escalation_issue == 229


def test_one_operations_success_does_not_erase_anothers_budget(sample_config) -> None:
    """Two label removals on one issue are two operations: the one that keeps
    succeeding must not clear the one that keeps failing (review round 3)."""
    failing = RemoveLabelAction(issue_number=410, label="in-progress", reason="stale")
    fine = RemoveLabelAction(issue_number=410, label="io:claimed", reason="stale claim")
    engine = _Engine(
        sample_config,
        planned=lambda: [failing, fine],
        apply=lambda a: ActionResult.fail(a, "403") if a is failing else ActionResult.ok(a),
    )
    for _ in range(20):
        engine.tick()

    removals = [call.args[0] for call in engine.applier.apply.call_args_list]
    assert sum(1 for a in removals if a is failing) == POLICY.max_attempts
    assert sum(1 for a in removals if a is fine) == 20


# --- #7303's typed GitHub rate limit is transient(retry_at) -----------------


def _rate_limited(engine, *, raise_it: bool):
    from issue_orchestrator.domain.host_rate_limit import HostRateLimit
    from issue_orchestrator.ports.repository_host import RepositoryHostRateLimitedError

    def apply(action):
        limit = HostRateLimit(
            resets_at=engine.clock.now + timedelta(minutes=45), kind="primary"
        )
        error = RepositoryHostRateLimitedError("API rate limit exceeded")
        error.rate_limit = limit  # the port's typed limit, as adapters attach it
        if raise_it:
            raise error
        return ActionResult.fail_from(action, error)

    return apply


@pytest.mark.parametrize("raise_it", [False, True], ids=["returned", "raised"])
def test_a_github_rate_limit_waits_for_its_reset_and_spends_nothing(
    sample_config, raise_it
) -> None:
    engine = _Engine(sample_config, planned=lambda: [_settle()], apply=lambda a: None)
    engine.applier.apply.side_effect = _rate_limited(engine, raise_it=raise_it)

    # Ticks every 10 minutes for 110 minutes: within the 2 h declared-wait
    # bound, each limit is waited out (45 min) and nothing is spent.
    for _ in range(12):
        engine.tick(advance=timedelta(minutes=10))

    attempts = engine.attempts_of(_settle().action_type)
    assert attempts == 3, "one attempt per reset, never while the limit holds"
    [row] = engine.store.rows.values()
    assert row.attempts == 0 and not row.parked
    assert "GitHub rate limit until" in row.last_reason


def test_a_github_rate_limit_that_never_lifts_still_parks(sample_config) -> None:
    engine = _Engine(sample_config, planned=lambda: [_settle()], apply=lambda a: None)
    engine.applier.apply.side_effect = _rate_limited(engine, raise_it=False)

    for _ in range(48):
        engine.tick(advance=timedelta(hours=1))

    assert len(engine.escalation.parked) == 1


def test_escalate_to_human_is_never_parked(sample_config) -> None:
    """A PR escalation refused for longer than any budget still lands (r4)."""
    from issue_orchestrator.control.actions import EscalateToHumanAction

    escalate = EscalateToHumanAction(issue_number=100, pr_number=200, escalation_reason="ci")
    refusing = {"on": True}
    engine = _Engine(
        sample_config,
        planned=lambda: [escalate],
        apply=lambda a: ActionResult.fail(a, "502") if refusing["on"] else ActionResult.ok(a),
    )
    for _ in range(POLICY.max_attempts * 2):
        engine.tick()
    refusing["on"] = False
    engine.tick()

    assert engine.attempts_of(escalate.action_type) == POLICY.max_attempts * 2 + 1
    assert engine.store.rows == {}


def test_a_rate_limit_whose_reset_has_passed_spends_like_any_failure(sample_config) -> None:
    """A past-due reset must not re-admit the action on every tick (r4)."""
    from issue_orchestrator.domain.host_rate_limit import HostRateLimit
    from issue_orchestrator.ports.repository_host import RepositoryHostRateLimitedError

    engine = _Engine(sample_config, planned=lambda: [_settle()], apply=lambda a: None)

    def stale_limit(action):
        error = RepositoryHostRateLimitedError("API rate limit exceeded")
        error.rate_limit = HostRateLimit(
            resets_at=engine.clock.now - timedelta(seconds=1), kind="primary"
        )
        return ActionResult.fail_from(action, error)

    engine.applier.apply.side_effect = stale_limit
    for _ in range(30):
        engine.tick(advance=timedelta(minutes=1))

    assert engine.attempts_of(_settle().action_type) == POLICY.max_attempts


def test_an_applied_action_whose_state_handler_fails_is_a_failure(sample_config) -> None:
    """The applier succeeded but recording its effect raised: the attempt did
    not succeed, and its failures must accumulate (review r5)."""
    from issue_orchestrator.control.actions import QueueReviewAction

    queue = QueueReviewAction(issue_number=42, pr_number=100, pr_url="u", branch_name="b")
    engine = _Engine(sample_config, planned=lambda: [queue], apply=lambda a: ActionResult.ok(a))
    engine.support.repository_host.create_issue_key.side_effect = RuntimeError("no key")
    for _ in range(20):
        engine.tick()

    assert engine.attempts_of(queue.action_type) == POLICY.max_attempts
    assert len(engine.escalation.parked) == 1


def test_terminal_recovery_settles_every_park_on_its_issue(sample_config) -> None:
    """Terminal recovery ends the issue's work and force-clears its block; its
    parks must leave the board with it (review r6)."""
    from issue_orchestrator.control.actions import RecoverTerminalIssueAction

    stale = RemoveLabelAction(issue_number=410, label="in-progress", reason="stale")
    recover = RecoverTerminalIssueAction(
        issue_number=410, pr_number=9, pr_url="u", status="merged", source="pull_request",
    )
    plans = {"now": [stale]}
    engine = _Engine(
        sample_config, planned=lambda: plans["now"],
        apply=lambda a: ActionResult.ok(a) if a is recover else _refused_for_pause(a),
        labels={410: ("in-progress", PAUSE)},
    )
    engine.tick()
    assert [row.key.escalation_issue for row in engine.owner.parked()] == [410]

    plans["now"] = [recover]
    engine.tick()

    assert engine.owner.parked() == ()
    assert [row.key.escalation_issue for rows in engine.escalation.released for row in rows] == [410]


def test_two_comments_on_one_issue_keep_separate_budgets(sample_config) -> None:
    """Two comments share the add_comment identity; the one that keeps
    succeeding must not clear the one that keeps failing (review r7). Fixed
    for every action type, not just comments: a success keeps the rows of
    sibling operations the same plan still names."""
    from issue_orchestrator.control.actions import AddCommentAction

    failing = AddCommentAction(number=410, comment="first finding")
    fine = AddCommentAction(number=410, comment="second finding")
    engine = _Engine(
        sample_config,
        planned=lambda: [failing, fine],
        apply=lambda a: ActionResult.fail(a, "422") if a is failing else ActionResult.ok(a),
    )
    for _ in range(20):
        engine.tick()

    applied = [call.args[0] for call in engine.applier.apply.call_args_list]
    assert sum(1 for a in applied if a is failing) == POLICY.max_attempts
    assert sum(1 for a in applied if a is fine) == 20
    assert [row.key.identity.action for row in engine.owner.parked()] == ["add_comment"]
