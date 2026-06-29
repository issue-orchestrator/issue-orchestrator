"""Unit tests for the Planner.

These tests verify the planner's pure policy logic without any external dependencies.
The planner decides "should we?" - no mocks for tmux/GitHub needed.
"""

import pytest
import logging
from pathlib import Path
from unittest.mock import Mock, MagicMock

from issue_orchestrator.infra.config import Config
from issue_orchestrator.control.planner import (
    Planner,
)
from issue_orchestrator.control.planner_types import (
    Plan,
    OrchestratorSnapshot,
    SkippedItem,
)
from issue_orchestrator.control.scheduler import Scheduler
from issue_orchestrator.control.dependency_evaluator import DependencyEvaluator
from issue_orchestrator.control.actions import (
    ActionType,
    LaunchSessionAction,
    RecoverTerminalIssueAction,
    SessionType,
    SyncLabelsAction,
    RemoveLabelAction,
)
from issue_orchestrator.domain.models import (
    Issue,
    Session,
    SessionStatus,
    PendingReview,
    PendingRetrospectiveReview,
    PendingRework,
    PendingTriageReview,
    PendingValidationRetry,
    AgentConfig,
    CompletionRecord,
    CompletionOutcome,
    DiscoveredAwaitingMergeDrift,
    DiscoveredAwaitingMergeReconciliation,
    DiscoveredRetrospectiveReview,
    RequestedAction,
    ObservedCompletion,
    SessionIdentity,
    WorktreeLocation,
)

from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.control.provider_resilience import ProviderResilienceManager
from issue_orchestrator.ports import InMemoryProviderCircuitStore
from tests.unit.session_run_helpers import make_session_run_assets


def make_config(**kwargs) -> Config:
    """Create a test config with sensible defaults."""
    defaults = {
        "repo": "test/repo",
        "max_concurrent_sessions": 3,
    }
    defaults.update(kwargs)
    return Config(**defaults)


def make_issue(number: int, title: str = "Test issue", **kwargs) -> Issue:
    """Create a test issue."""
    defaults = {
        "number": number,
        "title": title,
        "body": "",
        "labels": [],
        "state": "open",
        "milestone": None,
        "milestone_number": None,
        "milestone_due_on": None,
    }
    defaults.update(kwargs)
    return Issue(**defaults)


def make_session(issue: Issue, task: TaskKind = TaskKind.CODE) -> Session:
    """Create a test session for an issue."""
    from pathlib import Path
    from datetime import datetime

    agent_config = AgentConfig(
        prompt_path=Path("/tmp/test.md"),
    )
    issue_key = FakeIssueKey(name=str(issue.number))
    session_key = SessionKey(issue=issue_key, task=task)
    return Session(
        key=session_key,
        issue=issue,
        agent_config=agent_config,
        terminal_id=f"issue-{issue.number}",
        worktree_path=Path(f"/tmp/worktree-{issue.number}"),
        branch_name=f"issue-{issue.number}",
        run_assets=make_session_run_assets(
            Path(f"/tmp/worktree-{issue.number}"),
            session_name=f"issue-{issue.number}",
        ),
        started_at=datetime.now(),
        status=SessionStatus.RUNNING,
    )


def make_snapshot(
    issues: list[Issue] | None = None,
    active_sessions: list[Session] | None = None,
    pending_reviews: list[PendingReview] | None = None,
    pending_reworks: list[PendingRework] | None = None,
    pending_triage: list[PendingTriageReview] | None = None,
    paused: bool = False,
    **kwargs,
) -> OrchestratorSnapshot:
    """Create a test snapshot."""
    return OrchestratorSnapshot(
        issues=tuple(issues or []),
        active_sessions=tuple(active_sessions or []),
        pending_reviews=tuple(pending_reviews or []),
        pending_reworks=tuple(pending_reworks or []),
        pending_triage=tuple(pending_triage or []),
        paused=paused,
        **kwargs,
    )


class StaticIssueChecker:
    """Issue state checker for dependency-gating planner tests."""

    def __init__(self, states: dict[int, str], milestone: str | None = "M1"):
        self.states = states
        self.milestone = milestone

    def get_issue_state(self, issue_number: int, repo: str | None = None) -> str | None:
        return self.states.get(issue_number)

    def get_issue_milestone(self, issue_number: int, repo: str | None = None) -> str | None:
        return self.milestone


class TestPlanEmpty:
    """Tests for empty plan scenarios."""

    def test_empty_plan_when_paused(self):
        """Planner returns empty plan when orchestrator is paused."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        snapshot = make_snapshot(
            issues=[make_issue(1), make_issue(2)],
            paused=True,
        )

        plan = planner.plan(snapshot)

        assert plan.action_count == 0
        assert len(plan.skipped) == 0


class TestProviderResilienceLabels:
    def test_removes_label_when_review_rework_triage_providers_recover(self):
        config = make_config()
        config.code_review_agent = "agent:reviewer"
        config.triage_review_agent = "agent:triage"
        config.agents["agent:web"] = AgentConfig(prompt_path=Path("/tmp/web.md"), provider=None)
        config.agents["agent:reviewer"] = AgentConfig(prompt_path=Path("/tmp/review.md"), provider="review-provider")
        config.agents["agent:fixer"] = AgentConfig(prompt_path=Path("/tmp/fix.md"), provider="rework-provider")
        config.agents["agent:triage"] = AgentConfig(prompt_path=Path("/tmp/triage.md"), provider="triage-provider")

        scheduler = Scheduler(config)
        provider_resilience = ProviderResilienceManager(
            config.provider_resilience,
            store=InMemoryProviderCircuitStore(),
            events=MagicMock(),
        )
        planner = Planner(config=config, scheduler=scheduler, provider_resilience=provider_resilience)

        issue1 = make_issue(1, labels=["agent:web", config.get_label_provider_unavailable()])
        issue2 = make_issue(2, labels=["agent:web", config.get_label_provider_unavailable()])
        issue3 = make_issue(3, labels=["agent:web", config.get_label_provider_unavailable()])

        pending_review = PendingReview(
            issue_key=FakeIssueKey(name="1"),
            pr_number=101,
            pr_url="https://example.com/pr/101",
            branch_name="branch-101",
            _issue_number=1,
            agent_label=None,
        )
        pending_rework = PendingRework(
            issue_key=FakeIssueKey(name="2"),
            agent_type="agent:fixer",
            rework_cycle=1,
            issue_number=2,
        )
        pending_triage = PendingTriageReview(issue_number=3, title="Triage 3")

        snapshot = make_snapshot(
            issues=[issue1, issue2, issue3],
            pending_reviews=[pending_review],
            pending_reworks=[pending_rework],
            pending_triage=[pending_triage],
        )

        plan = planner.plan(snapshot)

        removed = [a for a in plan.actions if getattr(a, "action_type", None) == ActionType.REMOVE_LABEL]
        removed_numbers = {a.issue_number for a in removed}
        assert removed_numbers == {1, 2, 3}

    def test_empty_plan_when_at_capacity(self):
        """Planner returns empty plan when at max capacity."""
        config = make_config(max_concurrent_sessions=2)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        issue1 = make_issue(1)
        issue2 = make_issue(2)
        snapshot = make_snapshot(
            issues=[make_issue(3), make_issue(4)],
            active_sessions=[make_session(issue1), make_session(issue2)],
        )

        plan = planner.plan(snapshot)

        assert plan.action_count == 0

    def test_empty_plan_when_no_issues(self):
        """Planner returns empty plan when no issues available."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        snapshot = make_snapshot(issues=[])

        plan = planner.plan(snapshot)

        assert plan.action_count == 0


class TestPlanIssues:
    """Tests for issue planning logic."""

    def test_plans_single_issue_launch(self):
        """Planner creates action to launch a single available issue."""
        config = make_config(max_concurrent_sessions=3)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        snapshot = make_snapshot(issues=[make_issue(42)])

        plan = planner.plan(snapshot)

        assert plan.action_count == 1
        action = plan.actions[0]
        assert isinstance(action, LaunchSessionAction)
        assert action.session_type == SessionType.ISSUE
        assert action.number == 42

    def test_plans_multiple_issues_up_to_capacity(self):
        """Planner creates actions for issues up to remaining capacity."""
        config = make_config(max_concurrent_sessions=3)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        # 1 session running, 2 slots available
        issue1 = make_issue(1)
        snapshot = make_snapshot(
            issues=[make_issue(2), make_issue(3), make_issue(4)],
            active_sessions=[make_session(issue1)],
        )

        plan = planner.plan(snapshot)

        # Should only plan 2 issues (remaining capacity)
        assert plan.action_count == 2
        issue_actions = plan.actions_of_type(ActionType.LAUNCH_SESSION)
        assert len(issue_actions) == 2

    def test_skips_already_active_issues(self):
        """Planner doesn't plan actions for issues already being worked."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        issue1 = make_issue(1)
        snapshot = make_snapshot(
            issues=[issue1, make_issue(2)],
            active_sessions=[make_session(issue1)],
        )

        plan = planner.plan(snapshot)

        # Should only plan issue 2, not issue 1 (already active)
        assert plan.action_count == 1
        assert plan.actions[0].number == 2


class TestObservedCompletionLabels:
    """Tests for immediate label projection on observed completions."""

    def test_completed_completion_adds_pr_pending_when_publish_needed(self):
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        record = CompletionRecord(
            session_id="session-1",
            timestamp="2026-01-01T00:00:00",
            outcome=CompletionOutcome.COMPLETED,
            summary="done",
            requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
        )
        observed = ObservedCompletion(
            identity=SessionIdentity(
                issue_number=42,
                issue_title="Test Issue",
                session_key="code:42",
                terminal_id="issue-42",
            ),
            worktree=WorktreeLocation(
                path="/tmp/worktree-42",
                branch_name="issue-42",
                completion_path=".issue-orchestrator/completion.json",
            ),
            record=record,
            run_assets=make_session_run_assets(
                Path("/tmp/worktree-42"),
                session_name="issue-42",
            ),
        )

        snapshot = make_snapshot(
            issues=[],
            observed_completions=(observed,),
        )
        plan = planner.plan(snapshot)

        add_labels = [a for a in plan.actions if getattr(a, "label", None) == "pr-pending"]
        assert len(add_labels) == 1

    def test_respects_max_issues_to_start(self):
        """Planner respects the max_issues_to_start limit."""
        config = make_config(max_concurrent_sessions=5)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        snapshot = make_snapshot(
            issues=[make_issue(1), make_issue(2), make_issue(3)],
            max_issues_to_start=2,
            issues_started_count=1,  # Already started 1
        )

        plan = planner.plan(snapshot)

        # Should only plan 1 more issue (max=2, started=1)
        assert plan.action_count == 1


class TestPlanValidationRetries:
    """Tests for validation retry planning."""

    def test_plans_validation_retry_when_max_issues_to_start_reached(self):
        """Validation retries continue existing work and bypass max_to_start."""
        config = make_config(max_concurrent_sessions=2)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        snapshot = make_snapshot(
            issues=[make_issue(1), make_issue(2)],
            pending_validation_retries=[
                PendingValidationRetry(
                    issue_number=1,
                    issue_title="Retry me",
                    agent_label="agent:developer",
                    worktree_path="/tmp/repo-1",
                    branch_name="1-retry",
                    original_prompt="original task",
                    validation_error="dirty worktree",
                    validation_error_file=None,
                    retry_count=1,
                    source_task=TaskKind.CODE,
                    validation_cmd="make test",
                ),
            ],
            max_issues_to_start=1,
            issues_started_count=1,
        )

        plan = planner.plan(snapshot)

        retry_actions = plan.actions_of_type(ActionType.LAUNCH_VALIDATION_RETRY)
        issue_actions = [
            action for action in plan.actions_of_type(ActionType.LAUNCH_SESSION)
            if action.session_type == SessionType.ISSUE
        ]
        assert len(retry_actions) == 1
        assert retry_actions[0].issue_number == 1
        assert issue_actions == []

    def test_skips_validation_retry_for_active_issue(self):
        """Validation retry planner does not double-launch an active issue."""
        config = make_config(max_concurrent_sessions=2)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)
        issue = make_issue(1)

        snapshot = make_snapshot(
            issues=[issue],
            active_sessions=[make_session(issue)],
            pending_validation_retries=[
                PendingValidationRetry(
                    issue_number=1,
                    issue_title="Retry me",
                    agent_label="agent:developer",
                    worktree_path="/tmp/repo-1",
                    branch_name="1-retry",
                    original_prompt="original task",
                    validation_error="dirty worktree",
                    validation_error_file=None,
                    retry_count=1,
                    source_task=TaskKind.CODE,
                    validation_cmd="make test",
                ),
            ],
        )

        plan = planner.plan(snapshot)

        assert plan.actions_of_type(ActionType.LAUNCH_VALIDATION_RETRY) == []
        assert plan.skipped[0].item_type == "validation_retry"
        assert plan.skipped[0].reason == "active session running"


class TestPlanReviews:
    """Tests for review planning with workflow."""

    def test_plans_reviews_when_workflow_configured(self):
        """Planner uses ReviewWorkflow to decide on reviews."""
        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)

        # Mock the review workflow
        mock_workflow = Mock()
        mock_workflow.is_configured.return_value = True
        mock_decision = Mock()
        mock_decision.should_launch = True
        mock_decision.skip_reason = None
        mock_decision.reviews_to_launch = [
            PendingReview(issue_key=FakeIssueKey(name="1"), pr_number=100, pr_url="url", branch_name="branch", _issue_number=1),
        ]
        mock_workflow.should_launch_reviews.return_value = mock_decision

        planner = Planner(
            config=config,
            scheduler=scheduler,
            review_workflow=mock_workflow,
        )

        snapshot = make_snapshot(
            pending_reviews=[
                PendingReview(issue_key=FakeIssueKey(name="1"), pr_number=100, pr_url="url", branch_name="branch", _issue_number=1),
            ],
        )

        plan = planner.plan(snapshot)

        # Should have review launch action
        review_actions = [a for a in plan.actions if a.session_type == SessionType.REVIEW]
        assert len(review_actions) == 1
        assert review_actions[0].number == 100

    def test_plans_retrospective_reviews_when_workflow_configured(self):
        """Planner uses RetrospectiveReviewWorkflow for review-first launches."""
        config = make_config(
            code_review_agent="agent:reviewer",
            retrospective_review_enabled=True,
        )
        scheduler = Scheduler(config)
        pending = PendingRetrospectiveReview(
            issue_key=FakeIssueKey(name="365"),
            issue_number=365,
            issue_title="Review existing work",
            agent_label="agent:web",
            trigger_label="lack-of-review-redo",
        )

        mock_workflow = Mock()
        mock_workflow.is_configured.return_value = True
        mock_decision = Mock()
        mock_decision.should_launch = True
        mock_decision.skip_reason = None
        mock_decision.reviews_to_launch = [pending]
        mock_workflow.should_launch_reviews.return_value = mock_decision

        planner = Planner(
            config=config,
            scheduler=scheduler,
            retrospective_review_workflow=mock_workflow,
        )

        plan = planner.plan(make_snapshot(pending_retrospective_reviews=[pending]))

        launch_actions = plan.actions_of_type(ActionType.LAUNCH_SESSION)
        assert len(launch_actions) == 1
        assert launch_actions[0].session_type == SessionType.RETROSPECTIVE_REVIEW
        assert launch_actions[0].number == 365

    def test_discovered_retrospective_review_queues_and_blocks_code_launch(self):
        """Trigger-labeled issues queue retrospective review instead of coding."""
        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)
        issue = make_issue(365, labels=["agent:web", "lack-of-review-redo"])
        discovered = DiscoveredRetrospectiveReview(
            issue_number=365,
            issue_title="Review existing work",
            agent_label="agent:web",
            trigger_label="lack-of-review-redo",
            issue_key="365",
            prior_pr_number=512,
            prior_pr_url="https://github.com/test/repo/pull/512",
        )

        plan = planner.plan(
            make_snapshot(
                issues=[issue],
                discovered_retrospective_reviews=[discovered],
            )
        )

        queue_actions = plan.actions_of_type(ActionType.QUEUE_RETROSPECTIVE_REVIEW)
        launch_actions = plan.actions_of_type(ActionType.LAUNCH_SESSION)
        assert len(queue_actions) == 1
        assert queue_actions[0].issue_number == 365
        assert queue_actions[0].prior_pr_number == 512
        assert launch_actions == []
        assert any(
            skipped.item_type == "issue"
            and skipped.number == 365
            and skipped.reason == "pending retrospective review"
            for skipped in plan.skipped
        )


class TestPlanHelpers:
    """Tests for Plan and snapshot helper methods."""

    def test_plan_has_action_for(self):
        """Plan.has_action_for correctly identifies planned actions."""
        plan = Plan(
            actions=(
                LaunchSessionAction(session_type=SessionType.ISSUE, number=42, command="", working_dir=""),
            ),
            skipped=(),
        )

        assert plan.has_action_for(42) is True
        assert plan.has_action_for(99) is False

    def test_snapshot_active_issue_numbers(self):
        """Snapshot correctly computes active issue numbers."""
        issue1 = make_issue(1)
        issue2 = make_issue(2)

        snapshot = make_snapshot(
            issues=[issue1, issue2, make_issue(3)],
            active_sessions=[make_session(issue1), make_session(issue2)],
        )

        assert snapshot.active_issue_numbers == frozenset({1, 2})

    def test_skipped_item_creation(self):
        """SkippedItem correctly records skipped items."""
        item = SkippedItem(
            item_type="issue",
            number=42,
            reason="dependency: blocked by #41",
        )

        assert item.item_type == "issue"
        assert item.number == 42
        assert "blocked" in item.reason


class TestPlannerDependencyGating:
    """Regression coverage for planner/scheduler dependency wiring."""

    def test_planner_dependency_evaluator_blocks_unsatisfied_dependency(self):
        config = make_config(max_concurrent_sessions=1)
        evaluator = DependencyEvaluator(
            issue_checker=StaticIssueChecker({100: "open"}),
            events=Mock(),
        )
        scheduler = Scheduler(config, dependency_evaluator=evaluator)
        planner = Planner(config=config, scheduler=scheduler, dependency_evaluator=evaluator)

        snapshot = make_snapshot(
            issues=[
                make_issue(
                    2,
                    title="Dependent issue",
                    body="Depends-on: #100",
                    milestone="M1",
                ),
            ],
        )

        plan = planner.plan(snapshot)

        assert plan.actions_of_type(ActionType.LAUNCH_SESSION) == []
        assert plan.skipped == (
            SkippedItem(
                item_type="issue",
                number=2,
                reason="dependency: Blocked - waiting on: #100",
            ),
        )
        assert scheduler.dependency_evaluator is evaluator

    def test_planner_rejects_dependency_evaluator_without_scheduler_wiring(self):
        config = make_config()
        evaluator = DependencyEvaluator(
            issue_checker=StaticIssueChecker({100: "open"}),
            events=Mock(),
        )
        scheduler = Scheduler(config)

        with pytest.raises(ValueError, match="Scheduler dependency evaluator is required"):
            Planner(config=config, scheduler=scheduler, dependency_evaluator=evaluator)

        assert scheduler.dependency_evaluator is None

    def test_planner_uses_scheduler_dependency_evaluator_when_not_passed_directly(self):
        config = make_config(max_concurrent_sessions=1)
        evaluator = DependencyEvaluator(
            issue_checker=StaticIssueChecker({100: "open"}),
            events=Mock(),
        )
        scheduler = Scheduler(config, dependency_evaluator=evaluator)
        planner = Planner(config=config, scheduler=scheduler)

        snapshot = make_snapshot(
            issues=[
                make_issue(
                    2,
                    title="Dependent issue",
                    body="Depends-on: #100",
                    milestone="M1",
                ),
            ],
        )

        plan = planner.plan(snapshot)

        assert plan.actions_of_type(ActionType.LAUNCH_SESSION) == []
        assert planner.dependency_evaluator is evaluator

    def test_planner_rejects_mismatched_scheduler_dependency_evaluator(self):
        config = make_config()
        scheduler_evaluator = DependencyEvaluator(
            issue_checker=StaticIssueChecker({100: "open"}),
            events=Mock(),
        )
        planner_evaluator = DependencyEvaluator(
            issue_checker=StaticIssueChecker({100: "open"}),
            events=Mock(),
        )
        scheduler = Scheduler(config, dependency_evaluator=scheduler_evaluator)

        with pytest.raises(ValueError, match="dependency evaluators"):
            Planner(
                config=config,
                scheduler=scheduler,
                dependency_evaluator=planner_evaluator,
            )


class TestExplainSkip:
    """Tests for the explain_skip method."""

    def test_explain_skip_already_active(self):
        """Explains why an already-active issue is skipped."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        issue1 = make_issue(1)
        snapshot = make_snapshot(
            issues=[issue1],
            active_sessions=[make_session(issue1)],
        )

        reason = planner.explain_skip(1, snapshot)

        assert "active session" in reason.lower()

    def test_explain_skip_paused(self):
        """Explains that orchestrator is paused."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        snapshot = make_snapshot(
            issues=[make_issue(1)],
            paused=True,
        )

        reason = planner.explain_skip(1, snapshot)

        assert "paused" in reason.lower()

    def test_explain_skip_at_capacity(self):
        """Explains that system is at capacity."""
        config = make_config(max_concurrent_sessions=1)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        issue1 = make_issue(1)
        snapshot = make_snapshot(
            issues=[make_issue(2)],
            active_sessions=[make_session(issue1)],
        )

        reason = planner.explain_skip(2, snapshot)

        assert "capacity" in reason.lower()


class TestPlanDiscoveredReviews:
    """Tests for Planner's _plan_discovered_reviews method.

    This method processes DiscoveredReview facts from session completions
    and produces QueueReviewAction for the orchestrator to apply.
    """

    def test_plans_queue_action_for_discovered_review(self):
        """Planner produces QueueReviewAction for discovered reviews."""
        from issue_orchestrator.domain.models import DiscoveredReview
        from issue_orchestrator.control.actions import ActionType

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredReview(
            issue_number=42,
            pr_number=100,
            pr_url="https://github.com/test/repo/pull/100",
            branch_name="feature/issue-42",
        )

        snapshot = make_snapshot(
            issues=[
                make_issue(
                    42,
                    labels=["agent:web", "verbose"],
                )
            ],
            discovered_reviews=(discovered,),
            pending_reviews=(),  # Not already queued
        )

        plan = planner.plan(snapshot)

        # Should have a QueueReviewAction
        queue_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_REVIEW]
        assert len(queue_actions) == 1
        action = queue_actions[0]
        assert action.issue_number == 42
        assert action.pr_number == 100
        assert action.pr_url == "https://github.com/test/repo/pull/100"
        assert action.branch_name == "feature/issue-42"
        assert action.issue_labels == ("agent:web", "verbose")

    def test_skips_already_queued_reviews(self):
        """Planner skips discovered reviews that are already in pending_reviews."""
        from issue_orchestrator.domain.models import DiscoveredReview
        from issue_orchestrator.control.actions import ActionType

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredReview(
            issue_number=42,
            pr_number=100,
            pr_url="https://github.com/test/repo/pull/100",
            branch_name="feature/issue-42",
        )

        # Already queued in pending_reviews
        already_pending = PendingReview(
            issue_key=FakeIssueKey(name="42"),
            pr_number=100,
            pr_url="https://github.com/test/repo/pull/100",
            branch_name="feature/issue-42",
            _issue_number=42,
        )

        snapshot = make_snapshot(
            discovered_reviews=(discovered,),
            pending_reviews=(already_pending,),
        )

        plan = planner.plan(snapshot)

        # Should NOT have a QueueReviewAction for already-queued PR
        queue_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_REVIEW]
        assert len(queue_actions) == 0

    def test_no_queue_actions_when_no_discovered_reviews(self):
        """Planner produces no queue actions when no discovered reviews."""
        from issue_orchestrator.control.actions import ActionType

        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        snapshot = make_snapshot(
            discovered_reviews=(),  # Empty
        )

        plan = planner.plan(snapshot)

        queue_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_REVIEW]
        assert len(queue_actions) == 0

    def test_queue_action_has_expected_state(self):
        """Planner attaches ExpectedState to QueueReviewAction."""
        from issue_orchestrator.domain.models import DiscoveredReview
        from issue_orchestrator.control.actions import ActionType

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredReview(
            issue_number=42,
            pr_number=100,
            pr_url="https://github.com/test/repo/pull/100",
            branch_name="feature/issue-42",
        )

        snapshot = make_snapshot(
            discovered_reviews=(discovered,),
            pending_reviews=(),
        )

        plan = planner.plan(snapshot)

        queue_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_REVIEW]
        assert len(queue_actions) == 1
        action = queue_actions[0]

        # Should have ExpectedState attached
        assert action.expected is not None
        # Should forbid the pause label
        assert "io:needs-reconcile" in action.expected.forbidden_labels

    def test_add_label_action_has_expected_state(self):
        """Planner attaches ExpectedState to AddLabelAction for pr-pending."""
        from issue_orchestrator.domain.models import DiscoveredReview
        from issue_orchestrator.control.actions import ActionType

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredReview(
            issue_number=42,
            pr_number=100,
            pr_url="https://github.com/test/repo/pull/100",
            branch_name="feature/issue-42",
        )

        snapshot = make_snapshot(
            discovered_reviews=(discovered,),
            pending_reviews=(),
        )

        plan = planner.plan(snapshot)

        # Find the AddLabelAction for pr-pending
        add_label_actions = [
            a for a in plan.actions
            if a.action_type == ActionType.ADD_LABEL and a.label == "pr-pending"
        ]
        assert len(add_label_actions) == 1
        action = add_label_actions[0]

        # Should have ExpectedState attached
        assert action.expected is not None
        # Should forbid the pause label
        assert "io:needs-reconcile" in action.expected.forbidden_labels


class TestPlanAwaitingMergeReconciliations:
    """Tests for planning awaiting-merge history reconciliation facts."""

    def test_plans_terminal_recovery_action(self):
        """A terminal (non-drift) reconciliation plans a single owner command
        that sheds labels then finalizes history — not a standalone history
        reconciliation that could be terminalized before cleanup."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)
        discovered = DiscoveredAwaitingMergeReconciliation(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="merged",
            status_reason="PR merged; awaiting merge reconciled",
            source="pull_request",
            issue_key="M1-228",
        )

        snapshot = make_snapshot(
            discovered_awaiting_merge_reconciliations=(discovered,),
        )

        plan = planner.plan(snapshot)

        # No standalone history reconciliation for the terminal path — the
        # owner command finalizes history internally, after the shed succeeds.
        assert plan.actions_of_type(ActionType.RECONCILE_HISTORY_ENTRY) == []
        actions = plan.actions_of_type(ActionType.RECOVER_TERMINAL_ISSUE)
        assert len(actions) == 1
        action = actions[0]
        assert isinstance(action, RecoverTerminalIssueAction)
        assert action.issue_number == 228
        assert action.pr_number == 318
        assert action.pr_url == "https://github.com/test/repo/pull/318"
        assert action.status == "merged"
        assert action.status_reason == "PR merged; awaiting merge reconciled"
        assert action.source == "pull_request"
        assert action.issue_key == "M1-228"
        # Carries the reconciliation pause guard the old terminal-cleanup
        # RemoveLabelAction used to carry: a paused issue (io:needs-reconcile)
        # must not be shed or finalized behind fail-closed drift handling. The
        # applier enforces this at the owner-command boundary (#6431 F1).
        assert action.expected is not None
        assert "io:needs-reconcile" in action.expected.forbidden_labels

    def test_terminal_pr_merged_reconciliation_recovers_terminal_issue(self):
        """When a PR is observed merged, plan one RecoverTerminalIssueAction
        that sheds the issue's transient workflow labels (pr-pending,
        publish-failed, publish-fail-count-N, blocking) and then finalizes
        history. The applier reads the issue's live labels to pick the exact
        set and cleans both GitHub and the local label_store mirror.
        """
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)
        discovered = DiscoveredAwaitingMergeReconciliation(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="merged",
            status_reason="PR merged; awaiting merge reconciled",
            source="pull_request",
            issue_key="M1-228",
        )

        snapshot = make_snapshot(
            discovered_awaiting_merge_reconciliations=(discovered,),
        )

        plan = planner.plan(snapshot)

        recover_actions = [
            a for a in plan.actions_of_type(ActionType.RECOVER_TERMINAL_ISSUE)
            if isinstance(a, RecoverTerminalIssueAction)
            and a.issue_number == 228
        ]
        assert len(recover_actions) == 1
        action = recover_actions[0]
        assert action.issue_key == "M1-228"
        assert action.status == "merged"
        assert "merged" in action.reason

    def test_terminal_issue_closed_reconciliation_recovers_terminal_issue(self):
        """When the parent issue is closed (regardless of PR state), the same
        owner command applies: shed every transient workflow label, then
        finalize history.
        """
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)
        discovered = DiscoveredAwaitingMergeReconciliation(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="closed",
            status_reason="Issue closed; awaiting merge reconciled",
            source="issue",
            issue_key="M1-228",
        )

        snapshot = make_snapshot(
            discovered_awaiting_merge_reconciliations=(discovered,),
        )

        plan = planner.plan(snapshot)

        recover_actions = [
            a for a in plan.actions_of_type(ActionType.RECOVER_TERMINAL_ISSUE)
            if isinstance(a, RecoverTerminalIssueAction)
            and a.issue_number == 228
        ]
        assert len(recover_actions) == 1

    def test_terminal_reconciliation_dedupes_against_drift(self):
        """When a drift action already removes pr-pending for the same
        issue (PR closed but issue still open), don't shed: the drift is
        ADDING blocked:pr-closed, so shedding blocking labels would
        contradict it. The drift path finalizes history on its own (standalone
        ReconcileHistoryEntryAction) and the SyncLabelsAction owns pr-pending
        removal — no RecoverTerminalIssueAction is planned.
        """
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)
        reconciliation = DiscoveredAwaitingMergeReconciliation(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status="closed",
            status_reason="PR closed",
            source="pull_request",
            issue_key="M1-228",
        )
        drift = DiscoveredAwaitingMergeDrift(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status_reason="PR closed; issue remains open",
            issue_key="M1-228",
        )

        snapshot = make_snapshot(
            discovered_awaiting_merge_reconciliations=(reconciliation,),
            discovered_awaiting_merge_drifts=(drift,),
        )

        plan = planner.plan(snapshot)

        recover_actions = [
            a for a in plan.actions_of_type(ActionType.RECOVER_TERMINAL_ISSUE)
            if isinstance(a, RecoverTerminalIssueAction)
            and a.issue_number == 228
        ]
        assert recover_actions == [], (
            "Drift ADDS blocked:pr-closed; shedding blocking labels would "
            "contradict the drift. Only history finalize + SyncLabelsAction "
            "should act."
        )
        # Drift still finalizes its history entry on its own.
        history_actions = [
            a for a in plan.actions_of_type(ActionType.RECONCILE_HISTORY_ENTRY)
            if a.issue_number == 228
        ]
        assert len(history_actions) == 1
        sync_actions = plan.actions_of_type(ActionType.SYNC_LABELS)
        assert len(sync_actions) == 1
        assert "pr-pending" in sync_actions[0].remove_labels

    def test_plans_pr_closed_drift_label_sync_action(self):
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)
        drift = DiscoveredAwaitingMergeDrift(
            issue_number=228,
            pr_number=318,
            pr_url="https://github.com/test/repo/pull/318",
            status_reason="PR closed; issue remains open",
            issue_key="M1-228",
        )

        snapshot = make_snapshot(
            discovered_awaiting_merge_drifts=(drift,),
        )

        plan = planner.plan(snapshot)

        actions = plan.actions_of_type(ActionType.SYNC_LABELS)
        assert len(actions) == 1
        action = actions[0]
        assert isinstance(action, SyncLabelsAction)
        assert action.issue_number == 228
        assert action.add_labels == ("blocked:pr-closed",)
        assert action.remove_labels == ("pr-pending",)
        assert action.issue_key == "M1-228"
        assert action.reason == "PR closed; issue remains open"
        assert action.expected is not None
        assert action.expected.required_labels == frozenset({"pr-pending"})


class TestPlanTriageIssueCreation:
    """Tests for Planner's _plan_triage_issue_creation method.

    This method processes TriageFacts and produces CreateTriageIssueAction
    when the threshold is met and no existing triage issue exists.
    """

    def test_creates_triage_issue_at_threshold(self):
        """Planner produces CreateTriageIssueAction when threshold is met."""
        from issue_orchestrator.domain.models import TriageFacts
        from issue_orchestrator.control.actions import ActionType

        config = make_config(
            triage_review_agent="agent:triage",
            triage_review_threshold=3,
            triage_reviewed_label="triage-reviewed",
        )
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        triage_facts = TriageFacts(
            pr_count=3,
            threshold=3,
            existing_triage_issue=None,
            watch_label="code-reviewed",
            prs=((1, "PR 1"), (2, "PR 2"), (3, "PR 3")),
        )

        snapshot = make_snapshot(
            triage_facts=triage_facts,
        )

        plan = planner.plan(snapshot)

        create_actions = [a for a in plan.actions if a.action_type == ActionType.CREATE_TRIAGE_ISSUE]
        assert len(create_actions) == 1
        action = create_actions[0]
        assert "Triage Batch Review" in action.title
        assert action.pr_count == 3
        assert "agent:triage" in action.labels

    def test_no_triage_issue_below_threshold(self):
        """Planner produces no CreateTriageIssueAction when below threshold."""
        from issue_orchestrator.domain.models import TriageFacts
        from issue_orchestrator.control.actions import ActionType

        config = make_config(
            triage_review_agent="agent:triage",
            triage_review_threshold=5,
        )
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        triage_facts = TriageFacts(
            pr_count=2,  # Below threshold of 5
            threshold=5,
            existing_triage_issue=None,
            watch_label="code-reviewed",
            prs=((1, "PR 1"), (2, "PR 2")),
        )

        snapshot = make_snapshot(
            triage_facts=triage_facts,
        )

        plan = planner.plan(snapshot)

        create_actions = [a for a in plan.actions if a.action_type == ActionType.CREATE_TRIAGE_ISSUE]
        assert len(create_actions) == 0

    def test_no_triage_issue_when_existing_issue(self):
        """Planner produces no CreateTriageIssueAction when existing issue exists."""
        from issue_orchestrator.domain.models import TriageFacts
        from issue_orchestrator.control.actions import ActionType

        config = make_config(
            triage_review_agent="agent:triage",
            triage_review_threshold=3,
        )
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        triage_facts = TriageFacts(
            pr_count=5,  # Above threshold
            threshold=3,
            existing_triage_issue=100,  # Already exists!
            watch_label="code-reviewed",
            prs=tuple(),
        )

        snapshot = make_snapshot(
            triage_facts=triage_facts,
        )

        plan = planner.plan(snapshot)

        create_actions = [a for a in plan.actions if a.action_type == ActionType.CREATE_TRIAGE_ISSUE]
        assert len(create_actions) == 0

    def test_no_triage_issue_when_no_facts(self):
        """Planner produces no CreateTriageIssueAction when triage_facts is None."""
        from issue_orchestrator.control.actions import ActionType

        config = make_config()  # No triage config
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        snapshot = make_snapshot(
            triage_facts=None,  # No facts gathered
        )

        plan = planner.plan(snapshot)

        create_actions = [a for a in plan.actions if a.action_type == ActionType.CREATE_TRIAGE_ISSUE]
        assert len(create_actions) == 0

    def test_triage_issue_body_includes_pr_list(self):
        """Planner includes PR details in triage issue body."""
        from issue_orchestrator.domain.models import TriageFacts
        from issue_orchestrator.control.actions import ActionType

        config = make_config(
            triage_review_agent="agent:triage",
            triage_review_threshold=2,
            triage_reviewed_label="triage-reviewed",
        )
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        triage_facts = TriageFacts(
            pr_count=2,
            threshold=2,
            existing_triage_issue=None,
            watch_label="code-reviewed",
            prs=((10, "Fix bug A"), (20, "Add feature B")),
        )

        snapshot = make_snapshot(
            triage_facts=triage_facts,
        )

        plan = planner.plan(snapshot)

        create_actions = [a for a in plan.actions if a.action_type == ActionType.CREATE_TRIAGE_ISSUE]
        assert len(create_actions) == 1
        body = create_actions[0].body
        assert "PR #10" in body
        assert "Fix bug A" in body
        assert "PR #20" in body
        assert "Add feature B" in body
        assert "code-reviewed" in body
        assert "triage-reviewed" in body

    def test_triage_issue_inherits_labels_from_source(self):
        """Planner inherits labels from source issues based on triage config."""
        from issue_orchestrator.domain.models import TriageFacts
        from issue_orchestrator.control.actions import ActionType
        from issue_orchestrator.infra.config import TriageConfig, MilestoneStrategyConfig

        config = make_config(
            triage_review_agent="agent:triage",
            triage_review_threshold=2,
        )
        # Configure label inheritance
        config.triage = TriageConfig(
            inherit_labels=["io-e2e-test-data", "team:backend"],
            explicit_labels=["needs-batch-review"],
            milestone_strategy=MilestoneStrategyConfig(),
            priority="P2",
        )
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        # Source labels include one that should be inherited
        triage_facts = TriageFacts(
            pr_count=3,
            threshold=2,
            existing_triage_issue=None,
            watch_label="code-reviewed",
            prs=((1, "PR 1"), (2, "PR 2"), (3, "PR 3")),
            source_labels=frozenset(["io-e2e-test-data", "other-label"]),
            source_milestones=(),
        )

        snapshot = make_snapshot(triage_facts=triage_facts)
        plan = planner.plan(snapshot)

        create_actions = [a for a in plan.actions if a.action_type == ActionType.CREATE_TRIAGE_ISSUE]
        assert len(create_actions) == 1
        action = create_actions[0]

        # Should have agent label, explicit labels, and inherited labels
        assert "agent:triage" in action.labels
        assert "needs-batch-review" in action.labels
        assert "io-e2e-test-data" in action.labels  # Inherited (was in source_labels)
        assert "team:backend" not in action.labels  # Not inherited (not in source_labels)
        assert action.title.startswith("[P2-000]")

    def test_triage_issue_inherits_milestone_latest(self):
        """Planner picks latest milestone from source issues."""
        from issue_orchestrator.domain.models import TriageFacts
        from issue_orchestrator.control.actions import ActionType
        from issue_orchestrator.infra.config import TriageConfig, MilestoneStrategyConfig

        config = make_config(
            triage_review_agent="agent:triage",
            triage_review_threshold=2,
        )
        config.triage = TriageConfig(
            milestone_strategy=MilestoneStrategyConfig(inherit_from_issues="latest"),
        )
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        triage_facts = TriageFacts(
            pr_count=2,
            threshold=2,
            existing_triage_issue=None,
            watch_label="code-reviewed",
            prs=((1, "PR 1"), (2, "PR 2")),
            source_labels=frozenset(),
            source_milestones=((1, "M1"), (3, "M3"), (2, "M2")),  # Unsorted
        )

        snapshot = make_snapshot(triage_facts=triage_facts)
        plan = planner.plan(snapshot)

        create_actions = [a for a in plan.actions if a.action_type == ActionType.CREATE_TRIAGE_ISSUE]
        assert len(create_actions) == 1
        action = create_actions[0]

        # Should pick highest milestone number (3 = M3)
        assert action.milestone == 3

    def test_triage_issue_inherits_milestone_earliest(self):
        """Planner picks earliest milestone from source issues."""
        from issue_orchestrator.domain.models import TriageFacts
        from issue_orchestrator.control.actions import ActionType
        from issue_orchestrator.infra.config import TriageConfig, MilestoneStrategyConfig

        config = make_config(
            triage_review_agent="agent:triage",
            triage_review_threshold=2,
        )
        config.triage = TriageConfig(
            milestone_strategy=MilestoneStrategyConfig(inherit_from_issues="earliest"),
        )
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        triage_facts = TriageFacts(
            pr_count=2,
            threshold=2,
            existing_triage_issue=None,
            watch_label="code-reviewed",
            prs=((1, "PR 1"), (2, "PR 2")),
            source_labels=frozenset(),
            source_milestones=((3, "M3"), (1, "M1"), (2, "M2")),  # Unsorted
        )

        snapshot = make_snapshot(triage_facts=triage_facts)
        plan = planner.plan(snapshot)

        create_actions = [a for a in plan.actions if a.action_type == ActionType.CREATE_TRIAGE_ISSUE]
        assert len(create_actions) == 1
        action = create_actions[0]

        # Should pick lowest milestone number (1 = M1)
        assert action.milestone == 1


class TestPlanDiscoveredReworks:
    """Tests for Planner's _plan_discovered_reworks method.

    This method processes DiscoveredRework facts from scans
    and produces QueueReworkAction for the orchestrator to apply.
    """

    def test_plans_queue_action_for_discovered_rework(self):
        """Planner produces QueueReworkAction for discovered reworks."""
        from issue_orchestrator.domain.models import DiscoveredRework
        from issue_orchestrator.control.actions import ActionType

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredRework(
            issue_number=42,
            pr_number=100,
            branch_name="feature/issue-42",
            agent_type="agent:developer",
            rework_cycle=2,
        )

        snapshot = make_snapshot(
            discovered_reworks=(discovered,),
            pending_reworks=(),  # Not already queued
        )

        plan = planner.plan(snapshot)

        # Should have a QueueReworkAction
        queue_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_REWORK]
        assert len(queue_actions) == 1
        action = queue_actions[0]
        assert action.issue_number == 42
        assert action.rework_cycle == 2

    def test_removes_pr_pending_label_when_queueing_rework(self):
        """Planner removes pr-pending label so scheduler considers issue available."""
        from issue_orchestrator.domain.models import DiscoveredRework
        from issue_orchestrator.control.actions import ActionType

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredRework(
            issue_number=42,
            pr_number=100,
            branch_name="feature/issue-42",
            agent_type="agent:developer",
            rework_cycle=2,
        )

        snapshot = make_snapshot(
            discovered_reworks=(discovered,),
            pending_reworks=(),
        )

        plan = planner.plan(snapshot)

        # Should have RemoveLabelAction for pr-pending BEFORE QueueReworkAction
        remove_actions = [a for a in plan.actions if a.action_type == ActionType.REMOVE_LABEL and a.label == "pr-pending"]
        assert len(remove_actions) == 1
        assert remove_actions[0].issue_number == 42

        queue_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_REWORK]
        assert len(queue_actions) == 1

    def test_skips_already_queued_reworks(self):
        """Planner skips discovered reworks that are already in pending_reworks."""
        from issue_orchestrator.domain.models import DiscoveredRework
        from issue_orchestrator.domain.issue_key import GitHubIssueKey
        from issue_orchestrator.control.actions import ActionType

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredRework(
            issue_number=42,
            pr_number=100,
            branch_name="feature/issue-42",
            agent_type="agent:developer",
            rework_cycle=1,
        )

        # Already queued in pending_reworks
        already_pending = PendingRework(
            issue_key=GitHubIssueKey(repo="test/repo", external_id="42"),
            agent_type="agent:developer",
            rework_cycle=1,
        )

        snapshot = make_snapshot(
            discovered_reworks=(discovered,),
            pending_reworks=(already_pending,),
        )

        plan = planner.plan(snapshot)

        # Should NOT have a QueueReworkAction for already-queued rework
        queue_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_REWORK]
        assert len(queue_actions) == 0

    def test_post_publish_validation_rework_flips_pr_back_to_needs_rework(self):
        """Planner removes code-reviewed, adds needs-rework, and comments on the PR."""
        from issue_orchestrator.domain.models import DiscoveredRework
        from issue_orchestrator.control.actions import ActionType

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredRework(
            issue_number=42,
            pr_number=100,
            branch_name="feature/issue-42",
            agent_type="agent:developer",
            rework_cycle=2,
            source="post_publish_validation",
            feedback=(
                "POST-PUBLISH VALIDATION FAILURE (address these issues):\n\n"
                "PR #100 is no longer ready to merge."
            ),
        )

        snapshot = make_snapshot(
            discovered_reworks=(discovered,),
            pending_reworks=(),
        )

        plan = planner.plan(snapshot)

        remove_reviewed = [
            a for a in plan.actions
            if a.action_type == ActionType.REMOVE_LABEL
            and a.issue_number == 100
            and a.label == "code-reviewed"
        ]
        add_needs_rework = [
            a for a in plan.actions
            if a.action_type == ActionType.ADD_LABEL
            and a.issue_number == 100
            and a.label == "needs-rework"
        ]
        comment_actions = [
            a for a in plan.actions
            if a.action_type == ActionType.ADD_COMMENT
            and a.number == 100
            and a.is_pr
        ]
        queue_actions = [
            a for a in plan.actions
            if a.action_type == ActionType.QUEUE_REWORK
        ]

        assert len(remove_reviewed) == 1
        assert len(add_needs_rework) == 1
        assert len(comment_actions) == 1
        assert "<!-- io:post-publish-validation -->" in comment_actions[0].comment
        assert "POST-PUBLISH VALIDATION FAILURE" in comment_actions[0].comment
        assert len(queue_actions) == 1
        assert queue_actions[0].source == "post_publish_validation"
        assert queue_actions[0].feedback == discovered.feedback

    def test_post_publish_validation_rework_skips_duplicate_marker_comment(self):
        """When the marker comment already exists on the PR, the planner
        suppresses the duplicate comment but keeps the label flip and queue."""
        from issue_orchestrator.domain.models import DiscoveredRework
        from issue_orchestrator.control.actions import ActionType

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredRework(
            issue_number=42,
            pr_number=100,
            branch_name="feature/issue-42",
            agent_type="agent:developer",
            rework_cycle=2,
            source="post_publish_validation",
            feedback="PR #100 is no longer ready to merge.",
            feedback_comment_already_posted=True,
        )

        snapshot = make_snapshot(
            discovered_reworks=(discovered,),
            pending_reworks=(),
        )

        plan = planner.plan(snapshot)

        comment_actions = [
            a for a in plan.actions
            if a.action_type == ActionType.ADD_COMMENT
            and a.number == 100
            and a.is_pr
        ]
        remove_reviewed = [
            a for a in plan.actions
            if a.action_type == ActionType.REMOVE_LABEL
            and a.issue_number == 100
            and a.label == "code-reviewed"
        ]
        add_needs_rework = [
            a for a in plan.actions
            if a.action_type == ActionType.ADD_LABEL
            and a.issue_number == 100
            and a.label == "needs-rework"
        ]
        queue_actions = [
            a for a in plan.actions
            if a.action_type == ActionType.QUEUE_REWORK
        ]

        # Comment is deduped away...
        assert comment_actions == []
        # ...but the rest of the post-publish flip is unchanged.
        assert len(remove_reviewed) == 1
        assert len(add_needs_rework) == 1
        assert len(queue_actions) == 1
        assert queue_actions[0].feedback == discovered.feedback


class TestPlanDiscoveredEscalations:
    """Tests for Planner's _plan_discovered_escalations method.

    This method processes DiscoveredEscalation facts from scans
    and produces EscalateToHumanAction for the orchestrator to apply.
    """

    def test_plans_escalate_action_for_discovered_escalation(self):
        """Planner produces EscalateToHumanAction for discovered escalations."""
        from issue_orchestrator.domain.models import DiscoveredEscalation
        from issue_orchestrator.control.actions import ActionType

        config = make_config(code_review_agent="agent:reviewer", max_rework_cycles=2)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredEscalation(
            issue_number=42,
            pr_number=100,
            rework_cycle=3,  # Exceeded max of 2
        )

        snapshot = make_snapshot(
            discovered_escalations=(discovered,),
        )

        plan = planner.plan(snapshot)

        # Should have an EscalateToHumanAction
        escalate_actions = [a for a in plan.actions if a.action_type == ActionType.ESCALATE_TO_HUMAN]
        assert len(escalate_actions) == 1
        action = escalate_actions[0]
        assert action.issue_number == 42
        assert action.pr_number == 100
        assert action.rework_cycles == 3  # Passed through from scanner; ActionApplier subtracts 1 for display
        assert action.issue_key == "42"  # Falls back to str(issue_number) when no Issue in snapshot

    def test_discovered_escalation_uses_stable_issue_key(self):
        """Planner resolves stable issue_key from snapshot issues for discovered escalations."""
        from issue_orchestrator.domain.models import DiscoveredEscalation
        from issue_orchestrator.control.actions import ActionType

        config = make_config(code_review_agent="agent:reviewer", max_rework_cycles=2)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        issue = make_issue(number=42, title="[M0-721] Fix the widget")
        discovered = DiscoveredEscalation(
            issue_number=42,
            pr_number=100,
            rework_cycle=3,
        )

        snapshot = make_snapshot(
            issues=[issue],
            discovered_escalations=(discovered,),
        )

        plan = planner.plan(snapshot)

        escalate_actions = [a for a in plan.actions if a.action_type == ActionType.ESCALATE_TO_HUMAN]
        assert len(escalate_actions) == 1
        assert escalate_actions[0].issue_key == "M0-721"

    def test_no_escalate_actions_when_no_discovered_escalations(self):
        """Planner produces no escalate actions when no discovered escalations."""
        from issue_orchestrator.control.actions import ActionType

        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        snapshot = make_snapshot(
            discovered_escalations=(),  # Empty
        )

        plan = planner.plan(snapshot)

        escalate_actions = [a for a in plan.actions if a.action_type == ActionType.ESCALATE_TO_HUMAN]
        assert len(escalate_actions) == 0


class TestPlanAwaitingMergeEscalations:
    """Tests for Planner._plan_awaiting_merge_escalations.

    Distinct from rework-cycle exhaustion: the PR was *approved* and
    is being escalated because either CI checks stalled or branch
    protection blocks merge in a way code rework can't unstick.
    """

    def test_checks_pending_timeout_emits_escalate_with_override_comment(self):
        from issue_orchestrator.domain.models import (
            DiscoveredAwaitingMergeEscalation,
        )
        from issue_orchestrator.control.actions import ActionType

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredAwaitingMergeEscalation(
            issue_number=42,
            pr_number=100,
            pr_url="https://github.com/o/r/pull/100",
            issue_key="M0-042",
            rework_cycle=1,
            kind="checks_pending_timeout",
            reason="Required GitHub checks have been pending for ~31 minute(s) ...",
        )

        snapshot = make_snapshot(
            discovered_awaiting_merge_escalations=(discovered,),
        )

        plan = planner.plan(snapshot)

        escalate_actions = [
            a for a in plan.actions if a.action_type == ActionType.ESCALATE_TO_HUMAN
        ]
        assert len(escalate_actions) == 1
        action = escalate_actions[0]
        assert action.issue_number == 42
        assert action.pr_number == 100
        assert action.escalation_reason == "post-publish: checks_pending_timeout"
        assert action.comment_override is not None
        assert "CI checks timed out" in action.comment_override
        # The legacy rework-cycles narrative must NOT appear in the override
        assert "rework cycles" not in action.comment_override.lower()

    def test_branch_protection_blocked_emits_escalate_with_protection_copy(self):
        from issue_orchestrator.domain.models import (
            DiscoveredAwaitingMergeEscalation,
        )
        from issue_orchestrator.control.actions import ActionType

        config = make_config(
            code_review_agent="agent:reviewer",
            label_prefix="bot",
        )
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredAwaitingMergeEscalation(
            issue_number=42,
            pr_number=100,
            pr_url="https://github.com/o/r/pull/100",
            issue_key="M0-042",
            rework_cycle=1,
            kind="branch_protection_blocked",
            reason="Branch protection blocks merge despite all required checks passing.",
        )

        snapshot = make_snapshot(
            discovered_awaiting_merge_escalations=(discovered,),
        )

        plan = planner.plan(snapshot)

        escalate_actions = [
            a for a in plan.actions if a.action_type == ActionType.ESCALATE_TO_HUMAN
        ]
        assert len(escalate_actions) == 1
        action = escalate_actions[0]
        assert action.needs_human_label == "bot:needs-human"
        assert action.comment_override is not None
        assert "branch protection" in action.comment_override.lower()
        assert "`bot:needs-human`" in action.comment_override
        assert "`blocked-needs-human`" not in action.comment_override

    def test_status_rollup_permission_denied_emits_escalate_with_capability_copy(self):
        from issue_orchestrator.domain.models import (
            DiscoveredAwaitingMergeEscalation,
        )
        from issue_orchestrator.control.actions import ActionType

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredAwaitingMergeEscalation(
            issue_number=42,
            pr_number=100,
            pr_url="https://github.com/o/r/pull/100",
            issue_key="M0-042",
            rework_cycle=1,
            kind="status_rollup_permission_denied",
            reason=(
                "PR #100 is reviewer-approved but its merge-readiness is "
                "'unstable' ... the configured GitHub token lacks permission "
                "to read check status (statusCheckRollup) ..."
            ),
        )

        snapshot = make_snapshot(
            discovered_awaiting_merge_escalations=(discovered,),
        )

        plan = planner.plan(snapshot)

        escalate_actions = [
            a for a in plan.actions if a.action_type == ActionType.ESCALATE_TO_HUMAN
        ]
        assert len(escalate_actions) == 1
        action = escalate_actions[0]
        assert action.escalation_reason == "post-publish: status_rollup_permission_denied"
        assert action.comment_override is not None
        assert "cannot read check status" in action.comment_override.lower()


class TestPlanDiscoveredFailures:
    """Tests for Planner's _plan_discovered_failures method.

    This method processes DiscoveredFailure facts from session completions
    and produces QueueTriageAction for the orchestrator to apply.
    """

    def test_plans_triage_action_for_discovered_failure(self):
        """Planner produces QueueTriageAction for discovered failures."""
        from issue_orchestrator.domain.models import DiscoveredFailure
        from issue_orchestrator.control.actions import ActionType

        config = make_config(
            triage_review_agent="agent:triage",
            triage_review_on_failure=True,
        )
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredFailure(
            issue_number=42,
            issue_title="Test issue",
            failure_reason="failed",
        )

        snapshot = make_snapshot(
            discovered_failures=(discovered,),
        )

        plan = planner.plan(snapshot)

        # Should have a QueueTriageAction
        triage_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_TRIAGE]
        assert len(triage_actions) == 1
        action = triage_actions[0]
        assert action.issue_number == 42
        assert "failed" in action.title

    def test_no_triage_action_when_disabled(self):
        """Planner produces no triage actions when triage_review_on_failure is disabled."""
        from issue_orchestrator.domain.models import DiscoveredFailure
        from issue_orchestrator.control.actions import ActionType

        config = make_config(
            triage_review_agent="agent:triage",
            triage_review_on_failure=False,  # Disabled
        )
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredFailure(
            issue_number=42,
            issue_title="Test issue",
            failure_reason="failed",
        )

        snapshot = make_snapshot(
            discovered_failures=(discovered,),
        )

        plan = planner.plan(snapshot)

        triage_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_TRIAGE]
        assert len(triage_actions) == 0

    def test_no_triage_action_when_no_agent_configured(self):
        """Planner produces no triage actions when no triage_review_agent configured."""
        from issue_orchestrator.domain.models import DiscoveredFailure
        from issue_orchestrator.control.actions import ActionType

        config = make_config(
            triage_review_agent=None,  # Not configured
            triage_review_on_failure=True,
        )
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredFailure(
            issue_number=42,
            issue_title="Test issue",
            failure_reason="failed",
        )

        snapshot = make_snapshot(
            discovered_failures=(discovered,),
        )

        plan = planner.plan(snapshot)

        triage_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_TRIAGE]
        assert len(triage_actions) == 0

    def test_skips_already_queued_triage(self):
        """Planner skips failures for issues already queued for triage."""
        from issue_orchestrator.domain.models import DiscoveredFailure
        from issue_orchestrator.control.actions import ActionType

        config = make_config(
            triage_review_agent="agent:triage",
            triage_review_on_failure=True,
        )
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredFailure(
            issue_number=42,
            issue_title="Test issue",
            failure_reason="failed",
        )

        # Already queued for triage
        pending_triage = PendingTriageReview(
            issue_number=42,
            title="Already queued",
        )

        snapshot = make_snapshot(
            discovered_failures=(discovered,),
            pending_triage=[pending_triage],
        )

        plan = planner.plan(snapshot)

        triage_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_TRIAGE]
        assert len(triage_actions) == 0

    def test_no_triage_actions_when_no_discovered_failures(self):
        """Planner produces no triage actions when no discovered failures."""
        from issue_orchestrator.control.actions import ActionType

        config = make_config(
            triage_review_agent="agent:triage",
            triage_review_on_failure=True,
        )
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        snapshot = make_snapshot(
            discovered_failures=(),  # Empty
        )

        plan = planner.plan(snapshot)

        triage_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_TRIAGE]
        assert len(triage_actions) == 0


class TestPlanCleanups:
    """Tests for Planner's _plan_cleanups method.

    This method processes CleanupFacts from cleanup fact-gathering
    and produces CleanupSessionAction for the orchestrator to apply.
    """

    def test_plans_cleanup_action_for_reviewed_pr(self):
        """Planner produces CleanupSessionAction when PR has been reviewed."""
        from issue_orchestrator.domain.models import CleanupFacts
        from issue_orchestrator.control.actions import ActionType

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        # Pending cleanup with PR #100
        cleanup_facts = CleanupFacts(
            pending_cleanups=((42, 100, "session-42", "/tmp/worktree-42"),),
            reviewed_pr_numbers=frozenset({100}),  # PR #100 has been reviewed
            close_tabs=True,
            remove_worktrees=True,
        )

        snapshot = make_snapshot(
            cleanup_facts=cleanup_facts,
        )

        plan = planner.plan(snapshot)

        # Should have a CleanupSessionAction
        cleanup_actions = [a for a in plan.actions if a.action_type == ActionType.CLEANUP_SESSION]
        assert len(cleanup_actions) == 1
        action = cleanup_actions[0]
        assert action.issue_number == 42
        assert action.pr_number == 100
        assert action.terminal_id == "session-42"
        assert action.worktree_path == "/tmp/worktree-42"
        assert action.close_tabs is True
        assert action.remove_worktrees is True

    def test_no_cleanup_when_pr_not_reviewed(self):
        """Planner produces no CleanupSessionAction when PR is not reviewed."""
        from issue_orchestrator.domain.models import CleanupFacts
        from issue_orchestrator.control.actions import ActionType

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        # Pending cleanup with PR #100, but PR #100 is NOT in reviewed set
        cleanup_facts = CleanupFacts(
            pending_cleanups=((42, 100, "session-42", "/tmp/worktree-42"),),
            reviewed_pr_numbers=frozenset({200, 300}),  # Different PRs reviewed
            close_tabs=True,
            remove_worktrees=True,
        )

        snapshot = make_snapshot(
            cleanup_facts=cleanup_facts,
        )

        plan = planner.plan(snapshot)

        # Should NOT have a CleanupSessionAction
        cleanup_actions = [a for a in plan.actions if a.action_type == ActionType.CLEANUP_SESSION]
        assert len(cleanup_actions) == 0

    def test_no_cleanup_when_no_facts(self):
        """Planner produces no CleanupSessionAction when cleanup_facts is None."""
        from issue_orchestrator.control.actions import ActionType

        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        snapshot = make_snapshot(
            cleanup_facts=None,  # No facts gathered
        )

        plan = planner.plan(snapshot)

        cleanup_actions = [a for a in plan.actions if a.action_type == ActionType.CLEANUP_SESSION]
        assert len(cleanup_actions) == 0

    def test_cleanup_respects_close_tabs_setting(self):
        """Planner respects the close_tabs setting from CleanupFacts."""
        from issue_orchestrator.domain.models import CleanupFacts
        from issue_orchestrator.control.actions import ActionType

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        cleanup_facts = CleanupFacts(
            pending_cleanups=((42, 100, "session-42", "/tmp/worktree-42"),),
            reviewed_pr_numbers=frozenset({100}),
            close_tabs=False,  # Don't close tabs
            remove_worktrees=True,
        )

        snapshot = make_snapshot(
            cleanup_facts=cleanup_facts,
        )

        plan = planner.plan(snapshot)

        cleanup_actions = [a for a in plan.actions if a.action_type == ActionType.CLEANUP_SESSION]
        assert len(cleanup_actions) == 1
        assert cleanup_actions[0].close_tabs is False
        assert cleanup_actions[0].remove_worktrees is True


# =============================================================================
# BEHAVIOR-CENTRIC TESTS: Priority and Action Ordering
# =============================================================================

class TestActionPriority:
    """Tests for action priority: Reviews > Reworks > Triage > Issues.

    The planner enforces a strict priority order to ensure completed work
    (PRs waiting for review) is processed before starting new work.
    """

    def test_reviews_take_priority_over_issues(self):
        """Reviews are launched before issues when both are available."""
        config = make_config(code_review_agent="agent:reviewer", max_concurrent_sessions=1)
        scheduler = Scheduler(config)

        # Mock review workflow to return a review
        mock_review_workflow = Mock()
        mock_review_workflow.is_configured.return_value = True
        mock_decision = Mock()
        mock_decision.should_launch = True
        mock_decision.skip_reason = None
        mock_decision.reviews_to_launch = [
            PendingReview(issue_key=FakeIssueKey(name="1"), pr_number=100, pr_url="url", branch_name="branch", _issue_number=1),
        ]
        mock_review_workflow.should_launch_reviews.return_value = mock_decision

        planner = Planner(
            config=config,
            scheduler=scheduler,
            review_workflow=mock_review_workflow,
        )

        # Both issues and reviews are available
        snapshot = make_snapshot(
            issues=[make_issue(2)],  # Issue waiting
            pending_reviews=[
                PendingReview(issue_key=FakeIssueKey(name="1"), pr_number=100, pr_url="url", branch_name="branch", _issue_number=1),
            ],
        )

        plan = planner.plan(snapshot)

        # Should launch review, NOT issue (only 1 slot available)
        launch_actions = plan.actions_of_type(ActionType.LAUNCH_SESSION)
        assert len(launch_actions) == 1
        assert launch_actions[0].session_type == SessionType.REVIEW
        assert launch_actions[0].number == 100

    def test_reworks_take_priority_over_triage(self):
        """Reworks are launched before triage when both are available."""
        from tests.conftest import MockEventSink

        config = make_config(
            code_review_agent="agent:reviewer",
            triage_review_agent="agent:triage",
            max_concurrent_sessions=1,
        )
        scheduler = Scheduler(config)

        # Mock rework workflow to return a rework
        mock_rework_workflow = Mock()
        mock_decision = Mock()
        mock_decision.should_launch = True
        mock_decision.skip_reason = None
        pending_rework = PendingRework(
            issue_key=FakeIssueKey(name="1"),
            agent_type="agent:developer",
            rework_cycle=1,
        )
        mock_decision.reworks_to_launch = [pending_rework]
        mock_rework_workflow.should_launch_reworks.return_value = mock_decision
        mock_rework_workflow.should_escalate.return_value = Mock(should_escalate=False)

        # Mock triage workflow (should not be called if reworks consume capacity)
        mock_triage_workflow = Mock()
        mock_triage_workflow.is_configured.return_value = True

        planner = Planner(
            config=config,
            scheduler=scheduler,
            rework_workflow=mock_rework_workflow,
            triage_workflow=mock_triage_workflow,
        )

        snapshot = make_snapshot(
            pending_reworks=[pending_rework],
            pending_triage=[
                PendingTriageReview(issue_number=2, title="Investigate failure"),
            ],
        )

        plan = planner.plan(snapshot)

        # Should launch rework (priority over triage)
        launch_actions = plan.actions_of_type(ActionType.LAUNCH_SESSION)
        assert len(launch_actions) == 1
        assert launch_actions[0].session_type == SessionType.REWORK

    def test_issues_launch_when_pending_reviews_exist_but_no_review_launches(self):
        """Pending reviews should not starve issue work when review launches are skipped."""
        config = make_config(code_review_agent="agent:reviewer", max_concurrent_sessions=3)
        scheduler = Scheduler(config)

        # Mock review workflow that doesn't launch (e.g., all reviews in progress)
        mock_review_workflow = Mock()
        mock_review_workflow.is_configured.return_value = True
        mock_review_workflow.should_launch_reviews.return_value = Mock(
            should_launch=False, skip_reason="Already reviewing"
        )

        planner = Planner(
            config=config,
            scheduler=scheduler,
            review_workflow=mock_review_workflow,
        )

        snapshot = make_snapshot(
            issues=[make_issue(1), make_issue(2)],
            pending_reviews=[
                PendingReview(issue_key=FakeIssueKey(name="10"), pr_number=100, pr_url="url", branch_name="branch", _issue_number=10),
            ],
        )

        plan = planner.plan(snapshot)

        # Should launch issues because no review launches were started this tick
        issue_actions = [
            a for a in plan.actions_of_type(ActionType.LAUNCH_SESSION)
            if a.session_type == SessionType.ISSUE
        ]
        assert len(issue_actions) == 2

    def test_reviews_get_priority_but_remaining_capacity_goes_to_issues(self):
        """Reviews consume capacity first, remaining slots go to new issues."""
        config = make_config(code_review_agent="agent:reviewer", max_concurrent_sessions=3)
        scheduler = Scheduler(config)

        mock_review_workflow = Mock()
        mock_review_workflow.is_configured.return_value = True
        pending_review = PendingReview(
            issue_key=FakeIssueKey(name="10"),
            pr_number=100,
            pr_url="url",
            branch_name="branch",
            _issue_number=10,
        )
        mock_review_workflow.should_launch_reviews.return_value = Mock(
            should_launch=True,
            skip_reason=None,
            reviews_to_launch=[pending_review],
        )

        planner = Planner(
            config=config,
            scheduler=scheduler,
            review_workflow=mock_review_workflow,
        )

        snapshot = make_snapshot(
            issues=[make_issue(1), make_issue(2)],
            pending_reviews=[pending_review],
        )

        plan = planner.plan(snapshot)

        issue_actions = [
            a for a in plan.actions_of_type(ActionType.LAUNCH_SESSION)
            if a.session_type == SessionType.ISSUE
        ]
        review_actions = [
            a for a in plan.actions_of_type(ActionType.LAUNCH_SESSION)
            if a.session_type == SessionType.REVIEW
        ]
        # Review gets 1 slot, remaining 2 slots go to issues
        assert len(review_actions) == 1
        assert len(issue_actions) == 2

    def test_no_issues_when_reviews_exhaust_capacity(self):
        """When reviews consume all capacity, no issues are launched."""
        config = make_config(code_review_agent="agent:reviewer", max_concurrent_sessions=1)
        scheduler = Scheduler(config)

        mock_review_workflow = Mock()
        mock_review_workflow.is_configured.return_value = True
        pending_review = PendingReview(
            issue_key=FakeIssueKey(name="10"),
            pr_number=100,
            pr_url="url",
            branch_name="branch",
            _issue_number=10,
        )
        mock_review_workflow.should_launch_reviews.return_value = Mock(
            should_launch=True,
            skip_reason=None,
            reviews_to_launch=[pending_review],
        )

        planner = Planner(
            config=config,
            scheduler=scheduler,
            review_workflow=mock_review_workflow,
        )

        snapshot = make_snapshot(
            issues=[make_issue(1)],
            pending_reviews=[pending_review],
        )

        plan = planner.plan(snapshot)

        issue_actions = [
            a for a in plan.actions_of_type(ActionType.LAUNCH_SESSION)
            if a.session_type == SessionType.ISSUE
        ]
        review_actions = [
            a for a in plan.actions_of_type(ActionType.LAUNCH_SESSION)
            if a.session_type == SessionType.REVIEW
        ]
        # Review takes the only slot, no room for issues
        assert len(review_actions) == 1
        assert len(issue_actions) == 0

    def test_issues_launched_when_no_pending_work(self):
        """New issues are launched when no reviews, reworks, or triage are pending."""
        config = make_config(max_concurrent_sessions=3)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        snapshot = make_snapshot(
            issues=[make_issue(1), make_issue(2)],
            pending_reviews=[],
            pending_reworks=[],
            pending_triage=[],
        )

        plan = planner.plan(snapshot)

        # Should launch issues since nothing else is pending
        issue_actions = [
            a for a in plan.actions_of_type(ActionType.LAUNCH_SESSION)
            if a.session_type == SessionType.ISSUE
        ]
        assert len(issue_actions) == 2


class TestEdgeCases:
    """Tests for edge cases and unusual input combinations."""

    def test_empty_snapshot_produces_empty_plan(self):
        """A completely empty snapshot produces no actions."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        snapshot = make_snapshot(
            issues=[],
            active_sessions=[],
            pending_reviews=[],
            pending_reworks=[],
            pending_triage=[],
            discovered_reviews=(),
            discovered_reworks=(),
            discovered_escalations=(),
            discovered_failures=(),
        )

        plan = planner.plan(snapshot)

        assert plan.action_count == 0
        assert len(plan.skipped) == 0

    def test_multiple_discovered_reviews_all_queued(self):
        """Multiple discovered reviews all produce queue actions."""
        from issue_orchestrator.domain.models import DiscoveredReview

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = [
            DiscoveredReview(issue_number=1, pr_number=101, pr_url="url1", branch_name="branch1"),
            DiscoveredReview(issue_number=2, pr_number=102, pr_url="url2", branch_name="branch2"),
            DiscoveredReview(issue_number=3, pr_number=103, pr_url="url3", branch_name="branch3"),
        ]

        snapshot = make_snapshot(discovered_reviews=tuple(discovered))

        plan = planner.plan(snapshot)

        queue_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_REVIEW]
        assert len(queue_actions) == 3

        # Also produces AddLabel actions for pr-pending
        label_actions = [
            a for a in plan.actions
            if a.action_type == ActionType.ADD_LABEL and a.label == "pr-pending"
        ]
        assert len(label_actions) == 3

    def test_discovered_review_without_code_review_agent_only_adds_label(self):
        """Discovered review without code_review_agent configured only adds pr-pending label."""
        from issue_orchestrator.domain.models import DiscoveredReview

        config = make_config(code_review_agent=None)  # No review agent
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredReview(
            issue_number=42,
            pr_number=100,
            pr_url="https://github.com/test/repo/pull/100",
            branch_name="feature/issue-42",
        )

        snapshot = make_snapshot(discovered_reviews=(discovered,))

        plan = planner.plan(snapshot)

        # Should have AddLabelAction for pr-pending but NO QueueReviewAction
        label_actions = [
            a for a in plan.actions
            if a.action_type == ActionType.ADD_LABEL and a.label == "pr-pending"
        ]
        assert len(label_actions) == 1

        queue_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_REVIEW]
        assert len(queue_actions) == 0

    def test_conflicting_signals_pending_review_and_discovered_same_pr(self):
        """When a PR is both pending and discovered, don't re-queue."""
        from issue_orchestrator.domain.models import DiscoveredReview

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        # Same PR in both discovered and pending
        pr_number = 100
        discovered = DiscoveredReview(
            issue_number=42,
            pr_number=pr_number,
            pr_url="url",
            branch_name="branch",
        )
        pending = PendingReview(
            issue_key=FakeIssueKey(name="42"),
            pr_number=pr_number,
            pr_url="url",
            branch_name="branch",
            _issue_number=42,
        )

        snapshot = make_snapshot(
            discovered_reviews=(discovered,),
            pending_reviews=[pending],
        )

        plan = planner.plan(snapshot)

        # Should NOT re-queue since already pending
        queue_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_REVIEW]
        assert len(queue_actions) == 0

    def test_issue_in_discovered_reviews_excluded_from_launch(self):
        """Issues with discovered reviews are filtered out from new work launches.

        The planner filters issues that appear in discovered_reviews or discovered_reworks
        from being launched, even if they're in the available issues list.
        """
        from issue_orchestrator.domain.models import DiscoveredReview

        config = make_config(code_review_agent="agent:reviewer", max_concurrent_sessions=5)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        # Issue 42 has a discovered review
        discovered = DiscoveredReview(
            issue_number=42,
            pr_number=100,
            pr_url="url",
            branch_name="branch",
        )

        # Both issues in available list, but issue 42 has discovered review
        snapshot = make_snapshot(
            issues=[make_issue(42), make_issue(43)],
            discovered_reviews=(discovered,),
        )

        plan = planner.plan(snapshot)

        # Issue 42 should NOT be launched (has pending review in discovered_reviews)
        # Issue 43 CAN be launched if no other pending work
        # But wait - discovered_reviews produces QueueReviewAction, which means
        # pending_reviews will have an item AFTER the action is applied.
        # The exclusion happens via issues_with_pending_reviews check in _plan_issues

        issue_launches = [
            a for a in plan.actions_of_type(ActionType.LAUNCH_SESSION)
            if a.session_type == SessionType.ISSUE
        ]

        # Issue 42 is excluded (in discovered_reviews)
        # Issue 43 could be launched, but check what numbers actually launched
        launched_numbers = {a.number for a in issue_launches}
        assert 42 not in launched_numbers, "Issue 42 should not be launched (has discovered review)"

        # Issue 43 may or may not launch depending on whether pending_reviews
        # blocks new issues - but the discovered_reviews ARE converted to
        # pending_reviews indirectly via queue actions. The check is:
        # has_pending_work looks at snapshot.pending_reviews (which is empty here),
        # so issue 43 could still launch since the queue action hasn't been applied yet.
        # The key invariant is: issue 42 must not be launched.

    def test_trace_queue_decision_logs_session_history_skip(self, caplog: pytest.LogCaptureFixture):
        """Planner emits an explicit trace-queue decision for session_history skips."""
        config = make_config(max_concurrent_sessions=3)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)
        issue = make_issue(4057, title="Issue 4057")
        snapshot = make_snapshot(
            issues=[issue],
            session_history_issue_numbers=frozenset({4057}),
        )

        with caplog.at_level(logging.INFO):
            planner.plan(snapshot)

        assert "trace-queue-decision issue=4057 decision=skip reason=session_history" in caplog.text

    def test_trace_queue_decision_logs_only_when_reason_changes(self, caplog: pytest.LogCaptureFixture):
        """Queue decision traces are emitted on change, not every tick."""
        config = make_config(max_concurrent_sessions=3)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)
        issue = make_issue(4057, title="Issue 4057")
        snapshot = make_snapshot(
            issues=[issue],
            session_history_issue_numbers=frozenset({4057}),
        )

        with caplog.at_level(logging.INFO):
            planner.plan(snapshot)
            first_count = caplog.text.count("trace-queue-decision issue=4057 decision=skip reason=session_history")
            planner.plan(snapshot)
            second_count = caplog.text.count("trace-queue-decision issue=4057 decision=skip reason=session_history")

        assert first_count == 1
        assert second_count == 1

    def test_multiple_escalations_all_produce_actions(self):
        """Multiple discovered escalations all produce escalate actions."""
        from issue_orchestrator.domain.models import DiscoveredEscalation

        config = make_config(max_rework_cycles=2)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        escalations = [
            DiscoveredEscalation(issue_number=1, pr_number=101, rework_cycle=3),
            DiscoveredEscalation(issue_number=2, pr_number=102, rework_cycle=4),
        ]

        snapshot = make_snapshot(discovered_escalations=tuple(escalations))

        plan = planner.plan(snapshot)

        escalate_actions = [a for a in plan.actions if a.action_type == ActionType.ESCALATE_TO_HUMAN]
        assert len(escalate_actions) == 2
        assert {a.issue_number for a in escalate_actions} == {1, 2}

    def test_max_capacity_reached_mid_planning(self):
        """Actions respect capacity even when multiple types compete."""
        from tests.conftest import MockEventSink

        config = make_config(
            code_review_agent="agent:reviewer",
            max_concurrent_sessions=2,
        )
        scheduler = Scheduler(config)

        # Mock review workflow returning 3 reviews
        mock_review_workflow = Mock()
        mock_review_workflow.is_configured.return_value = True
        reviews = [
            PendingReview(issue_key=FakeIssueKey(name="1"), pr_number=101, pr_url="u1", branch_name="b1", _issue_number=1),
            PendingReview(issue_key=FakeIssueKey(name="2"), pr_number=102, pr_url="u2", branch_name="b2", _issue_number=2),
            PendingReview(issue_key=FakeIssueKey(name="3"), pr_number=103, pr_url="u3", branch_name="b3", _issue_number=3),
        ]
        mock_review_workflow.should_launch_reviews.return_value = Mock(
            should_launch=True,
            skip_reason=None,
            reviews_to_launch=reviews,
        )

        # Mock rework workflow returning 2 reworks
        mock_rework_workflow = Mock()
        reworks = [
            PendingRework(issue_key=FakeIssueKey(name="10"), agent_type="agent:dev", rework_cycle=1),
            PendingRework(issue_key=FakeIssueKey(name="11"), agent_type="agent:dev", rework_cycle=1),
        ]
        mock_rework_workflow.should_launch_reworks.return_value = Mock(
            should_launch=True,
            skip_reason=None,
            reworks_to_launch=reworks,
        )
        mock_rework_workflow.should_escalate.return_value = Mock(should_escalate=False)

        planner = Planner(
            config=config,
            scheduler=scheduler,
            review_workflow=mock_review_workflow,
            rework_workflow=mock_rework_workflow,
        )

        snapshot = make_snapshot(
            pending_reviews=reviews,
            pending_reworks=reworks,
        )

        plan = planner.plan(snapshot)

        # Should launch exactly 2 sessions (max capacity), all reviews (higher priority)
        launch_actions = plan.actions_of_type(ActionType.LAUNCH_SESSION)
        assert len(launch_actions) == 2
        assert all(a.session_type == SessionType.REVIEW for a in launch_actions)


class TestPlanQueueActionsOnlyPhase:
    """Tests that queue actions (Phase 1) happen even at capacity.

    The planner has two phases:
    - Phase 1: Queue population (AddLabel, QueueReview, etc.) - always runs
    - Phase 2: Session launches - only when capacity available
    """

    def test_queue_actions_produced_at_capacity(self):
        """Queue actions are generated even when at max capacity."""
        from issue_orchestrator.domain.models import DiscoveredReview

        config = make_config(code_review_agent="agent:reviewer", max_concurrent_sessions=1)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        # Already at capacity
        issue = make_issue(1)
        active_session = make_session(issue)

        # New review discovered
        discovered = DiscoveredReview(
            issue_number=42,
            pr_number=100,
            pr_url="url",
            branch_name="branch",
        )

        snapshot = make_snapshot(
            issues=[make_issue(2)],
            active_sessions=[active_session],  # At capacity
            discovered_reviews=(discovered,),
        )

        plan = planner.plan(snapshot)

        # Queue actions should still be produced
        queue_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_REVIEW]
        assert len(queue_actions) == 1

        label_actions = [
            a for a in plan.actions
            if a.action_type == ActionType.ADD_LABEL and a.label == "pr-pending"
        ]
        assert len(label_actions) == 1

        # But no launch actions (at capacity)
        launch_actions = plan.actions_of_type(ActionType.LAUNCH_SESSION)
        assert len(launch_actions) == 0

    def test_escalation_actions_produced_at_capacity(self):
        """Escalation actions are generated even when at max capacity."""
        from issue_orchestrator.domain.models import DiscoveredEscalation

        config = make_config(max_concurrent_sessions=1, max_rework_cycles=2)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        # Already at capacity
        issue = make_issue(1)
        active_session = make_session(issue)

        escalation = DiscoveredEscalation(issue_number=42, pr_number=100, rework_cycle=3)

        snapshot = make_snapshot(
            active_sessions=[active_session],
            discovered_escalations=(escalation,),
        )

        plan = planner.plan(snapshot)

        # Escalation action should still be produced
        escalate_actions = [a for a in plan.actions if a.action_type == ActionType.ESCALATE_TO_HUMAN]
        assert len(escalate_actions) == 1

    def test_triage_queue_actions_produced_at_capacity(self):
        """Triage queue actions are generated even when at max capacity."""
        from issue_orchestrator.domain.models import DiscoveredFailure

        config = make_config(
            max_concurrent_sessions=1,
            triage_review_agent="agent:triage",
            triage_review_on_failure=True,
        )
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        # Already at capacity
        issue = make_issue(1)
        active_session = make_session(issue)

        failure = DiscoveredFailure(
            issue_number=42,
            issue_title="Failed issue",
            failure_reason="timed_out",
        )

        snapshot = make_snapshot(
            active_sessions=[active_session],
            discovered_failures=(failure,),
        )

        plan = planner.plan(snapshot)

        # Triage queue action should still be produced
        triage_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_TRIAGE]
        assert len(triage_actions) == 1

    def test_cleanup_actions_produced_at_capacity(self):
        """Cleanup actions are generated even when at max capacity."""
        from issue_orchestrator.domain.models import CleanupFacts

        config = make_config(max_concurrent_sessions=1)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        # Already at capacity
        issue = make_issue(1)
        active_session = make_session(issue)

        cleanup_facts = CleanupFacts(
            pending_cleanups=((42, 100, "session-42", "/tmp/worktree-42"),),
            reviewed_pr_numbers=frozenset({100}),
            close_tabs=True,
            remove_worktrees=True,
        )

        snapshot = make_snapshot(
            active_sessions=[active_session],
            cleanup_facts=cleanup_facts,
        )

        plan = planner.plan(snapshot)

        # Cleanup action should still be produced
        cleanup_actions = [a for a in plan.actions if a.action_type == ActionType.CLEANUP_SESSION]
        assert len(cleanup_actions) == 1


class TestReworkEscalationWithinReworkPlanning:
    """Tests for escalation detection during rework planning phase."""

    def test_rework_escalates_when_max_cycles_exceeded(self):
        """Rework is escalated instead of launched when max cycles exceeded."""
        from tests.conftest import MockEventSink
        from issue_orchestrator.control.workflows import ReworkWorkflow

        config = make_config(max_rework_cycles=2)
        scheduler = Scheduler(config)

        # Create real rework workflow with mock event sink
        events = MockEventSink()
        rework_workflow = ReworkWorkflow(config=config, events=events)

        # Pending rework at cycle 3 (exceeds max of 2)
        pending_rework = PendingRework(
            issue_key=FakeIssueKey(name="42"),
            agent_type="agent:developer",
            rework_cycle=3,
        )

        planner = Planner(
            config=config,
            scheduler=scheduler,
            rework_workflow=rework_workflow,
        )

        snapshot = make_snapshot(pending_reworks=[pending_rework])

        plan = planner.plan(snapshot)

        # Should escalate, not launch
        escalate_actions = [a for a in plan.actions if a.action_type == ActionType.ESCALATE_TO_HUMAN]
        launch_actions = [
            a for a in plan.actions_of_type(ActionType.LAUNCH_SESSION)
            if a.session_type == SessionType.REWORK
        ]

        assert len(escalate_actions) == 1
        assert len(launch_actions) == 0
        assert escalate_actions[0].issue_number == 42

    def test_rework_launches_when_under_max_cycles(self):
        """Rework is launched when under max cycles."""
        from tests.conftest import MockEventSink
        from issue_orchestrator.control.workflows import ReworkWorkflow

        config = make_config(max_rework_cycles=3)
        scheduler = Scheduler(config)

        events = MockEventSink()
        rework_workflow = ReworkWorkflow(config=config, events=events)

        # Pending rework at cycle 2 (under max of 3)
        pending_rework = PendingRework(
            issue_key=FakeIssueKey(name="42"),
            agent_type="agent:developer",
            rework_cycle=2,
        )

        planner = Planner(
            config=config,
            scheduler=scheduler,
            rework_workflow=rework_workflow,
        )

        snapshot = make_snapshot(pending_reworks=[pending_rework])

        plan = planner.plan(snapshot)

        # Should launch, not escalate
        launch_actions = [
            a for a in plan.actions_of_type(ActionType.LAUNCH_SESSION)
            if a.session_type == SessionType.REWORK
        ]
        escalate_actions = [a for a in plan.actions if a.action_type == ActionType.ESCALATE_TO_HUMAN]

        assert len(launch_actions) == 1
        assert len(escalate_actions) == 0
        assert launch_actions[0].number == 42


class TestSnapshotFromState:
    """Tests for OrchestratorSnapshot.from_state factory method."""

    def test_snapshot_from_state_captures_all_fields(self):
        """Snapshot correctly captures all state fields."""
        from issue_orchestrator.domain.models import (
            OrchestratorState,
            DiscoveredReview,
            DiscoveredRework,
            DiscoveredEscalation,
            DiscoveredFailure,
            TriageFacts,
            CleanupFacts,
        )

        state = OrchestratorState()
        issue = make_issue(1)
        session = make_session(issue)
        state.active_sessions = [session]
        state.paused = True
        state.priority_queue = [1, 2, 3]
        state.issues_started_count = 5

        review = PendingReview(issue_key=FakeIssueKey(name="1"), pr_number=100, pr_url="url", branch_name="b", _issue_number=1)
        state.pending_reviews = [review]

        rework = PendingRework(issue_key=FakeIssueKey(name="2"), agent_type="agent:dev", rework_cycle=1)
        state.pending_reworks = [rework]

        triage = PendingTriageReview(issue_number=3, title="Triage")
        state.pending_triage_reviews = [triage]
        validation_retry = PendingValidationRetry(
            issue_number=4,
            issue_title="Retry",
            agent_label="agent:dev",
            worktree_path="/tmp/repo-4",
            branch_name="4-retry",
            original_prompt="original",
            validation_error="dirty",
            validation_error_file=None,
            retry_count=1,
            source_task=TaskKind.CODE,
            validation_cmd="make test",
        )
        state.pending_validation_retries = [validation_retry]

        discovered_review = DiscoveredReview(issue_number=10, pr_number=110, pr_url="url", branch_name="b")
        discovered_awaiting_merge_reconciliation = DiscoveredAwaitingMergeReconciliation(
            issue_number=10,
            pr_number=110,
            pr_url="url",
            status="merged",
            status_reason="PR merged; awaiting merge reconciled",
            source="pull_request",
        )
        discovered_awaiting_merge_drift = DiscoveredAwaitingMergeDrift(
            issue_number=10,
            pr_number=110,
            pr_url="url",
            status_reason="PR closed; issue remains open",
        )
        discovered_rework = DiscoveredRework(issue_number=11, pr_number=111, branch_name="b", agent_type="a", rework_cycle=1)
        discovered_escalation = DiscoveredEscalation(issue_number=12, pr_number=112, rework_cycle=5)
        discovered_failure = DiscoveredFailure(issue_number=13, issue_title="F", failure_reason="failed")
        triage_facts = TriageFacts(pr_count=2, threshold=3)
        cleanup_facts = CleanupFacts(pending_cleanups=(), reviewed_pr_numbers=frozenset())

        snapshot = OrchestratorSnapshot.from_state(
            issues=[issue],
            state=state,
            max_issues_to_start=10,
            discovered_reviews=[discovered_review],
            discovered_awaiting_merge_reconciliations=[
                discovered_awaiting_merge_reconciliation
            ],
            discovered_awaiting_merge_drifts=[
                discovered_awaiting_merge_drift
            ],
            discovered_reworks=[discovered_rework],
            discovered_escalations=[discovered_escalation],
            discovered_failures=[discovered_failure],
            triage_facts=triage_facts,
            cleanup_facts=cleanup_facts,
        )

        assert len(snapshot.issues) == 1
        assert len(snapshot.active_sessions) == 1
        assert snapshot.paused is True
        assert snapshot.priority_queue == (1, 2, 3)
        assert snapshot.issues_started_count == 5
        assert snapshot.max_issues_to_start == 10
        assert len(snapshot.pending_reviews) == 1
        assert len(snapshot.pending_reworks) == 1
        assert len(snapshot.pending_triage) == 1
        assert len(snapshot.pending_validation_retries) == 1
        assert len(snapshot.discovered_reviews) == 1
        assert len(snapshot.discovered_awaiting_merge_reconciliations) == 1
        assert len(snapshot.discovered_awaiting_merge_drifts) == 1
        assert len(snapshot.discovered_reworks) == 1
        assert len(snapshot.discovered_escalations) == 1
        assert len(snapshot.discovered_failures) == 1
        assert snapshot.triage_facts is not None
        assert snapshot.cleanup_facts is not None


class TestActionReasonMessages:
    """Tests that action reason messages are informative."""

    def test_queue_review_action_has_descriptive_reason(self):
        """QueueReviewAction includes PR number in reason."""
        from issue_orchestrator.domain.models import DiscoveredReview

        config = make_config(code_review_agent="agent:reviewer")
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        discovered = DiscoveredReview(
            issue_number=42,
            pr_number=100,
            pr_url="url",
            branch_name="branch",
        )

        snapshot = make_snapshot(discovered_reviews=(discovered,))
        plan = planner.plan(snapshot)

        queue_actions = [a for a in plan.actions if a.action_type == ActionType.QUEUE_REVIEW]
        assert len(queue_actions) == 1
        assert "#100" in queue_actions[0].reason

    def test_escalation_action_has_descriptive_reason(self):
        """EscalateToHumanAction includes cycle count in reason."""
        from issue_orchestrator.domain.models import DiscoveredEscalation

        config = make_config(max_rework_cycles=2)
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        escalation = DiscoveredEscalation(issue_number=42, pr_number=100, rework_cycle=3)
        snapshot = make_snapshot(discovered_escalations=(escalation,))
        plan = planner.plan(snapshot)

        escalate_actions = [a for a in plan.actions if a.action_type == ActionType.ESCALATE_TO_HUMAN]
        assert len(escalate_actions) == 1
        # Reason should mention cycles
        assert "2" in escalate_actions[0].reason  # rework_cycle - 1

    def test_launch_session_action_has_priority_reason(self):
        """LaunchSessionAction includes priority info in reason."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        issue = make_issue(42, title="[P1-001] High priority task", milestone="M1")
        snapshot = make_snapshot(issues=[issue])
        plan = planner.plan(snapshot)

        launch_actions = plan.actions_of_type(ActionType.LAUNCH_SESSION)
        assert len(launch_actions) == 1
        # Reason should include priority and milestone info
        reason = launch_actions[0].reason
        assert "milestone=M1" in reason or "P1" in reason


class TestMultiplePendingTypesInteraction:
    """Tests for interactions when multiple pending types exist simultaneously."""

    def test_pending_work_gets_priority_remaining_capacity_goes_to_issues(self):
        """Pending review/triage consume slots first, remaining capacity goes to issues."""
        from tests.conftest import MockEventSink
        from issue_orchestrator.control.workflows import ReviewWorkflow, TriageWorkflow

        config = make_config(
            code_review_agent="agent:reviewer",
            triage_review_agent="agent:triage",
            max_concurrent_sessions=5,
        )
        scheduler = Scheduler(config)
        events = MockEventSink()
        planner = Planner(
            config=config,
            scheduler=scheduler,
            review_workflow=ReviewWorkflow(config=config, events=events),
            triage_workflow=TriageWorkflow(config=config, events=events),
        )

        pending_review = PendingReview(
            issue_key=FakeIssueKey(name="100"),
            pr_number=200,
            pr_url="https://example.test/pull/200",
            branch_name="issue-100",
            _issue_number=100,
        )
        pending_triage = PendingTriageReview(issue_number=101, title="Investigate")

        snapshot = make_snapshot(
            issues=[make_issue(100), make_issue(1), make_issue(2)],
            pending_reviews=[pending_review],
            pending_triage=[pending_triage],
        )

        plan = planner.plan(snapshot)

        issue_actions = [
            a for a in plan.actions_of_type(ActionType.LAUNCH_SESSION)
            if a.session_type == SessionType.ISSUE
        ]
        review_triage_actions = [
            a for a in plan.actions_of_type(ActionType.LAUNCH_SESSION)
            if a.session_type in {SessionType.REVIEW, SessionType.TRIAGE}
        ]
        # Review + triage get 2 slots, issue 100 excluded (has pending review),
        # issues 1 and 2 get the remaining 3 slots
        assert len(review_triage_actions) == 2
        assert len(issue_actions) == 2
        assert {a.number for a in issue_actions} == {1, 2}

    def test_capacity_shared_across_all_types(self):
        """Capacity is correctly shared when multiple types launch."""
        from tests.conftest import MockEventSink
        from issue_orchestrator.control.workflows import (
            ReviewWorkflow,
            ReworkWorkflow,
            TriageWorkflow,
        )

        config = make_config(
            code_review_agent="agent:reviewer",
            triage_review_agent="agent:triage",
            max_concurrent_sessions=3,
        )
        scheduler = Scheduler(config)
        events = MockEventSink()

        review_workflow = ReviewWorkflow(config=config, events=events)
        rework_workflow = ReworkWorkflow(config=config, events=events)
        triage_workflow = TriageWorkflow(config=config, events=events)

        # 1 review, 1 rework, 1 triage, 3 issues - only 3 capacity
        pending_review = PendingReview(
            issue_key=FakeIssueKey(name="1"),
            pr_number=101,
            pr_url="url",
            branch_name="b",
            _issue_number=1,
        )
        pending_rework = PendingRework(
            issue_key=FakeIssueKey(name="2"),
            agent_type="agent:dev",
            rework_cycle=1,
        )
        pending_triage = PendingTriageReview(issue_number=3, title="Investigate")

        planner = Planner(
            config=config,
            scheduler=scheduler,
            review_workflow=review_workflow,
            rework_workflow=rework_workflow,
            triage_workflow=triage_workflow,
        )

        snapshot = make_snapshot(
            issues=[make_issue(4), make_issue(5), make_issue(6)],
            pending_reviews=[pending_review],
            pending_reworks=[pending_rework],
            pending_triage=[pending_triage],
        )

        plan = planner.plan(snapshot)

        # Should have exactly 3 launch actions total
        launch_actions = plan.actions_of_type(ActionType.LAUNCH_SESSION)
        assert len(launch_actions) == 3

        # Priority order: review first, then rework, then triage
        types = [a.session_type for a in launch_actions]
        assert types[0] == SessionType.REVIEW
        assert types[1] == SessionType.REWORK
        assert types[2] == SessionType.TRIAGE

        # No issue launches (pending work exists)
        issue_launches = [a for a in launch_actions if a.session_type == SessionType.ISSUE]
        assert len(issue_launches) == 0


class TestPlanStaleInProgressCleanup:
    """Tests for planner's stale in-progress label cleanup.

    When an issue has the in-progress label but no active session exists,
    the planner should generate a RemoveLabelAction to clean up the stale label.
    """

    def test_no_stale_issues_no_actions(self):
        """When no stale in-progress issues, no cleanup actions are generated."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        # No stale issues
        snapshot = make_snapshot(
            issues=[make_issue(1)],
            stale_in_progress_issues=(),  # Empty
        )

        plan = planner.plan(snapshot)

        # Should be no RemoveLabel actions
        remove_actions = plan.actions_of_type(ActionType.REMOVE_LABEL)
        assert len(remove_actions) == 0

    def test_stale_issue_generates_remove_label_action(self):
        """Stale in-progress issue generates RemoveLabelAction."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        stale_issue = make_issue(1, labels=["in-progress"])

        snapshot = make_snapshot(
            issues=[stale_issue],
            stale_in_progress_issues=(stale_issue,),
        )

        plan = planner.plan(snapshot)

        # Should have a RemoveLabel action for issue #1
        remove_actions = plan.actions_of_type(ActionType.REMOVE_LABEL)
        assert len(remove_actions) == 1
        assert remove_actions[0].issue_number == 1
        assert remove_actions[0].label == "in-progress"
        assert "stale" in remove_actions[0].reason.lower()

    def test_multiple_stale_issues_generate_multiple_actions(self):
        """Multiple stale issues generate multiple RemoveLabelActions."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        stale_issues = [
            make_issue(1, labels=["in-progress"]),
            make_issue(2, labels=["in-progress"]),
            make_issue(3, labels=["in-progress"]),
        ]

        snapshot = make_snapshot(
            issues=stale_issues,
            stale_in_progress_issues=tuple(stale_issues),
        )

        plan = planner.plan(snapshot)

        remove_actions = plan.actions_of_type(ActionType.REMOVE_LABEL)
        assert len(remove_actions) == 3

        # Verify all issue numbers are covered
        issue_numbers = {a.issue_number for a in remove_actions}
        assert issue_numbers == {1, 2, 3}

    def test_stale_cleanup_runs_when_paused(self):
        """When orchestrator is paused, no stale cleanup actions are generated."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        stale_issue = make_issue(1, labels=["in-progress"])

        snapshot = make_snapshot(
            issues=[stale_issue],
            stale_in_progress_issues=(stale_issue,),
            paused=True,  # Orchestrator is paused
        )

        plan = planner.plan(snapshot)

        # Paused orchestrator returns empty plan
        assert plan.action_count == 0

    def test_stale_cleanup_is_phase_1_action(self):
        """Stale cleanup happens in Phase 1 (queue population), before capacity check."""
        config = make_config(max_concurrent_sessions=0)  # No capacity
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        stale_issue = make_issue(1, labels=["in-progress"])

        snapshot = make_snapshot(
            issues=[stale_issue],
            stale_in_progress_issues=(stale_issue,),
        )

        plan = planner.plan(snapshot)

        # Even with no capacity, stale cleanup should happen (Phase 1)
        remove_actions = plan.actions_of_type(ActionType.REMOVE_LABEL)
        assert len(remove_actions) == 1

    def test_stale_cleanup_with_active_session_not_stale(self):
        """Issue with active session is NOT in stale_in_progress_issues."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        # Issue has active session - NOT stale
        issue_with_session = make_issue(1, labels=["in-progress"])
        session = make_session(issue_with_session)

        snapshot = make_snapshot(
            issues=[issue_with_session],
            active_sessions=[session],
            stale_in_progress_issues=(),  # Not stale - has session
        )

        plan = planner.plan(snapshot)

        # No cleanup actions
        remove_actions = plan.actions_of_type(ActionType.REMOVE_LABEL)
        assert len(remove_actions) == 0
