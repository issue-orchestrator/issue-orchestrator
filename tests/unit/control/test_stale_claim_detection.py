"""Unit tests for stale claim detection in the Planner."""

import pytest

from issue_orchestrator.control.planner import (
    Planner,
)
from issue_orchestrator.control.planner_types import (
    OrchestratorSnapshot,
)
from issue_orchestrator.control.scheduler import Scheduler
from issue_orchestrator.control.actions import ActionType
from issue_orchestrator.control.orchestrator_support import _detect_stale_claims
from issue_orchestrator.domain.claim import ClaimFetchError
from issue_orchestrator.events import EventContext
from issue_orchestrator.infra.config import Config
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
        pending_tech_lead=tuple(),
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
        stale_issue = make_issue(42, issue_labels=["io:claimed"])

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
        assert remove_actions[0].label == "io:claimed"

        # Check add label action
        add_actions = [
            a for a in stale_actions
            if a.action_type == ActionType.ADD_LABEL
        ]
        assert len(add_actions) == 1
        assert add_actions[0].label == "blocked:stale-claim"

    def test_adds_stale_claim_label(self):
        """Adds blocked:stale-claim label to stale claimed issues."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        stale_issue = make_issue(123, issue_labels=["io:claimed"])

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
        assert add_label_actions[0].label == "blocked:stale-claim"

    def test_removes_claimed_label(self):
        """Removes io:claimed label from stale claimed issues."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        stale_issue = make_issue(456, issue_labels=["io:claimed"])

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
        assert remove_label_actions[0].label == "io:claimed"

    def test_ignores_valid_claims(self):
        """Does not plan actions for issues not in stale_claim_issues."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        # Issue with valid claim (not in stale_claim_issues)
        valid_issue = make_issue(42, issue_labels=["io:claimed"])

        snapshot = make_snapshot(
            issues=[valid_issue],
            stale_claim_issues=[],  # Empty - no stale claims detected
        )

        plan = planner.plan(snapshot)

        # Should not have any claim-related actions
        claim_actions = [
            a for a in plan.actions
            if getattr(a, "label", None) in ["io:claimed", "blocked:stale-claim"]
        ]
        assert len(claim_actions) == 0

    def test_handles_multiple_stale_claims(self):
        """Handles multiple stale claimed issues correctly."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        stale_issues = [
            make_issue(1, issue_labels=["io:claimed"]),
            make_issue(2, issue_labels=["io:claimed"]),
            make_issue(3, issue_labels=["io:claimed"]),
        ]

        snapshot = make_snapshot(
            issues=stale_issues,
            stale_claim_issues=stale_issues,
        )

        plan = planner.plan(snapshot)

        # Should have 2 actions per stale issue (6 total)
        stale_actions = [
            a for a in plan.actions
            if getattr(a, "label", None) in ["io:claimed", "blocked:stale-claim"]
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
            if getattr(a, "label", None) in ["io:claimed", "blocked:stale-claim"]
        ]
        assert len(claim_actions) == 0

    def test_stale_claim_cleanup_runs_before_issue_selection(self):
        """Stale claim cleanup happens in phase 1 (before session launches)."""
        config = make_config()
        scheduler = Scheduler(config)
        planner = Planner(config=config, scheduler=scheduler)

        # Issue that is stale claimed - should be cleaned up first
        stale_issue = make_issue(42, issue_labels=["io:claimed"])

        snapshot = make_snapshot(
            issues=[stale_issue],
            stale_claim_issues=[stale_issue],
        )

        plan = planner.plan(snapshot)

        # Should have stale claim cleanup actions
        remove_claimed = [
            a for a in plan.actions
            if a.action_type == ActionType.REMOVE_LABEL
            and getattr(a, "label", None) == "io:claimed"
        ]
        assert len(remove_claimed) == 1

        add_stale = [
            a for a in plan.actions
            if a.action_type == ActionType.ADD_LABEL
            and getattr(a, "label", None) == "blocked:stale-claim"
        ]
        assert len(add_stale) == 1


class MockEventSink:
    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)


class TestDetectStaleClaimsAPIResilience:
    """Tests for _detect_stale_claims handling of ClaimFetchError."""

    def test_skips_issue_on_api_error(self):
        """Issues that fail API fetch are skipped, not flagged as stale."""

        class FailingClaimManager:
            def get_current_claim(self, issue_number):
                raise ClaimFetchError("GitHub 502")

        issues = [make_issue(42, issue_labels=["io:claimed"])]
        events = MockEventSink()
        ctx = EventContext()

        result = _detect_stale_claims(
            issues=issues,
            active_sessions=[],
            claim_manager=FailingClaimManager(),
            events=events,
            event_context=ctx,
        )

        # Should NOT flag as stale — we don't know if claim is stale
        assert len(result) == 0

    def test_mixed_api_errors_and_valid_checks(self):
        """Some issues fail API, others succeed — only truly stale ones reported."""

        class MixedClaimManager:
            def get_current_claim(self, issue_number):
                if issue_number == 42:
                    raise ClaimFetchError("transient error")
                # Issue 43: no valid claim → stale
                return None

        issues = [
            make_issue(42, issue_labels=["io:claimed"]),
            make_issue(43, issue_labels=["io:claimed"]),
        ]
        events = MockEventSink()
        ctx = EventContext()

        result = _detect_stale_claims(
            issues=issues,
            active_sessions=[],
            claim_manager=MixedClaimManager(),
            events=events,
            event_context=ctx,
        )

        # Only issue 43 should be flagged as stale
        assert len(result) == 1
        assert result[0].number == 43
