"""Unit tests for the Planner.

These tests verify the planner's pure policy logic without any external dependencies.
The planner decides "should we?" - no mocks for tmux/GitHub needed.
"""

import pytest
from unittest.mock import Mock, MagicMock

from issue_orchestrator.config import Config
from issue_orchestrator.control.planner import (
    Planner,
    Plan,
    OrchestratorSnapshot,
    SkippedItem,
)
from issue_orchestrator.control.scheduler import Scheduler
from issue_orchestrator.control.actions import ActionType, LaunchSessionAction
from issue_orchestrator.models import (
    Issue,
    Session,
    SessionStatus,
    PendingReview,
    PendingRework,
    PendingTriageReview,
    AgentConfig,
)


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


def make_session(issue: Issue) -> Session:
    """Create a test session for an issue."""
    from pathlib import Path
    from datetime import datetime

    agent_config = AgentConfig(
        prompt_path=Path("/tmp/test.md"),
        worktree_base=Path("/tmp/worktrees"),
    )
    return Session(
        issue=issue,
        agent_config=agent_config,
        tmux_session_name=f"issue-{issue.number}",
        worktree_path=Path(f"/tmp/worktree-{issue.number}"),
        branch_name=f"issue-{issue.number}",
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
        assert action.session_type == "issue"
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
            PendingReview(issue_number=1, pr_number=100, pr_url="url", branch_name="branch"),
        ]
        mock_workflow.should_launch_reviews.return_value = mock_decision

        planner = Planner(
            config=config,
            scheduler=scheduler,
            review_workflow=mock_workflow,
        )

        snapshot = make_snapshot(
            pending_reviews=[
                PendingReview(issue_number=1, pr_number=100, pr_url="url", branch_name="branch"),
            ],
        )

        plan = planner.plan(snapshot)

        # Should have review launch action
        review_actions = [a for a in plan.actions if a.session_type == "review"]
        assert len(review_actions) == 1
        assert review_actions[0].number == 100


class TestPlanHelpers:
    """Tests for Plan and snapshot helper methods."""

    def test_plan_has_action_for(self):
        """Plan.has_action_for correctly identifies planned actions."""
        plan = Plan(
            actions=(
                LaunchSessionAction(session_type="issue", number=42, command="", working_dir=""),
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
