"""Unit tests for queue audit module (infra/audit.py).

Tests the issue filtering logic that determines which issues are queued
and which are skipped. Critical for preventing retry loops on failed issues.
"""

from pathlib import Path

import pytest

from issue_orchestrator.infra.audit import (
    audit_issue,
    SkipReason,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra import labels
from issue_orchestrator.domain.models import Issue


@pytest.fixture
def sample_config():
    """Create a minimal config for testing."""
    return Config(
        repo="owner/repo",
        repo_root=Path("/tmp/test"),
        worktree_base=Path("/tmp"),
        agents={},
        max_concurrent_sessions=3,
    )


class TestBlockingLabelFiltering:
    """Tests for blocking label filtering - critical for preventing retry loops."""

    def test_blocked_label_skips_issue(self, sample_config):
        """Issue with 'blocked' label should be skipped."""
        issue = Issue(
            number=1,
            title="Blocked issue",
            labels=["blocked", "agent:backend"],
            body="",
        )

        entry = audit_issue(
            issue=issue,
            config=sample_config,
            history_numbers=set(),
            active_numbers=set(),
            issue_branches=None,
        )

        assert entry.status == SkipReason.BLOCKED
        assert "blocked" in entry.detail  # type: ignore - Union type narrowing limitation
    def test_blocked_failed_label_skips_issue(self, sample_config):
        """Issue with 'blocked-failed' label should be skipped.

        This is critical for preventing retry loops when a session fails
        without writing a completion file.
        """
        issue = Issue(
            number=1,
            title="Failed session issue",
            labels=[labels.BLOCKED_FAILED, "agent:backend"],
            body="",
        )

        entry = audit_issue(
            issue=issue,
            config=sample_config,
            history_numbers=set(),
            active_numbers=set(),
            issue_branches=None,
        )

        assert entry.status == SkipReason.BLOCKED
        assert labels.BLOCKED_FAILED in entry.detail  # type: ignore - Union type narrowing limitation
    def test_blocked_needs_human_label_returns_needs_human(self, sample_config):
        """Issue with 'blocked-needs-human' label should return NEEDS_HUMAN reason."""
        issue = Issue(
            number=1,
            title="Needs human issue",
            labels=[labels.BLOCKED_NEEDS_HUMAN, "agent:backend"],
            body="",
        )

        entry = audit_issue(
            issue=issue,
            config=sample_config,
            history_numbers=set(),
            active_numbers=set(),
            issue_branches=None,
        )

        assert entry.status == SkipReason.NEEDS_HUMAN
        assert labels.BLOCKED_NEEDS_HUMAN in entry.detail  # type: ignore - Union type narrowing limitation
    def test_blocked_cross_milestone_label_skips_issue(self, sample_config):
        """Issue with 'blocked-cross-milestone' label should be skipped."""
        issue = Issue(
            number=1,
            title="Cross milestone blocked",
            labels=[labels.BLOCKED_CROSS_MILESTONE, "agent:backend"],
            body="",
        )

        entry = audit_issue(
            issue=issue,
            config=sample_config,
            history_numbers=set(),
            active_numbers=set(),
            issue_branches=None,
        )

        assert entry.status == SkipReason.BLOCKED
        assert labels.BLOCKED_CROSS_MILESTONE in entry.detail  # type: ignore - Union type narrowing limitation
    def test_legacy_needs_human_label_skips_issue(self, sample_config):
        """Issue with legacy 'needs-human' label should be skipped."""
        issue = Issue(
            number=1,
            title="Legacy needs human",
            labels=["needs-human", "agent:backend"],
            body="",
        )

        entry = audit_issue(
            issue=issue,
            config=sample_config,
            history_numbers=set(),
            active_numbers=set(),
            issue_branches=None,
        )

        # Should be caught by either blocking check or direct needs-human check
        assert entry.status in (SkipReason.BLOCKED, SkipReason.NEEDS_HUMAN)

    def test_custom_blocked_prefix_label_skips_issue(self, sample_config):
        """Any label starting with 'blocked-' should skip the issue."""
        issue = Issue(
            number=1,
            title="Custom blocked",
            labels=["blocked-custom-reason", "agent:backend"],
            body="",
        )

        entry = audit_issue(
            issue=issue,
            config=sample_config,
            history_numbers=set(),
            active_numbers=set(),
            issue_branches=None,
        )

        assert entry.status == SkipReason.BLOCKED
        assert "blocked-custom-reason" in entry.detail  # type: ignore - Union type narrowing limitation
class TestActiveSessionFiltering:
    """Tests for active session filtering."""

    def test_active_session_skips_issue(self, sample_config):
        """Issue with active session should be skipped."""
        issue = Issue(
            number=1,
            title="Active issue",
            labels=["agent:backend"],
            body="",
        )

        entry = audit_issue(
            issue=issue,
            config=sample_config,
            history_numbers=set(),
            active_numbers={1},  # This issue has an active session
            issue_branches=None,
        )

        assert entry.status == SkipReason.ACTIVE_SESSION


class TestHistoryFiltering:
    """Tests for session history filtering."""

    def test_in_history_skips_issue(self, sample_config):
        """Issue in session history should be skipped."""
        issue = Issue(
            number=1,
            title="Already processed",
            labels=["agent:backend"],
            body="",
        )

        entry = audit_issue(
            issue=issue,
            config=sample_config,
            history_numbers={1},  # This issue was already processed
            active_numbers=set(),
            issue_branches=None,
        )

        assert entry.status == SkipReason.IN_HISTORY


class TestClosedIssueFiltering:
    """Tests for closed issue filtering."""

    def test_closed_issue_skips(self, sample_config):
        """Closed issue should be skipped."""
        issue = Issue(
            number=1,
            title="Closed issue",
            labels=["agent:backend"],
            body="",
            state="closed",
        )

        entry = audit_issue(
            issue=issue,
            config=sample_config,
            history_numbers=set(),
            active_numbers=set(),
            issue_branches=None,
        )

        assert entry.status == SkipReason.CLOSED


class TestQueuedIssue:
    """Tests for issues that should be queued."""

    def test_available_issue_is_queued(self, sample_config):
        """Issue without blocking conditions should be queued."""
        # Need to add agent to config for issue to be queued
        from issue_orchestrator.infra.config import AgentConfig
        sample_config.agents = {
            "agent:backend": AgentConfig(prompt_path=Path("/tmp/prompt.txt"))
        }

        issue = Issue(
            number=1,
            title="Ready to work",
            labels=["agent:backend"],
            body="",
            state="open",
        )

        entry = audit_issue(
            issue=issue,
            config=sample_config,
            history_numbers=set(),
            active_numbers=set(),
            issue_branches=None,
        )

        assert entry.status == SkipReason.QUEUED
