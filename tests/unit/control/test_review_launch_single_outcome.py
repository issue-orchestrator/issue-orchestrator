"""One review launch is one attempt and one outcome (#7454).

Exam Case U logged ``launch_session failed`` immediately after ``Launched
SessionType.REVIEW`` on the first tick after every restart: the restart put
the SAME review on its queue twice (the in-flight ledger returned the dead
reviewer's request, then the startup PR scan appended a label-derived copy the
queue's whole-object ``not in`` check did not recognise), the planner planned
one launch per queue entry, the first started the session, and the second
found no queued review and was applied as a failed launch.

These tests drive the production path end to end - the planner, the liveness
gate, the plan applier, the real ``ActionApplier``, the production launch
routing and ``LaunchSettlement``, and a real ``SessionLauncher`` - and observe
the terminal the launcher spawned and the apply events the plan published.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.action_applier import ActionApplier
from issue_orchestrator.control.completion_handler import launch_review_by_number
from issue_orchestrator.control.orchestrator_support import OrchestratorSupport
from issue_orchestrator.control.pending_session_queues import PendingSessionQueues
from issue_orchestrator.control.planner import Planner
from issue_orchestrator.control.planner_types import OrchestratorSnapshot, Plan
from issue_orchestrator.control.issue_fetch_resilience import IssueFetchResilience
from issue_orchestrator.control.scheduler import Scheduler
from issue_orchestrator.control.startup_manager import StartupManager
from issue_orchestrator.control.worktree_reconciliation import WorktreeRecoverySummary
from issue_orchestrator.control.github_workflow import launch_issue_by_number
from issue_orchestrator.control.session_routing import (
    orchestrator_launch_review_session,
    orchestrator_launch_session,
    session_launcher_callback,
)
from issue_orchestrator.control.workflows import ReviewWorkflow
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.domain.models import (
    ORCHESTRATOR_PR_MARKER,
    Issue,
    OrchestratorState,
    PendingReview,
)
from issue_orchestrator.domain.human_block import NeedsHumanCause
from issue_orchestrator.domain.pending_work import PendingWorkClaim, PendingWorkKind
from issue_orchestrator.events import EventContext, EventName
from issue_orchestrator.infra.config import AgentConfig, Config
from issue_orchestrator.ports.pull_request_tracker import PRInfo
from tests.runtime_lifecycle_helpers import make_action_applier
from tests.unit.control.liveness_doubles import gated
from tests.unit.test_session_launcher import (
    LauncherTestBundle,
    MockCommandRunner,
    MockEventSink,
    MockRepositoryHost,
    MockWorkingCopy,
    MockWorktreeManager,
    _build_launcher_bundle,
    _claims_store,
)

PR = 456
ISSUE = 123


class _RecoveryHolds:
    """The recovery owner's holds, as the launcher asks them (#7455)."""

    def __init__(self) -> None:
        self.held: set[int] = set()

    def holds_recovery(self, issue_number: int) -> bool:
        return issue_number in self.held


@pytest.fixture
def recovery_holds() -> _RecoveryHolds:
    return _RecoveryHolds()


@pytest.fixture
def launcher_bundle(tmp_path: Path, recovery_holds: _RecoveryHolds) -> LauncherTestBundle:
    """A real SessionLauncher over the launch tests' fakes, with a reviewer."""
    return _bundle(tmp_path, recovery_holds)


class _RecordedCauses:
    """The shared needs-human block's cause record, as review policy reads it."""

    def __init__(self) -> None:
        self.causes: dict[int, frozenset[NeedsHumanCause]] = {}

    def recorded_causes(self, issue_numbers):
        return {n: self.causes.get(n, frozenset()) for n in issue_numbers}

    hold_causes = recorded_causes  # every record here is the standing one

    def held_by_another_cause(self, issue_number, *, excluding):
        return bool(self.causes.get(issue_number, frozenset()) - {excluding})

    def forget_stale_causes(self):
        return ()


@pytest.fixture
def recorded_causes() -> _RecordedCauses:
    return _RecordedCauses()


def _bundle(
    tmp_path: Path, recovery_holds: _RecoveryHolds, *, provider_readiness_probe=None,
    provider_resilience=None, needs_human_block=None,
) -> LauncherTestBundle:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Test prompt")
    config = Config()
    config.repo = "test/repo"
    config.repo_root = tmp_path / "repo"
    config.repo_root.mkdir()
    config.worktree_base = tmp_path / "worktrees"
    for label in ("agent:web", "agent:reviewer"):
        config.agents[label] = AgentConfig(prompt_path=prompt_path, model="sonnet")
    config.code_review_agent = "agent:reviewer"
    config.setup_worktree = []
    return _build_launcher_bundle(
        config,
        MockEventSink(),
        MockRepositoryHost(),
        MockWorktreeManager(tmp_path),
        MockWorkingCopy(),
        MockCommandRunner(),
        recovery_holds=recovery_holds,
        provider_readiness_probe=provider_readiness_probe,
        provider_resilience=provider_resilience,
        needs_human_block=needs_human_block,
    )


class _RecordingEvents:
    def __init__(self) -> None:
        self.published: list = []

    def publish(self, event) -> None:
        self.published.append(event)

    def of(self, name: EventName) -> list[dict]:
        return [e.data for e in self.published if e.event_type is name]


def _review(*, agent_label: str | None) -> PendingReview:
    return PendingReview(
        issue_key=GitHubIssueKey(repo="test/repo", external_id=str(ISSUE)),
        pr_number=PR,
        pr_url=f"https://github.com/test/repo/pull/{PR}",
        branch_name=f"{ISSUE}-feature",
        _issue_number=ISSUE,
        agent_label=agent_label,
    )


class _Engine:
    """One planning cycle over the production launch path."""

    def __init__(self, bundle: LauncherTestBundle, state: OrchestratorState) -> None:
        self.bundle = bundle
        self.state = state
        self.events = _RecordingEvents()
        self.claims = _claims_store()
        config = bundle.launcher.config
        self.planner = Planner(
            config=config,
            scheduler=Scheduler(config),
            review_workflow=ReviewWorkflow(config=config, events=self.events),
        )
        restorer = MagicMock()
        restorer.restore_known_terminal.return_value = []

        def _launch(session_type, number):
            # The composition root's callback: the same routing the
            # Orchestrator wires (``Orchestrator.session_launcher_callback``).
            def _no_other_kind(_n):
                raise AssertionError(f"only reviews launch here, got {session_type}")

            return session_launcher_callback(
                session_type,
                number,
                lambda n: launch_issue_by_number(
                    n,
                    state.cached_queue_issues,
                    lambda issue: orchestrator_launch_session(issue, state, bundle.launcher, restorer),
                    lambda: None,
                ),
                lambda n: launch_review_by_number(
                    n,
                    state.pending_reviews,
                    lambda review: orchestrator_launch_review_session(
                        review, state, bundle.launcher, restorer, self.claims
                    ),
                ),
                _no_other_kind,
                _no_other_kind,
                _no_other_kind,
            )

        self.applier: ActionApplier = make_action_applier(
            labels=MagicMock(),
            sessions=MagicMock(),
            events=self.events,
            repository_host=MagicMock(),
            session_launcher=_launch,
        )
        self.support = OrchestratorSupport(
            config=config,
            events=self.events,
            repository_host=MagicMock(),
            state=state,
            event_context=EventContext(),
            session_manager=MagicMock(),
            action_applier=self.applier,
            fact_gatherer=MagicMock(),
            planner=self.planner,
            worktree_manager=MagicMock(),
            state_machine_manager=MagicMock(),
            cleanup_manager=MagicMock(),
            get_review_machine=MagicMock(),
            kill_session=MagicMock(),
            pending_work_claims=self.claims,
        )

    def plan(self) -> Plan:
        return self.planner.plan(
            OrchestratorSnapshot.from_state(list(self.state.cached_queue_issues), self.state)
        )

    def apply(self, plan: Plan) -> None:
        self.support.apply_plan(gated(plan), MagicMock())

    def spawned(self) -> list[str]:
        return [call["name"] for call in self.bundle.create_session_calls]


def _assert_one_launch_one_success(engine: _Engine) -> None:
    assert engine.spawned() == [f"review-{PR}"]  # exactly one launch attempt
    applied = engine.events.of(EventName.APPLY_STEP_APPLIED)
    assert [e["step_type"] for e in applied] == ["launch_session"]  # one success
    assert engine.events.of(EventName.APPLY_FAILED) == []  # and no failure
    assert engine.state.failed_this_cycle == set()
    assert [s.terminal_id for s in engine.state.active_sessions] == [f"review-{PR}"]
    assert engine.state.pending_reviews == []


def test_one_queued_review_is_one_attempt_and_one_success(
    launcher_bundle: LauncherTestBundle,
) -> None:
    state = OrchestratorState()
    assert state.queue_pending_review(_review(agent_label=None))
    engine = _Engine(launcher_bundle, state)

    plan = engine.plan()
    engine.apply(plan)

    assert [a.action_type.value for a in plan.actions] == ["launch_session"]
    _assert_one_launch_one_success(engine)


def _startup_manager(
    config: Config, *, issue_labels: tuple[str, ...] = (), pr_labels: tuple[str, ...] = (),
    needs_human_block=None,
) -> StartupManager:
    """The real startup recovery over a host that shows one PR awaiting review."""
    host = MagicMock()
    host.get_prs_with_label.return_value = [
        PRInfo(
            number=PR,
            title="Feature",
            url=f"https://github.com/test/repo/pull/{PR}",
            branch=f"{ISSUE}-feature",
            body=f"Closes #{ISSUE}\n\n{ORCHESTRATOR_PR_MARKER}",
            state="open",
            labels=["needs-code-review", *pr_labels],
        )
    ]
    host.get_issue.return_value = Issue(
        number=ISSUE, title="Feature", labels=["agent:web", *issue_labels], repo="test/repo",
        state="open",
    )
    host.create_issue_key.side_effect = lambda number: GitHubIssueKey(
        repo="test/repo", external_id=str(number)
    )
    host.list_issues.return_value = []
    reconciler = MagicMock()
    reconciler.recover.return_value = WorktreeRecoverySummary(0, 0, 0)
    runner = MagicMock()
    runner.cleanup_idle_sessions.return_value = 0
    runner.discover_running_sessions.return_value = []
    return StartupManager(
        config=config,
        events=MagicMock(),
        runner=runner,
        repository_host=host,
        action_applier=MagicMock(),
        issue_branches_fn=lambda: {},
        session_exists_fn=lambda name: False,
        restore_sessions_fn=MagicMock(),
        launch_session_fn=lambda issue: None,
        update_queue_cache_fn=lambda: None,
        issue_fetch_resilience=IssueFetchResilience("test/repo"),
        startup_worktree_reconciler=reconciler,
        pending_work_claims=MagicMock(),
        **({} if needs_human_block is None else {"needs_human_block": needs_human_block}),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("number", "cause", "on_pr", "queued"),
    [
        # A person decides before the PR merges: the review runs (#7678).
        (PR, NeedsHumanCause.MERGE_DECISION, True, True),
        # The engine escalated the PR itself: its work is held.
        (PR, NeedsHumanCause.MERGE_ESCALATION, True, False),
        # An agent's pre-work question on the issue holds the work (porchpin#262).
        (ISSUE, NeedsHumanCause.AGENT_COMPLETION, False, False),
    ],
)
async def test_startup_types_a_needs_human_before_recovering_the_review(
    launcher_bundle: LauncherTestBundle,
    recorded_causes: _RecordedCauses, number: int, cause: NeedsHumanCause, on_pr: bool,
    queued: bool,
) -> None:
    """A restart must decide the review exactly as the launch path would."""
    config = launcher_bundle.launcher.config
    config.code_review_label = "needs-code-review"
    recorded_causes.causes[number] = frozenset({cause})
    state = OrchestratorState()

    await _startup_manager(
        config,
        issue_labels=() if on_pr else ("needs-human",),
        pr_labels=("needs-human",) if on_pr else (),
        needs_human_block=recorded_causes,
    ).run_startup(state)

    assert [review.pr_number for review in state.pending_reviews] == ([PR] if queued else [])


@pytest.mark.asyncio
async def test_a_restart_that_finds_the_review_twice_queues_and_launches_it_once(
    launcher_bundle: LauncherTestBundle,
) -> None:
    """The Case U restart, through both of its admission paths.

    The dead reviewer's request comes back through the in-flight ledger's
    restore, carrying its reviewer label; then the startup PR scan finds the
    same PR by its review label and builds a label-derived copy that differs
    from it field by field. The queue must hold ONE review for the PR, and the
    next plan must launch it once.
    """
    config = launcher_bundle.launcher.config
    config.code_review_label = "needs-code-review"
    state = OrchestratorState()
    returned = _review(agent_label="agent:web")
    assert PendingSessionQueues(state).restore_deferred(
        PendingWorkClaim(PendingWorkKind.REVIEW, returned)
    )

    await _startup_manager(config).run_startup(state)

    assert state.pending_reviews == [returned]
    engine = _Engine(launcher_bundle, state)
    engine.apply(engine.plan())

    _assert_one_launch_one_success(engine)


def test_a_queue_holding_one_pr_twice_plans_and_applies_one_launch(
    launcher_bundle: LauncherTestBundle,
) -> None:
    """A queue that holds one PR twice must still cost one launch, and no failure.

    The queue owner no longer admits the duplicate; this pins the planner's
    own defence for a queue built some other way: one launch action, one
    terminal, one applied step and no failed one.
    """
    state = OrchestratorState()
    state.pending_reviews[:] = [_review(agent_label="agent:web"), _review(agent_label=None)]
    engine = _Engine(launcher_bundle, state)

    plan = engine.plan()

    assert [a.action_type.value for a in plan.actions] == ["launch_session"]

    engine.apply(plan)

    _assert_one_launch_one_success(engine)


# --- #7455: a withdrawn or held review is not a failed launch ----------------


def _host_shows(
    bundle: LauncherTestBundle, *issue_labels: str, pr_labels: tuple[str, ...] = ()
) -> None:
    """The live issue carries ``issue_labels``; its PR is open and awaits review."""
    host = bundle.launcher.repository_host
    host.labels[ISSUE] = set(issue_labels) | {"agent:web"}
    host.prs[ISSUE] = [
        PRInfo(
            number=PR,
            title="Feature",
            url=f"https://github.com/test/repo/pull/{PR}",
            branch=f"{ISSUE}-feature",
            body="",
            state="open",
            labels=["needs-code-review", *pr_labels],
        )
    ]


def _skips(engine: _Engine) -> list[str]:
    return [
        e["skip_reason"]
        for e in engine.events.of(EventName.APPLY_STEP_APPLIED)
        if e.get("result") == "skipped"
    ]


def _assert_nothing_failed(engine: _Engine) -> None:
    assert engine.spawned() == []
    assert engine.events.of(EventName.APPLY_FAILED) == []
    assert engine.state.failed_this_cycle == set()


def test_a_review_of_a_blocked_issue_is_withdrawn_not_failed(
    launcher_bundle: LauncherTestBundle,
) -> None:
    """Porchpin: a queued review of a ``blocked-failed`` issue is dropped.

    Dropping it is right; reporting it as ``launch_session failed`` and
    marking the PR number failed this cycle was not.
    """
    _host_shows(launcher_bundle, "blocked-failed")
    state = OrchestratorState()
    assert state.queue_pending_review(_review(agent_label=None))
    engine = _Engine(launcher_bundle, state)

    engine.apply(engine.plan())

    _assert_nothing_failed(engine)
    assert _skips(engine) == ["Stale pending review: issue_blocked"]
    assert state.pending_reviews == []  # withdrawn: the queue drops it


# --- #7593: an agent's own question does not withhold its PR's review --------


def test_a_merge_hold_on_the_pr_does_not_withhold_its_review(
    tmp_path: Path, recovery_holds: _RecoveryHolds, recorded_causes: _RecordedCauses
) -> None:
    """porchpin#379 (#7678): PR #379 was dropped every loop (``QUEUED → SKIP
    (stale pending review: issue_blocked)``) because the agent's question about
    the PR's merge sat on the issue. Typed as a merge hold on the PR, the
    review runs: the person decides with a reviewed PR in hand."""
    bundle = _bundle(tmp_path, recovery_holds, needs_human_block=recorded_causes)
    _host_shows(bundle, "pr-pending", pr_labels=("needs-human",))
    recorded_causes.causes[PR] = frozenset({NeedsHumanCause.MERGE_DECISION})
    state = OrchestratorState()
    assert state.queue_pending_review(_review(agent_label=None))
    engine = _Engine(bundle, state)

    engine.apply(engine.plan())

    _assert_one_launch_one_success(engine)


@pytest.mark.parametrize(
    ("causes", "extra_labels"),
    [
        # An agent's question on the ISSUE: a pre-work question holds the work.
        ({NeedsHumanCause.AGENT_COMPLETION}, ()),
        # The stuck sweep escalated it.
        ({NeedsHumanCause.AGENT_COMPLETION, NeedsHumanCause.SESSION_LIFECYCLE}, ()),
        # A tech-lead hand-over.
        ({NeedsHumanCause.AGENT_COMPLETION}, ("tech-lead-needs-human",)),
        # No recorded cause: the operator put it on by hand.
        (set(), ()),
    ],
)
def test_any_other_human_block_still_withdraws_the_review(
    tmp_path: Path, recovery_holds: _RecoveryHolds, recorded_causes: _RecordedCauses,
    causes: set[NeedsHumanCause], extra_labels: tuple[str, ...],
) -> None:
    bundle = _bundle(tmp_path, recovery_holds, needs_human_block=recorded_causes)
    _host_shows(bundle, "needs-human", *extra_labels)
    recorded_causes.causes[ISSUE] = frozenset(causes)
    state = OrchestratorState()
    assert state.queue_pending_review(_review(agent_label=None))
    engine = _Engine(bundle, state)

    engine.apply(engine.plan())

    _assert_nothing_failed(engine)
    assert _skips(engine) == ["Stale pending review: issue_blocked"]
    assert state.pending_reviews == []


def test_a_review_held_only_by_recovery_waits_then_launches_once(
    launcher_bundle: LauncherTestBundle, recovery_holds: _RecoveryHolds
) -> None:
    """Case U: the first review of a just-published PR lands inside the
    recovery owner's ``recovery-pending`` window. It must wait for the owner
    to release the hold, not be dropped, and then launch exactly once."""
    _host_shows(launcher_bundle, "recovery-pending", "pr-pending")
    recovery_holds.held.add(ISSUE)  # the owner holds it: publish not yet routed
    state = OrchestratorState()
    queued = _review(agent_label=None)
    assert state.queue_pending_review(queued)
    engine = _Engine(launcher_bundle, state)

    engine.apply(engine.plan())

    _assert_nothing_failed(engine)
    assert state.pending_reviews == [queued]  # still queued, nothing spent
    assert len(_skips(engine)) == 1

    _host_shows(launcher_bundle, "pr-pending")  # the recovery owner released it
    recovery_holds.held.clear()
    released = _Engine(launcher_bundle, state)
    released.apply(released.plan())

    assert released.spawned() == [f"review-{PR}"]
    assert released.events.of(EventName.APPLY_FAILED) == []
    assert state.pending_reviews == []


def test_recovery_with_any_other_block_still_withdraws_the_review(
    launcher_bundle: LauncherTestBundle,
) -> None:
    _host_shows(launcher_bundle, "recovery-pending", "needs-human")
    state = OrchestratorState()
    assert state.queue_pending_review(_review(agent_label=None))
    engine = _Engine(launcher_bundle, state)

    engine.apply(engine.plan())

    _assert_nothing_failed(engine)
    assert state.pending_reviews == []


def test_a_failed_review_launch_never_marks_its_pr_number_failed_this_cycle(
    launcher_bundle: LauncherTestBundle,
) -> None:
    """``failed_this_cycle`` holds ISSUE numbers; a review launch names a PR."""
    launcher_bundle.create_session_override[0] = lambda *_args: False
    state = OrchestratorState()
    assert state.queue_pending_review(_review(agent_label=None))
    engine = _Engine(launcher_bundle, state)

    engine.apply(engine.plan())

    assert [e["step_type"] for e in engine.events.of(EventName.APPLY_FAILED)] == ["launch_session"]
    assert PR not in state.failed_this_cycle


def test_a_lingering_recovery_label_the_owner_does_not_hold_withdraws_the_review(
    launcher_bundle: LauncherTestBundle,
) -> None:
    """#7455 review r3: only a hold the recovery owner confirms is a wait. A
    ``recovery-pending`` label with no retained record behind it would never be
    released by that owner, so waiting on it would retry forever."""
    _host_shows(launcher_bundle, "recovery-pending")
    state = OrchestratorState()
    assert state.queue_pending_review(_review(agent_label=None))
    engine = _Engine(launcher_bundle, state)

    engine.apply(engine.plan())

    _assert_nothing_failed(engine)
    assert state.pending_reviews == []  # withdrawn, not left to wait forever
    assert _skips(engine) == ["Stale pending review: issue_blocked"]


def test_an_unreadable_recovery_hold_is_a_retried_launch_failure_not_a_wait(
    launcher_bundle: LauncherTestBundle, recovery_holds: _RecoveryHolds
) -> None:
    def unreadable(issue_number: int) -> bool:
        raise OSError()  # no message: the failure must still be seen

    recovery_holds.holds_recovery = unreadable  # type: ignore[method-assign]
    _host_shows(launcher_bundle, "recovery-pending")
    state = OrchestratorState()
    queued = _review(agent_label=None)
    assert state.queue_pending_review(queued)
    engine = _Engine(launcher_bundle, state)

    engine.apply(engine.plan())

    assert engine.spawned() == []
    assert [e["step_type"] for e in engine.events.of(EventName.APPLY_FAILED)] == ["launch_session"]
    assert state.pending_reviews == [queued]  # retained for the next attempt


# --- #7461 review: a deferred launch is a wait, not a failed action ---------


def test_a_rate_limited_review_launch_waits_queued_and_is_not_a_failure(
    launcher_bundle: LauncherTestBundle,
) -> None:
    """The host's rate-limit window is open: the launch is retained, nothing
    is spent, and the plan step is a wait - no apply.failed, nothing failed."""
    from datetime import UTC, datetime, timedelta

    from issue_orchestrator.control.host_rate_limit_launch_gate import live_episode_keys
    from issue_orchestrator.domain.host_rate_limit import HostRateLimit, episode_key

    state = OrchestratorState()
    queued = _review(agent_label=None)
    assert state.queue_pending_review(queued)
    now = datetime.now(UTC)
    state.host_rate_limit.observe(
        HostRateLimit(resets_at=now + timedelta(minutes=30), kind="primary", resource="core"),
        now, episode_key("review", ISSUE), live=live_episode_keys(state),
    )
    engine = _Engine(launcher_bundle, state)

    engine.apply(engine.plan())

    _assert_nothing_failed(engine)
    assert state.pending_reviews == [queued]
    assert len(_skips(engine)) == 1


def test_a_provider_deferred_issue_launch_waits_and_never_marks_the_issue_failed(
    tmp_path: Path, recovery_holds: _RecoveryHolds
) -> None:
    """The provider CLI is not installed: the launch is refused before anything
    is attempted. That is a wait for a ready provider, not a failed launch that
    holds the issue out of the next tick's plan (``failed_this_cycle``)."""
    from issue_orchestrator.domain.models import Issue as ModelIssue
    from issue_orchestrator.ports.provider_readiness import (
        ProviderReadiness,
        StaticProviderReadinessProbe,
    )

    readiness = ProviderReadiness.not_installed("claude-code", "claude not on PATH")
    from tests.unit.test_provider_readiness_boundary import _manager

    bundle = _bundle(
        tmp_path, recovery_holds,
        provider_readiness_probe=StaticProviderReadinessProbe(readiness.state, readiness.detail),
        provider_resilience=_manager(MockEventSink()),
    )
    bundle.launcher.config.agents["agent:web"].provider = "claude-code"
    state = OrchestratorState()
    issue = ModelIssue(number=ISSUE, title="Feature", labels=["agent:web"], repo="test/repo")
    state.cached_queue_issues = [issue]
    state.cached_scope_issues = [issue]
    engine = _Engine(bundle, state)

    plan = engine.plan()
    assert [a.action_type.value for a in plan.actions] == ["launch_session"]
    engine.apply(plan)

    _assert_nothing_failed(engine)
    assert len(_skips(engine)) == 1
    assert state.launch_deferred_this_cycle == {ISSUE}

    # An issue has no queue to wait on: it sits out the rest of the cycle, so
    # the unchanged refusal does not repeat every tick (#7461 review r1).
    again = engine.plan()
    assert again.actions == ()
    assert [s.reason for s in again.skipped if s.number == ISSUE] == [
        "launch deferred this cycle (waiting, not failed)"
    ]

    state.launch_deferred_this_cycle.clear()  # the next refresh
    assert [a.action_type.value for a in engine.plan().actions] == ["launch_session"]
