"""Unit tests for the LabelProjection module."""

import pytest
from unittest.mock import MagicMock

from issue_orchestrator.control.label_projection import (
    LabelProjection,
    DesiredLabels,
    LabelCategory,
    compute_label_changes,
)
from issue_orchestrator.domain.state_machines.issue_machine import IssueState


class TestDesiredLabels:
    """Test the DesiredLabels dataclass."""

    def test_add_creates_to_add_set(self):
        """Test add factory creates to_add set."""
        desired = DesiredLabels.add("in-progress", "priority:high")
        assert desired.to_add == frozenset({"in-progress", "priority:high"})
        assert desired.to_remove == frozenset()

    def test_remove_creates_to_remove_set(self):
        """Test remove factory creates to_remove set."""
        desired = DesiredLabels.remove("in-progress", "blocked")
        assert desired.to_remove == frozenset({"in-progress", "blocked"})
        assert desired.to_add == frozenset()

    def test_replace_creates_both_sets(self):
        """Test replace factory creates both sets."""
        desired = DesiredLabels.replace(
            add={"in-progress"},
            remove={"blocked"},
        )
        assert desired.to_add == frozenset({"in-progress"})
        assert desired.to_remove == frozenset({"blocked"})

    def test_merge_combines_sets(self):
        """Test merge combines two DesiredLabels."""
        d1 = DesiredLabels.add("a", "b")
        d2 = DesiredLabels.add("c")
        d3 = DesiredLabels.remove("x")

        merged = d1.merge(d2).merge(d3)

        assert merged.to_add == frozenset({"a", "b", "c"})
        assert merged.to_remove == frozenset({"x"})

    def test_is_frozen(self):
        """Test that DesiredLabels is immutable."""
        desired = DesiredLabels.add("test")
        with pytest.raises(AttributeError):
            desired.to_add = frozenset()


class TestLabelProjection:
    """Test the LabelProjection class."""

    @pytest.fixture
    def mock_config(self):
        config = MagicMock()
        config.get_label_in_progress.return_value = "in-progress"
        return config

    @pytest.fixture
    def projection(self, mock_config):
        return LabelProjection(mock_config)

    def test_for_available_state_removes_in_progress(self, projection):
        """Test AVAILABLE state removes in-progress label."""
        desired = projection.for_issue_state(IssueState.AVAILABLE)
        assert "in-progress" in desired.to_remove

    def test_for_claimed_state_adds_in_progress(self, projection):
        """Test CLAIMED state adds in-progress label."""
        desired = projection.for_issue_state(IssueState.CLAIMED)
        assert "in-progress" in desired.to_add

    def test_for_in_progress_state_adds_in_progress(self, projection):
        """Test IN_PROGRESS state adds in-progress label."""
        desired = projection.for_issue_state(IssueState.IN_PROGRESS)
        assert "in-progress" in desired.to_add

    def test_for_blocked_state_adds_blocked_label(self, projection):
        """Test BLOCKED state adds blocked label."""
        desired = projection.for_issue_state(IssueState.BLOCKED)
        assert "in-progress" in desired.to_add
        assert "blocked" in desired.to_add

    def test_for_needs_human_state_adds_blocked_needs_human(self, projection):
        """Test NEEDS_HUMAN state adds blocked-needs-human label."""
        desired = projection.for_issue_state(IssueState.NEEDS_HUMAN)
        assert "in-progress" in desired.to_add
        assert "blocked-needs-human" in desired.to_add

    def test_for_pr_pending_state_keeps_in_progress(self, projection):
        """Test PR_PENDING state keeps in-progress label."""
        desired = projection.for_issue_state(IssueState.PR_PENDING)
        assert "in-progress" in desired.to_add

    def test_for_completed_state_removes_in_progress(self, projection):
        """Test COMPLETED state removes in-progress label."""
        desired = projection.for_issue_state(IssueState.COMPLETED)
        assert "in-progress" in desired.to_remove

    def test_for_blocked_with_reason(self, projection):
        """Test blocked with reason creates specific label."""
        desired = projection.for_blocked(reason="tests-failing")
        assert "blocked-tests-failing" in desired.to_add
        assert "in-progress" in desired.to_add

    def test_for_blocked_without_reason(self, projection):
        """Test blocked without reason uses generic label."""
        desired = projection.for_blocked()
        assert "blocked" in desired.to_add

    def test_for_unblocked_sets_must_not_have(self, projection):
        """Test unblocked sets must_not_have for blocked labels."""
        desired = projection.for_unblocked()
        assert "blocked" in desired.must_not_have

    def test_for_review_needed(self, projection):
        """Test review needed adds needs-code-review label."""
        desired = projection.for_review_needed(pr_number=123)
        assert "needs-code-review" in desired.to_add

    def test_for_rework_needed(self, projection):
        """Test rework needed adds rework labels."""
        desired = projection.for_rework_needed(cycle=2)
        assert "needs-rework" in desired.to_add
        assert "rework-cycle-2" in desired.to_add


class TestComputeLabelChanges:
    """Test the compute_label_changes function."""

    def test_adds_missing_labels(self):
        """Test that missing labels are added."""
        current = {"bug", "documentation"}
        desired = DesiredLabels.add("in-progress", "bug")

        to_add, to_remove = compute_label_changes(current, desired)

        assert to_add == {"in-progress"}  # bug already exists
        assert to_remove == set()

    def test_removes_existing_labels(self):
        """Test that existing labels are removed."""
        current = {"in-progress", "blocked"}
        desired = DesiredLabels.remove("in-progress", "nonexistent")

        to_add, to_remove = compute_label_changes(current, desired)

        assert to_add == set()
        assert to_remove == {"in-progress"}  # nonexistent not in current

    def test_handles_must_not_have_patterns(self):
        """Test that must_not_have patterns match prefixes."""
        current = {"blocked-tests", "blocked-needs-human", "in-progress"}
        desired = DesiredLabels(must_not_have=frozenset(["blocked"]))

        to_add, to_remove = compute_label_changes(current, desired)

        assert to_add == set()
        assert to_remove == {"blocked-tests", "blocked-needs-human"}

    def test_combined_add_remove(self):
        """Test combined add and remove operations."""
        current = {"in-progress", "blocked"}
        desired = DesiredLabels.replace(
            add={"needs-review"},
            remove={"blocked"},
        )

        to_add, to_remove = compute_label_changes(current, desired)

        assert to_add == {"needs-review"}
        assert to_remove == {"blocked"}

    def test_no_changes_when_in_sync(self):
        """Test no changes when current matches desired."""
        current = {"in-progress", "bug"}
        desired = DesiredLabels.add("in-progress", "bug")

        to_add, to_remove = compute_label_changes(current, desired)

        assert to_add == set()
        assert to_remove == set()
