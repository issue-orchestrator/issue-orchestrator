"""Unit tests for the scheduler module."""

import pytest
from unittest.mock import MagicMock, patch
from issue_orchestrator.scheduler import (
    Scheduler, SchedulerResult, DueDateStrategy, NumberStrategy,
    PatternStrategy, NameStrategy, get_milestone_strategy, load_strategy_class,
    BUILTIN_STRATEGIES
)
from issue_orchestrator.models import Issue
from issue_orchestrator.config import Config


def create_mock_issue(number, priority=None, milestone=None, state="open", milestone_number=None, milestone_due_on=None):
    """Helper to create mock GitHub issue objects."""
    mock_issue = MagicMock()
    mock_issue.number = number
    # Milestone must be a string or None, not MagicMock
    mock_issue.milestone = milestone
    mock_issue.state = state
    mock_issue.milestone_number = milestone_number
    mock_issue.milestone_due_on = milestone_due_on

    # Labels are plain strings, not objects with .name
    if priority:
        mock_issue.labels = [f"priority:{priority}"]
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
        """Test sorting with milestone priority using pattern strategy."""
        sample_config.milestone_sort = "pattern"
        sample_config.milestone_sort_config = {"pattern": r"M(\d+)"}
        scheduler = Scheduler(config=sample_config)

        # Create mock issues with milestone
        # Milestones and labels are plain strings, not MagicMock objects
        issue_m6 = create_mock_issue(1, priority="high", milestone="M6")
        issue_m7 = create_mock_issue(2, priority="high", milestone="M7")
        issue_no_m = create_mock_issue(3, priority="high", milestone=None)

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
        sample_config.max_concurrent_sessions = 2
        scheduler = Scheduler(config=sample_config)

        available = [create_mock_issue(i, priority="high") for i in range(1, 6)]

        # Current count is 0, so we can pick 2
        batch = scheduler.pick_next_batch(available, current_count=0)

        assert len(batch) == 2

    def test_pick_next_batch_with_current_sessions(self, sample_config):
        """Test picking batch when some sessions are already active."""
        sample_config.max_concurrent_sessions = 3
        scheduler = Scheduler(config=sample_config)

        available = [create_mock_issue(i, priority="high") for i in range(1, 6)]

        # Current count is 2, so we can pick 1 more
        batch = scheduler.pick_next_batch(available, current_count=2)

        assert len(batch) == 1

    def test_pick_next_batch_no_slots_available(self, sample_config):
        """Test picking batch when no slots are available."""
        sample_config.max_concurrent_sessions = 2
        scheduler = Scheduler(config=sample_config)

        available = [create_mock_issue(i, priority="high") for i in range(1, 4)]

        # Current count equals max sessions
        batch = scheduler.pick_next_batch(available, current_count=2)

        assert len(batch) == 0

    def test_pick_next_batch_with_priority_overrides(self, sample_config):
        """Test that priority overrides are picked first."""
        sample_config.max_concurrent_sessions = 3
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
        sample_config.max_concurrent_sessions = 2
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
        sample_config.max_concurrent_sessions = 3
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


class TestMilestoneSortStrategies:
    """Test milestone sorting strategies."""

    def test_due_date_strategy_with_dates(self):
        """Test DueDateStrategy sorts by due date."""
        strategy = DueDateStrategy()

        issue1 = Issue(
            number=1, title="First", labels=[],
            milestone="M1", milestone_due_on="2025-01-15T00:00:00Z"
        )
        issue2 = Issue(
            number=2, title="Second", labels=[],
            milestone="M2", milestone_due_on="2025-01-10T00:00:00Z"
        )
        issue3 = Issue(
            number=3, title="Third", labels=[],
            milestone="M3", milestone_due_on="2025-01-20T00:00:00Z"
        )

        key1 = strategy.get_sort_key(issue1)
        key2 = strategy.get_sort_key(issue2)
        key3 = strategy.get_sort_key(issue3)

        # Earlier dates should sort first
        assert key2 < key1 < key3

    def test_due_date_strategy_nulls_last(self):
        """Test DueDateStrategy places nulls at the end."""
        strategy = DueDateStrategy()

        issue_with_date = Issue(
            number=1, title="Has date", labels=[],
            milestone="M1", milestone_due_on="2025-01-15T00:00:00Z"
        )
        issue_no_date = Issue(
            number=2, title="No date", labels=[],
            milestone="M2", milestone_due_on=None
        )

        key_with_date = strategy.get_sort_key(issue_with_date)
        key_no_date = strategy.get_sort_key(issue_no_date)

        assert key_with_date < key_no_date
        assert key_no_date == (float("inf"),)

    def test_due_date_strategy_invalid_date(self):
        """Test DueDateStrategy handles invalid dates gracefully."""
        strategy = DueDateStrategy()

        issue = Issue(
            number=1, title="Bad date", labels=[],
            milestone="M1", milestone_due_on="invalid-date"
        )

        key = strategy.get_sort_key(issue)
        assert key == (float("inf"),)

    def test_number_strategy_with_numbers(self):
        """Test NumberStrategy sorts by milestone number."""
        strategy = NumberStrategy()

        issue1 = Issue(
            number=1, title="First", labels=[],
            milestone="M6", milestone_number=6
        )
        issue2 = Issue(
            number=2, title="Second", labels=[],
            milestone="M7", milestone_number=7
        )
        issue3 = Issue(
            number=3, title="Third", labels=[],
            milestone="M5", milestone_number=5
        )

        key1 = strategy.get_sort_key(issue1)
        key2 = strategy.get_sort_key(issue2)
        key3 = strategy.get_sort_key(issue3)

        # Lower numbers sort first
        assert key3 < key1 < key2
        assert key3 == (5,)
        assert key1 == (6,)
        assert key2 == (7,)

    def test_number_strategy_nulls_last(self):
        """Test NumberStrategy places nulls at the end."""
        strategy = NumberStrategy()

        issue_with_num = Issue(
            number=1, title="Has number", labels=[],
            milestone="M6", milestone_number=6
        )
        issue_no_num = Issue(
            number=2, title="No number", labels=[],
            milestone="Some Milestone", milestone_number=None
        )

        key_with_num = strategy.get_sort_key(issue_with_num)
        key_no_num = strategy.get_sort_key(issue_no_num)

        assert key_with_num < key_no_num
        assert key_no_num == (float("inf"),)

    def test_pattern_strategy_default_pattern(self):
        """Test PatternStrategy with default M(\\d+) pattern."""
        strategy = PatternStrategy(r"M(\d+)")

        issue1 = Issue(number=1, title="First", labels=[], milestone="M6")
        issue2 = Issue(number=2, title="Second", labels=[], milestone="M10")
        issue3 = Issue(number=3, title="Third", labels=[], milestone="M7")

        key1 = strategy.get_sort_key(issue1)
        key2 = strategy.get_sort_key(issue2)
        key3 = strategy.get_sort_key(issue3)

        # Extract numbers and sort
        assert key1 == (6,)
        assert key2 == (10,)
        assert key3 == (7,)
        assert key1 < key3 < key2

    def test_pattern_strategy_custom_pattern(self):
        """Test PatternStrategy with custom pattern."""
        strategy = PatternStrategy(r"Sprint (\d+)")

        issue1 = Issue(number=1, title="First", labels=[], milestone="Sprint 15")
        issue2 = Issue(number=2, title="Second", labels=[], milestone="Sprint 12")
        issue3 = Issue(number=3, title="Third", labels=[], milestone="Sprint 20")

        key1 = strategy.get_sort_key(issue1)
        key2 = strategy.get_sort_key(issue2)
        key3 = strategy.get_sort_key(issue3)

        assert key1 == (15,)
        assert key2 == (12,)
        assert key3 == (20,)
        assert key2 < key1 < key3

    def test_pattern_strategy_no_match(self):
        """Test PatternStrategy when pattern doesn't match."""
        strategy = PatternStrategy(r"M(\d+)")

        issue_no_match = Issue(
            number=1, title="No match", labels=[],
            milestone="Random Milestone"
        )
        issue_no_milestone = Issue(
            number=2, title="No milestone", labels=[],
            milestone=None
        )

        key_no_match = strategy.get_sort_key(issue_no_match)
        key_no_milestone = strategy.get_sort_key(issue_no_milestone)

        # Both should be infinity
        assert key_no_match == (float("inf"),)
        assert key_no_milestone == (float("inf"),)

    def test_name_strategy_alphabetical(self):
        """Test NameStrategy sorts alphabetically."""
        strategy = NameStrategy()

        issue1 = Issue(number=1, title="First", labels=[], milestone="Beta")
        issue2 = Issue(number=2, title="Second", labels=[], milestone="Alpha")
        issue3 = Issue(number=3, title="Third", labels=[], milestone="Gamma")

        key1 = strategy.get_sort_key(issue1)
        key2 = strategy.get_sort_key(issue2)
        key3 = strategy.get_sort_key(issue3)

        assert key1 == ("Beta",)
        assert key2 == ("Alpha",)
        assert key3 == ("Gamma",)
        assert key2 < key1 < key3

    def test_name_strategy_nulls_last(self):
        """Test NameStrategy places nulls at the end."""
        strategy = NameStrategy()

        issue_with_name = Issue(
            number=1, title="Has name", labels=[],
            milestone="Milestone A"
        )
        issue_no_name = Issue(
            number=2, title="No name", labels=[],
            milestone=None
        )

        key_with_name = strategy.get_sort_key(issue_with_name)
        key_no_name = strategy.get_sort_key(issue_no_name)

        assert key_with_name < key_no_name
        assert key_no_name == ("\uffff",)

    def test_get_milestone_strategy_due_date(self):
        """Test factory creates DueDateStrategy."""
        config = Config()
        config.milestone_sort = "due_date"

        strategy = get_milestone_strategy(config)
        assert isinstance(strategy, DueDateStrategy)

    def test_get_milestone_strategy_number(self):
        """Test factory creates NumberStrategy."""
        config = Config()
        config.milestone_sort = "number"

        strategy = get_milestone_strategy(config)
        assert isinstance(strategy, NumberStrategy)

    def test_get_milestone_strategy_pattern(self):
        """Test factory creates PatternStrategy."""
        config = Config()
        config.milestone_sort = "pattern"
        config.milestone_sort_config = {"pattern": r"Sprint (\d+)"}

        strategy = get_milestone_strategy(config)
        assert isinstance(strategy, PatternStrategy)
        assert strategy.pattern.pattern == r"Sprint (\d+)"

    def test_get_milestone_strategy_name(self):
        """Test factory creates NameStrategy."""
        config = Config()
        config.milestone_sort = "name"

        strategy = get_milestone_strategy(config)
        assert isinstance(strategy, NameStrategy)

    def test_get_milestone_strategy_invalid(self):
        """Test factory raises error for invalid strategy."""
        config = Config()
        config.milestone_sort = "invalid_strategy"

        with pytest.raises(ValueError, match="Cannot load strategy class"):
            get_milestone_strategy(config)

    def test_get_milestone_strategy_case_insensitive(self):
        """Test factory is case-insensitive."""
        config = Config()
        config.milestone_sort = "DUE_DATE"

        strategy = get_milestone_strategy(config)
        assert isinstance(strategy, DueDateStrategy)

    def test_load_strategy_class_builtin(self):
        """Test load_strategy_class can load built-in strategies."""
        cls = load_strategy_class("issue_orchestrator.scheduler.DueDateStrategy")
        assert cls is DueDateStrategy

    def test_load_strategy_class_invalid_module(self):
        """Test load_strategy_class raises for invalid module."""
        with pytest.raises(ValueError, match="Cannot load strategy class"):
            load_strategy_class("nonexistent.module.Strategy")

    def test_load_strategy_class_invalid_class(self):
        """Test load_strategy_class raises for invalid class name."""
        with pytest.raises(ValueError, match="Cannot load strategy class"):
            load_strategy_class("issue_orchestrator.scheduler.NonexistentStrategy")

    def test_get_milestone_strategy_full_module_path(self):
        """Test factory accepts full module path (same mechanism as user plugins)."""
        config = Config()
        # Use full module path instead of alias - proves dynamic import works
        config.milestone_sort = "issue_orchestrator.scheduler.NumberStrategy"

        strategy = get_milestone_strategy(config)
        assert isinstance(strategy, NumberStrategy)

    def test_builtin_strategies_registry(self):
        """Test all builtin aliases map to valid module paths."""
        for alias, module_path in BUILTIN_STRATEGIES.items():
            cls = load_strategy_class(module_path)
            assert cls is not None, f"Failed to load {alias} -> {module_path}"

    def test_strict_kwargs_rejects_unknown_args(self):
        """Test that unknown config arguments raise TypeError (strict mode)."""
        config = Config()
        config.milestone_sort = "due_date"
        config.milestone_sort_config = {"unknown_param": "value"}

        # DueDateStrategy doesn't accept any kwargs, so this should fail
        # Error message varies by Python version: "takes no arguments" or "unexpected keyword argument"
        with pytest.raises(TypeError):
            get_milestone_strategy(config)

    def test_strategy_kwargs_uniform_with_plugins(self):
        """Test that built-in and plugin strategies use the same kwargs mechanism."""
        # This test proves a user-defined plugin would work the same way
        config = Config()

        # Using full module path (as a plugin would be specified)
        config.milestone_sort = "issue_orchestrator.scheduler.PatternStrategy"
        config.milestone_sort_config = {"pattern": r"v(\d+)"}

        strategy = get_milestone_strategy(config)
        assert isinstance(strategy, PatternStrategy)
        assert strategy.pattern.pattern == r"v(\d+)"


class TestSchedulerWithStrategies:
    """Test Scheduler integration with milestone strategies."""

    def test_scheduler_uses_default_strategy(self, sample_config):
        """Test Scheduler uses default due_date strategy from config."""
        sample_config.milestone_sort = "due_date"
        scheduler = Scheduler(config=sample_config)

        assert isinstance(scheduler.milestone_strategy, DueDateStrategy)

    def test_scheduler_with_custom_strategy(self, sample_config):
        """Test Scheduler can accept custom strategy."""
        custom_strategy = NumberStrategy()
        scheduler = Scheduler(config=sample_config, milestone_strategy=custom_strategy)

        assert scheduler.milestone_strategy is custom_strategy

    def test_sort_by_priority_with_due_date_strategy(self, sample_config):
        """Test sorting with DueDateStrategy."""
        sample_config.milestone_sort = "due_date"
        scheduler = Scheduler(config=sample_config)

        issues = [
            Issue(
                number=1, title="Later", labels=["priority:high"],
                milestone="M2", milestone_due_on="2025-01-20T00:00:00Z"
            ),
            Issue(
                number=2, title="Earlier", labels=["priority:high"],
                milestone="M1", milestone_due_on="2025-01-10T00:00:00Z"
            ),
            Issue(
                number=3, title="No date", labels=["priority:high"],
                milestone="M3", milestone_due_on=None
            ),
        ]

        sorted_issues = scheduler.sort_by_priority(issues)

        # Should sort by due date: earlier first, no date last
        assert sorted_issues[0].number == 2
        assert sorted_issues[1].number == 1
        assert sorted_issues[2].number == 3

    def test_sort_by_priority_with_number_strategy(self, sample_config):
        """Test sorting with NumberStrategy."""
        sample_config.milestone_sort = "number"
        scheduler = Scheduler(config=sample_config)

        issues = [
            Issue(
                number=1, title="M7", labels=["priority:high"],
                milestone="M7", milestone_number=7
            ),
            Issue(
                number=2, title="M5", labels=["priority:high"],
                milestone="M5", milestone_number=5
            ),
            Issue(
                number=3, title="M10", labels=["priority:high"],
                milestone="M10", milestone_number=10
            ),
        ]

        sorted_issues = scheduler.sort_by_priority(issues)

        # Should sort by milestone number
        assert sorted_issues[0].number == 2  # M5
        assert sorted_issues[1].number == 1  # M7
        assert sorted_issues[2].number == 3  # M10

    def test_sort_by_priority_with_pattern_strategy(self, sample_config):
        """Test sorting with PatternStrategy."""
        sample_config.milestone_sort = "pattern"
        sample_config.milestone_sort_config = {"pattern": r"M(\d+)"}
        scheduler = Scheduler(config=sample_config)

        issues = [
            Issue(number=1, title="M7", labels=["priority:high"], milestone="M7"),
            Issue(number=2, title="M5", labels=["priority:high"], milestone="M5"),
            Issue(number=3, title="No match", labels=["priority:high"], milestone="Random"),
        ]

        sorted_issues = scheduler.sort_by_priority(issues)

        # Should sort by extracted number, no match goes last
        assert sorted_issues[0].number == 2  # M5
        assert sorted_issues[1].number == 1  # M7
        assert sorted_issues[2].number == 3  # Random

    def test_sort_by_priority_milestone_then_priority(self, sample_config):
        """Test sorting respects milestone first, then priority."""
        sample_config.milestone_sort = "number"
        scheduler = Scheduler(config=sample_config)

        issues = [
            Issue(
                number=1, title="M7 low", labels=["priority:low"],
                milestone="M7", milestone_number=7
            ),
            Issue(
                number=2, title="M5 high", labels=["priority:high"],
                milestone="M5", milestone_number=5
            ),
            Issue(
                number=3, title="M5 low", labels=["priority:low"],
                milestone="M5", milestone_number=5
            ),
        ]

        sorted_issues = scheduler.sort_by_priority(issues)

        # M5 issues come first, then within M5 priority matters
        assert sorted_issues[0].number == 2  # M5 high
        assert sorted_issues[1].number == 3  # M5 low
        assert sorted_issues[2].number == 1  # M7 low
