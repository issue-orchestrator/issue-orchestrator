"""Tests for the centralized labels module."""

import pytest

from issue_orchestrator.infra.labels import (
    # Constants
    IN_PROGRESS,
    BLOCKED,
    BLOCKED_FAILED,
    BLOCKED_NEEDS_HUMAN,
    LEGACY_NEEDS_HUMAN,
    LEGACY_FAILED,
    BLOCKING_PREFIX,
    # Functions
    is_blocking,
    is_blocking_any,
    get_blocking_labels,
    is_in_progress,
    requires_human,
    requires_human_any,
    pick_blocking_label,
)


class TestIsBlocking:
    """Test is_blocking function."""

    def test_blocked_prefix_labels_are_blocking(self):
        """All blocked-* labels should block."""
        assert is_blocking(BLOCKED)
        assert is_blocking(BLOCKED_FAILED)
        assert is_blocking(BLOCKED_NEEDS_HUMAN)
        assert is_blocking("blocked-custom-reason")

    def test_legacy_labels_are_blocking(self):
        """Legacy labels should still block for backwards compat."""
        assert is_blocking(LEGACY_NEEDS_HUMAN)
        assert is_blocking(LEGACY_FAILED)

    def test_non_blocking_labels(self):
        """Regular labels should not block."""
        assert not is_blocking(IN_PROGRESS)
        assert not is_blocking("bug")
        assert not is_blocking("enhancement")
        assert not is_blocking("agent:coder")

    def test_empty_string(self):
        """Empty string should not block."""
        assert not is_blocking("")


class TestIsBlockingAny:
    """Test is_blocking_any function."""

    def test_returns_true_if_any_blocking(self):
        """Should return True if any label blocks."""
        labels = ["bug", "enhancement", BLOCKED_FAILED]
        assert is_blocking_any(labels)

    def test_returns_false_if_none_blocking(self):
        """Should return False if no labels block."""
        labels = ["bug", "enhancement", IN_PROGRESS]
        assert not is_blocking_any(labels)

    def test_empty_list(self):
        """Empty list should return False."""
        assert not is_blocking_any([])


class TestGetBlockingLabels:
    """Test get_blocking_labels function."""

    def test_filters_to_blocking_only(self):
        """Should return only blocking labels."""
        labels = ["bug", BLOCKED_NEEDS_HUMAN, "enhancement", LEGACY_FAILED]
        result = get_blocking_labels(labels)
        assert result == [BLOCKED_NEEDS_HUMAN, LEGACY_FAILED]

    def test_empty_when_none_blocking(self):
        """Should return empty list when no blocking labels."""
        labels = ["bug", "enhancement"]
        assert get_blocking_labels(labels) == []


class TestIsInProgress:
    """Test is_in_progress function."""

    def test_true_when_in_progress_present(self):
        """Should return True when in-progress label present."""
        assert is_in_progress([IN_PROGRESS, "bug"])

    def test_false_when_not_present(self):
        """Should return False when in-progress not present."""
        assert not is_in_progress(["bug", "enhancement"])


class TestRequiresHuman:
    """Test requires_human functions."""

    def test_blocked_needs_human_requires_human(self):
        """blocked-needs-human requires human."""
        assert requires_human(BLOCKED_NEEDS_HUMAN)

    def test_legacy_needs_human_requires_human(self):
        """Legacy needs-human requires human."""
        assert requires_human(LEGACY_NEEDS_HUMAN)

    def test_other_blocking_labels_dont_require_human(self):
        """Other blocking labels don't require human."""
        assert not requires_human(BLOCKED)
        assert not requires_human(BLOCKED_FAILED)
        assert not requires_human(LEGACY_FAILED)

    def test_requires_human_any(self):
        """Should detect human-required labels in list."""
        assert requires_human_any(["bug", BLOCKED_NEEDS_HUMAN])
        assert not requires_human_any(["bug", BLOCKED_FAILED])


class TestPickBlockingLabel:
    """Test pick_blocking_label function."""

    def test_pick_needs_human(self):
        """Should return blocked-needs-human."""
        assert pick_blocking_label(needs_human=True) == BLOCKED_NEEDS_HUMAN

    def test_pick_failed(self):
        """Should return blocked-failed."""
        assert pick_blocking_label(failed=True) == BLOCKED_FAILED

    def test_pick_default_is_blocked(self):
        """Should return generic blocked when no specific reason."""
        assert pick_blocking_label() == BLOCKED

    def test_needs_human_takes_precedence(self):
        """needs_human should take precedence over other reasons."""
        # When multiple reasons, needs_human wins (checked first)
        assert pick_blocking_label(needs_human=True, failed=True) == BLOCKED_NEEDS_HUMAN
