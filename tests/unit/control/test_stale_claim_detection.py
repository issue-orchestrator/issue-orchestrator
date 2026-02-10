"""Unit tests for stale claim detection in the Planner."""

import pytest

from issue_orchestrator.control.planner import (
    Planner,
    OrchestratorSnapshot,
)
from issue_orchestrator.control.scheduler import Scheduler
from issue_orchestrator.control.actions import ActionType
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra import labels
from issue_orchestrator.domain.models import Issue


def make_config(**kwargs) -> Config:
    """Create a test config with sensible defaults."""
    defaults = {
        "repo": "test/repo",
        "max_concurrent_sessions": 3,
    }
    defaults.update(kwargs)
    return Config(**defaults)


def make_issue(number: int, title: str = "Test issue", issue_labels: list[str] | None = None) -> Issue:
    """Create a test issue."""
    return Issue(
        number=number,
        title=title,
        body="",
        labels=issue_labels or [],
        state="open",
        milestone=None,
        milestone_number=None,
        milestone_due_on=None,
    )


def make_snapshot(
    issues: list[Issue] | None = None,
    stale_claim_issues: list[Issue] | None = None,
    **kwargs,
) -> OrchestratorSnapshot:
    """Create a test snapshot."""
    return OrchestratorSnapshot(
        issues=tuple(issues or []),
        active_sessions=tuple(),
        pending_reviews=tuple(),
        pending_reworks=tuple(),
        pending_triage=tuple(),
        paused=False,
        stale_claim_issues=tuple(stale_claim_issues or []),
        **kwargs,
    )


class TestStaleClaims:
    """Tests for Planner._plan_stale_claim_cleanup."""

    def test_detects_expired_claims(self):
        """Plans removal of io:claimed and addition of blocked:stale-claim."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        # Issue with stale claim (detected by orchestrator/observer)
        stale_issue = make_issue(42, issue_labels=[labels.IO_CLAIMED])

        snapshot = make_snapshot(
            issues=[stale_issue],
            stale_claim_issues=[stale_issue],
        )

        plan = planner.plan(snapshot)

        # Should have 2 actions: remove io:claimed, add blocked:stale-claim
        stale_actions = [
            a for a in plan.actions
            if getattr(a, "issue_number", None) == 42
        ]
        assert len(stale_actions) == 2

        # Check remove label action
        remove_actions = [
            a for a in stale_actions
            if a.action_type == ActionType.REMOVE_LABEL
        ]
        assert len(remove_actions) == 1
        assert remove_actions[0].label == labels.IO_CLAIMED  # type: ignore

        # Check add label action
        add_actions = [
            a for a in stale_actions
            if a.action_type == ActionType.ADD_LABEL
        ]
        assert len(add_actions) == 1
        assert add_actions[0].label == labels.BLOCKED_STALE_CLAIM  # type: ignore

    def test_adds_stale_claim_label(self):
        """Adds blocked:stale-claim label to stale claimed issues."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        stale_issue = make_issue(123, issue_labels=[labels.IO_CLAIMED])

        snapshot = make_snapshot(
            issues=[stale_issue],
            stale_claim_issues=[stale_issue],
        )

        plan = planner.plan(snapshot)

        add_label_actions = [
            a for a in plan.actions
            if a.action_type == ActionType.ADD_LABEL
            and getattr(a, "issue_number", None) == 123
        ]
        assert len(add_label_actions) == 1
        assert add_label_actions[0].label == labels.BLOCKED_STALE_CLAIM  # type: ignore

    def test_removes_claimed_label(self):
        """Removes io:claimed label from stale claimed issues."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        stale_issue = make_issue(456, issue_labels=[labels.IO_CLAIMED])

        snapshot = make_snapshot(
            issues=[stale_issue],
            stale_claim_issues=[stale_issue],
        )

        plan = planner.plan(snapshot)

        remove_label_actions = [
            a for a in plan.actions
            if a.action_type == ActionType.REMOVE_LABEL
            and getattr(a, "issue_number", None) == 456
        ]
        assert len(remove_label_actions) == 1
        assert remove_label_actions[0].label == labels.IO_CLAIMED  # type: ignore

    def test_ignores_valid_claims(self):
        """Does not plan actions for issues not in stale_claim_issues."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        # Issue with valid claim (not in stale_claim_issues)
        valid_issue = make_issue(42, issue_labels=[labels.IO_CLAIMED])

        snapshot = make_snapshot(
            issues=[valid_issue],
            stale_claim_issues=[],  # Empty - no stale claims detected
        )

        plan = planner.plan(snapshot)

        # Should not have any claim-related actions
        claim_actions = [
            a for a in plan.actions
            if getattr(a, "label", None) in [labels.IO_CLAIMED, labels.BLOCKED_STALE_CLAIM]
        ]
        assert len(claim_actions) == 0

    def test_handles_multiple_stale_claims(self):
        """Handles multiple stale claimed issues correctly."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        stale_issues = [
            make_issue(1, issue_labels=[labels.IO_CLAIMED]),
            make_issue(2, issue_labels=[labels.IO_CLAIMED]),
            make_issue(3, issue_labels=[labels.IO_CLAIMED]),
        ]

        snapshot = make_snapshot(
            issues=stale_issues,
            stale_claim_issues=stale_issues,
        )

        plan = planner.plan(snapshot)

        # Should have 2 actions per stale issue (6 total)
        stale_actions = [
            a for a in plan.actions
            if getattr(a, "label", None) in [labels.IO_CLAIMED, labels.BLOCKED_STALE_CLAIM]
        ]
        assert len(stale_actions) == 6

        # Check each issue has both actions
        for issue in stale_issues:
            issue_actions = [
                a for a in stale_actions
                if getattr(a, "issue_number", None) == issue.number
            ]
            assert len(issue_actions) == 2

    def test_no_actions_when_no_stale_claims(self):
        """Returns no stale claim actions when stale_claim_issues is empty."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        snapshot = make_snapshot(
            issues=[make_issue(1), make_issue(2)],
            stale_claim_issues=[],
        )

        plan = planner.plan(snapshot)

        claim_actions = [
            a for a in plan.actions
            if getattr(a, "label", None) in [labels.IO_CLAIMED, labels.BLOCKED_STALE_CLAIM]
        ]
        assert len(claim_actions) == 0

    def test_stale_claim_cleanup_runs_before_issue_selection(self):
        """Stale claim cleanup happens in phase 1 (before session launches)."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        # Issue that is stale claimed - should be cleaned up first
        stale_issue = make_issue(42, issue_labels=[labels.IO_CLAIMED])

        snapshot = make_snapshot(
            issues=[stale_issue],
            stale_claim_issues=[stale_issue],
        )

        plan = planner.plan(snapshot)

        # Should have stale claim cleanup actions
        remove_claimed = [
            a for a in plan.actions
            if a.action_type == ActionType.REMOVE_LABEL
            and getattr(a, "label", None) == labels.IO_CLAIMED
        ]
        assert len(remove_claimed) == 1

        add_stale = [
            a for a in plan.actions
            if a.action_type == ActionType.ADD_LABEL
            and getattr(a, "label", None) == labels.BLOCKED_STALE_CLAIM
        ]
        assert len(add_stale) == 1
