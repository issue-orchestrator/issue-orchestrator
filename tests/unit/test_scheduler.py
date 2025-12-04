"""Unit tests for the scheduler module."""

import pytest
from unittest.mock import MagicMock, patch
from issue_orchestrator.scheduler import Scheduler, SchedulerResult
from issue_orchestrator.models import Issue


def create_mock_issue(number, priority=None, milestone=None):
    """Helper to create mock GitHub issue objects."""
    mock_issue = MagicMock()
    mock_issue.number = number
    mock_issue.milestone = milestone

    if priority:
        label = MagicMock()
        label.name = f"priority:{priority}"
        mock_issue.labels = [label]
    else:
        mock_issue.labels = []

    return mock_issue


class TestScheduler:
    """Test the Scheduler class."""

    def test_scheduler_creation(self, sample_config):
        """Test basic scheduler creation."""
        scheduler = Scheduler(config=sample_config)
        assert scheduler.config == sample_config

    def test_sort_by_priority_high_to_low(self, sample_config):
        """Test sorting issues by priority from high to low."""
        scheduler = Scheduler(config=sample_config)

        issues = [
            create_mock_issue(1, priority="low"),
            create_mock_issue(2, priority="high"),
            create_mock_issue(3, priority="medium"),
            create_mock_issue(4),  # No priority
        ]

        sorted_issues = scheduler.sort_by_priority(issues)

        # Priority order: high (0), medium (1), low (2), none (3)
        assert sorted_issues[0].number == 2  # High priority
        assert sorted_issues[1].number == 3  # Medium priority
        assert sorted_issues[2].number == 1  # Low priority
        assert sorted_issues[3].number == 4  # No priority

    def test_sort_by_priority_same_priority_by_number(self, sample_config):
        """Test that issues with same priority are sorted by issue number."""
        scheduler = Scheduler(config=sample_config)

        issues = [
            create_mock_issue(10, priority="high"),
            create_mock_issue(5, priority="high"),
            create_mock_issue(15, priority="high"),
        ]

        sorted_issues = scheduler.sort_by_priority(issues)

        assert [i.number for i in sorted_issues] == [5, 10, 15]

    def test_sort_by_priority_with_milestones(self, sample_config):
        """Test sorting with milestone priority."""
        scheduler = Scheduler(config=sample_config)

        # Create mock issues with milestone
        issue_m6 = MagicMock()
        issue_m6.number = 1
        issue_m6.labels = [MagicMock(name="priority:high")]
        issue_m6.milestone = MagicMock(title="M6")

        issue_m7 = MagicMock()
        issue_m7.number = 2
        issue_m7.labels = [MagicMock(name="priority:high")]
        issue_m7.milestone = MagicMock(title="M7")

        issue_no_m = MagicMock()
        issue_no_m.number = 3
        issue_no_m.labels = [MagicMock(name="priority:high")]
        issue_no_m.milestone = None

        sorted_issues = scheduler.sort_by_priority([issue_no_m, issue_m7, issue_m6])

        # M6 should come before M7, both before no milestone
        assert sorted_issues[0].number == 1  # M6
        assert sorted_issues[1].number == 2  # M7
        assert sorted_issues[2].number == 3  # No milestone

    def test_get_priority_value(self, sample_config):
        """Test getting numeric priority value from labels."""
        scheduler = Scheduler(config=sample_config)

        high_issue = create_mock_issue(1, priority="high")
        assert scheduler._get_priority_value(high_issue) == 0

        medium_issue = create_mock_issue(2, priority="medium")
        assert scheduler._get_priority_value(medium_issue) == 1

        low_issue = create_mock_issue(3, priority="low")
        assert scheduler._get_priority_value(low_issue) == 2

        no_priority = create_mock_issue(4)
        assert scheduler._get_priority_value(no_priority) == 3

    def test_pick_next_batch_respects_max_sessions(self, sample_config):
        """Test that pick_next_batch respects max_sessions limit."""
        sample_config.max_sessions = 2
        scheduler = Scheduler(config=sample_config)

        available = [create_mock_issue(i, priority="high") for i in range(1, 6)]

        # Current count is 0, so we can pick 2
        batch = scheduler.pick_next_batch(available, current_count=0)

        assert len(batch) == 2

    def test_pick_next_batch_with_current_sessions(self, sample_config):
        """Test picking batch when some sessions are already active."""
        sample_config.max_sessions = 3
        scheduler = Scheduler(config=sample_config)

        available = [create_mock_issue(i, priority="high") for i in range(1, 6)]

        # Current count is 2, so we can pick 1 more
        batch = scheduler.pick_next_batch(available, current_count=2)

        assert len(batch) == 1

    def test_pick_next_batch_no_slots_available(self, sample_config):
        """Test picking batch when no slots are available."""
        sample_config.max_sessions = 2
        scheduler = Scheduler(config=sample_config)

        available = [create_mock_issue(i, priority="high") for i in range(1, 4)]

        # Current count equals max sessions
        batch = scheduler.pick_next_batch(available, current_count=2)

        assert len(batch) == 0

    def test_pick_next_batch_with_priority_overrides(self, sample_config):
        """Test that priority overrides are picked first."""
        sample_config.max_sessions = 3
        scheduler = Scheduler(config=sample_config)

        available = [
            create_mock_issue(1, priority="low"),
            create_mock_issue(2, priority="low"),
            create_mock_issue(3, priority="high"),
        ]

        # Override to prioritize issue 1
        batch = scheduler.pick_next_batch(
            available, current_count=0, priority_overrides=[1]
        )

        # Issue 1 should be first in the batch
        assert batch[0].number == 1

    def test_pick_next_batch_override_respects_max_sessions(self, sample_config):
        """Test that overrides don't exceed max sessions."""
        sample_config.max_sessions = 2
        scheduler = Scheduler(config=sample_config)

        available = [create_mock_issue(i, priority="low") for i in range(1, 6)]

        batch = scheduler.pick_next_batch(
            available, current_count=0, priority_overrides=[3, 4, 5]
        )

        # Should only pick 2 even though we have 3 overrides
        assert len(batch) == 2
        # Overrides should be picked first
        assert batch[0].number == 3
        assert batch[1].number == 4

    def test_pick_next_batch_override_missing_issue(self, sample_config):
        """Test that missing override issues are skipped gracefully."""
        sample_config.max_sessions = 3
        scheduler = Scheduler(config=sample_config)

        available = [create_mock_issue(i) for i in [1, 2]]

        # Override includes issue 99 which doesn't exist
        batch = scheduler.pick_next_batch(available, current_count=0, priority_overrides=[99, 1])

        assert len(batch) == 2
        assert batch[0].number == 1
        assert batch[1].number == 2

    def test_analyze_dependencies_single_blocked_by(self, sample_config):
        """Test analyzing single 'blocked by' dependency."""
        scheduler = Scheduler(config=sample_config)

        issues = [
            Issue(
                number=1,
                title="Base task",
                labels=[],
                body="Do this first",
            ),
            Issue(
                number=2,
                title="Dependent task",
                labels=[],
                body="blocked by #1",
            ),
        ]

        deps = scheduler.analyze_dependencies(issues)

        assert deps[2] == [1]
        assert 1 not in deps

    def test_analyze_dependencies_multiple_patterns(self, sample_config):
        """Test analyzing various dependency patterns."""
        scheduler = Scheduler(config=sample_config)

        issues = [
            Issue(
                number=1,
                title="First",
                labels=[],
                body="",
            ),
            Issue(
                number=2,
                title="Second",
                labels=[],
                body="blocked by #1",
            ),
            Issue(
                number=3,
                title="Third",
                labels=[],
                body="depends on #1",
            ),
            Issue(
                number=4,
                title="Fourth",
                labels=[],
                body="after #1",
            ),
            Issue(
                number=5,
                title="Fifth",
                labels=[],
                body="waiting for #1",
            ),
            Issue(
                number=6,
                title="Sixth",
                labels=[],
                body="requires #1",
            ),
        ]

        deps = scheduler.analyze_dependencies(issues)

        # All issues 2-6 should depend on 1
        for issue_num in [2, 3, 4, 5, 6]:
            assert deps[issue_num] == [1]

    def test_analyze_dependencies_multiple_blockers(self, sample_config):
        """Test analyzing issue with multiple blockers using different patterns."""
        scheduler = Scheduler(config=sample_config)

        issues = [
            Issue(number=1, title="First", labels=[], body=""),
            Issue(number=2, title="Second", labels=[], body=""),
            Issue(
                number=3,
                title="Third",
                labels=[],
                body="blocked by #1\ndepends on #2",
            ),
        ]

        deps = scheduler.analyze_dependencies(issues)

        assert set(deps[3]) == {1, 2}

    def test_analyze_dependencies_case_insensitive(self, sample_config):
        """Test that dependency analysis is case-insensitive."""
        scheduler = Scheduler(config=sample_config)

        issues = [
            Issue(number=1, title="First", labels=[], body=""),
            Issue(
                number=2,
                title="Second",
                labels=[],
                body="BLOCKED BY #1 and Blocked By #1",
            ),
        ]

        deps = scheduler.analyze_dependencies(issues)

        # Should find both despite case difference
        assert deps[2] == [1, 1] or deps[2] == [1]

    def test_analyze_dependencies_no_mentions(self, sample_config):
        """Test analyzing issue with no dependency mentions."""
        scheduler = Scheduler(config=sample_config)

        issues = [
            Issue(number=1, title="Task", labels=[], body="Just a regular task"),
        ]

        deps = scheduler.analyze_dependencies(issues)

        assert 1 not in deps

    def test_analyze_dependencies_no_body(self, sample_config):
        """Test analyzing issue with no body text."""
        scheduler = Scheduler(config=sample_config)

        issues = [
            Issue(number=1, title="Task", labels=[], body=None),
        ]

        deps = scheduler.analyze_dependencies(issues)

        assert 1 not in deps

    def test_analyze_dependencies_sorted_output(self, sample_config):
        """Test that dependencies are sorted in output."""
        scheduler = Scheduler(config=sample_config)

        issues = [
            Issue(
                number=1,
                title="Complex",
                labels=[],
                body="blocked by #5\ndepends on #2\nwaiting for #10",
            ),
        ]

        deps = scheduler.analyze_dependencies(issues)

        assert deps[1] == [2, 5, 10]

    def test_get_milestone_order(self, sample_config):
        """Test getting milestone order dictionary."""
        scheduler = Scheduler(config=sample_config)

        order = scheduler._get_milestone_order()

        assert order["M6"] == 0
        assert order["M7"] == 1
        assert order["M8"] == 2
        assert order["M9"] == 3
        assert order["M10"] == 4
        assert order["M11"] == 5


class TestSchedulerResult:
    """Test the SchedulerResult data class."""

    def test_scheduler_result_creation(self, sample_issues):
        """Test basic SchedulerResult creation."""
        result = SchedulerResult(
            issues_to_launch=[sample_issues[0]],
            blocked_issues=[(sample_issues[1], "Blocked by #1")],
        )

        assert len(result.issues_to_launch) == 1
        assert result.issues_to_launch[0].number == 1
        assert len(result.blocked_issues) == 1
        assert result.blocked_issues[0][0].number == 2
        assert result.blocked_issues[0][1] == "Blocked by #1"

    def test_scheduler_result_empty(self):
        """Test SchedulerResult with empty lists."""
        result = SchedulerResult(
            issues_to_launch=[],
            blocked_issues=[],
        )

        assert result.issues_to_launch == []
        assert result.blocked_issues == []
