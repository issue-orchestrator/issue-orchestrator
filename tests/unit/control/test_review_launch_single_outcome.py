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
from issue_orchestrator.control.session_routing import (
    orchestrator_launch_review_session,
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


@pytest.fixture
def launcher_bundle(tmp_path: Path) -> LauncherTestBundle:
    """A real SessionLauncher over the launch tests' fakes, with a reviewer."""
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
                _no_other_kind,
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
        return self.planner.plan(OrchestratorSnapshot.from_state([], self.state))

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


def _startup_manager(config: Config) -> StartupManager:
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
            labels=["needs-code-review"],
        )
    ]
    host.get_issue.return_value = Issue(
        number=ISSUE, title="Feature", labels=["agent:web"], repo="test/repo", state="open"
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
    )


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
