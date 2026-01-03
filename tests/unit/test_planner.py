"""Unit tests for the Planner.

These tests verify the planner's pure policy logic without any external dependencies.
The planner decides "should we?" - no mocks for tmux/GitHub needed.
"""

import pytest
from unittest.mock import Mock, MagicMock

from issue_orchestrator.infra.config import Config
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
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.session_key import SessionKey, TaskKind


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
        worktree_base=Path("/tmp/worktrees"),
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
            PendingReview(issue_key=FakeIssueKey(name="1"), pr_number=100, pr_url="url", branch_name="branch"),
        ]
        mock_workflow.should_launch_reviews.return_value = mock_decision

        planner = Planner(
            config=config,
            scheduler=scheduler,
            review_workflow=mock_workflow,
        )

        snapshot = make_snapshot(
            pending_reviews=[
                PendingReview(issue_key=FakeIssueKey(name="1"), pr_number=100, pr_url="url", branch_name="branch"),
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


class TestPlanDiscoveredReviews:
    """Tests for Planner's _plan_discovered_reviews method.

    This method processes DiscoveredReview facts from session completions
    and produces QueueReviewAction for the orchestrator to apply.
    """

    def test_plans_queue_action_for_discovered_review(self):
        """Planner produces QueueReviewAction for discovered reviews."""
        from issue_orchestrator.models import DiscoveredReview
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

    def test_skips_already_queued_reviews(self):
        """Planner skips discovered reviews that are already in pending_reviews."""
        from issue_orchestrator.models import DiscoveredReview
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
        from issue_orchestrator.models import DiscoveredReview
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
        from issue_orchestrator.models import DiscoveredReview
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


class TestPlanTriageIssueCreation:
    """Tests for Planner's _plan_triage_issue_creation method.

    This method processes TriageFacts and produces CreateTriageIssueAction
    when the threshold is met and no existing triage issue exists.
    """

    def test_creates_triage_issue_at_threshold(self):
        """Planner produces CreateTriageIssueAction when threshold is met."""
        from issue_orchestrator.models import TriageFacts
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
        from issue_orchestrator.models import TriageFacts
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
        from issue_orchestrator.models import TriageFacts
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
        from issue_orchestrator.models import TriageFacts
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


class TestPlanDiscoveredReworks:
    """Tests for Planner's _plan_discovered_reworks method.

    This method processes DiscoveredRework facts from scans
    and produces QueueReworkAction for the orchestrator to apply.
    """

    def test_plans_queue_action_for_discovered_rework(self):
        """Planner produces QueueReworkAction for discovered reworks."""
        from issue_orchestrator.models import DiscoveredRework
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

    def test_skips_already_queued_reworks(self):
        """Planner skips discovered reworks that are already in pending_reworks."""
        from issue_orchestrator.models import DiscoveredRework
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


class TestPlanDiscoveredEscalations:
    """Tests for Planner's _plan_discovered_escalations method.

    This method processes DiscoveredEscalation facts from scans
    and produces EscalateToHumanAction for the orchestrator to apply.
    """

    def test_plans_escalate_action_for_discovered_escalation(self):
        """Planner produces EscalateToHumanAction for discovered escalations."""
        from issue_orchestrator.models import DiscoveredEscalation
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
        assert action.rework_cycles == 2  # rework_cycle - 1

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


class TestPlanDiscoveredFailures:
    """Tests for Planner's _plan_discovered_failures method.

    This method processes DiscoveredFailure facts from session completions
    and produces QueueTriageAction for the orchestrator to apply.
    """

    def test_plans_triage_action_for_discovered_failure(self):
        """Planner produces QueueTriageAction for discovered failures."""
        from issue_orchestrator.models import DiscoveredFailure
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
        from issue_orchestrator.models import DiscoveredFailure
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
        from issue_orchestrator.models import DiscoveredFailure
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
        from issue_orchestrator.models import DiscoveredFailure
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
        from issue_orchestrator.models import CleanupFacts
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
        assert action.terminal_session_name == "session-42"
        assert action.worktree_path == "/tmp/worktree-42"
        assert action.close_tabs is True
        assert action.remove_worktrees is True

    def test_no_cleanup_when_pr_not_reviewed(self):
        """Planner produces no CleanupSessionAction when PR is not reviewed."""
        from issue_orchestrator.models import CleanupFacts
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
        from issue_orchestrator.models import CleanupFacts
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
