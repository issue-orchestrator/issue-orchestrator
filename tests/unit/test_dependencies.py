"""Tests for dependency evaluation.

These tests verify the key invariants from the design docs:
- Satisfied: dependency issue is CLOSED
- Unsatisfied: dependency issue is OPEN
- Missing: dependency cannot be found (404/permission)
- Unknown: transient API error
- Runnable: false if any unsatisfied/missing/unknown
"""

import pytest
from unittest.mock import MagicMock

from issue_orchestrator.domain.dependencies import (
    Dependency,
    DependencyReport,
    DependencyState,
    parse_dependencies,
)
from issue_orchestrator.control.dependency_evaluator import DependencyEvaluator
from issue_orchestrator.ports import NullEventSink
from issue_orchestrator.ports.repository_host import DependencyIssueSnapshot


class TestParseDependencies:
    """Tests for parsing Depends-on lines from issue body."""

    def test_parse_simple_issue_reference(self):
        """Parse Depends-on: #123."""
        body = "Some text\n\nDepends-on: #123\n\nMore text"
        deps = parse_dependencies(body)
        assert deps == [(123, None)]

    def test_parse_multiple_dependencies(self):
        """Parse multiple Depends-on lines."""
        body = """
        Depends-on: #10
        Depends-on: #20
        Depends-on: #30
        """
        deps = parse_dependencies(body)
        assert deps == [(10, None), (20, None), (30, None)]

    def test_parse_cross_repo_dependency(self):
        """Parse Depends-on: owner/repo#123."""
        body = "Depends-on: acme/widgets#456"
        deps = parse_dependencies(body)
        assert deps == [(456, "acme/widgets")]

    def test_parse_case_insensitive(self):
        """Depends-on parsing is case insensitive."""
        body = "DEPENDS-ON: #100"
        deps = parse_dependencies(body)
        assert deps == [(100, None)]

    def test_parse_no_dependencies(self):
        """No Depends-on lines returns empty list."""
        body = "Just a regular issue body"
        deps = parse_dependencies(body)
        assert deps == []

    def test_parse_ignores_invalid_format(self):
        """Invalid formats are ignored."""
        body = """
        Depends-on: not a number
        Depends-on: #123
        Depends on: #456
        """
        deps = parse_dependencies(body)
        # Only #123 should be parsed (the second is wrong format "on:" vs "on: ")
        assert deps == [(123, None)]


class TestDependency:
    """Tests for Dependency dataclass."""

    def test_satisfied_dependency(self):
        """Satisfied dependency doesn't block."""
        dep = Dependency(issue_number=123, state=DependencyState.SATISFIED)
        assert dep.is_satisfied
        assert not dep.blocks_running

    def test_unsatisfied_dependency(self):
        """Unsatisfied dependency blocks."""
        dep = Dependency(issue_number=123, state=DependencyState.UNSATISFIED)
        assert not dep.is_satisfied
        assert dep.blocks_running

    def test_missing_dependency(self):
        """Missing dependency blocks."""
        dep = Dependency(
            issue_number=123,
            state=DependencyState.MISSING,
            error="Not found",
        )
        assert dep.blocks_running

    def test_unknown_dependency(self):
        """Unknown dependency blocks."""
        dep = Dependency(
            issue_number=123,
            state=DependencyState.UNKNOWN,
            error="Network error",
        )
        assert dep.blocks_running


class TestDependencyReport:
    """Tests for DependencyReport."""

    def test_no_dependencies_is_runnable(self):
        """Issue with no dependencies is runnable."""
        report = DependencyReport(issue_number=1)
        assert report.runnable
        assert report.summary() == "No dependencies"

    def test_all_satisfied_is_runnable(self):
        """Issue with all satisfied dependencies is runnable."""
        report = DependencyReport(
            issue_number=1,
            satisfied=(
                Dependency(issue_number=10, state=DependencyState.SATISFIED),
                Dependency(issue_number=20, state=DependencyState.SATISFIED),
            ),
        )
        assert report.runnable
        assert "2 dependencies satisfied" in report.summary()

    def test_unsatisfied_blocks_running(self):
        """Unsatisfied dependency blocks running."""
        report = DependencyReport(
            issue_number=1,
            satisfied=(
                Dependency(issue_number=10, state=DependencyState.SATISFIED),
            ),
            unsatisfied=(
                Dependency(issue_number=20, state=DependencyState.UNSATISFIED),
            ),
        )
        assert not report.runnable
        assert "waiting on: #20" in report.summary()

    def test_missing_blocks_running(self):
        """Missing dependency blocks running."""
        report = DependencyReport(
            issue_number=1,
            missing=(
                Dependency(
                    issue_number=99,
                    state=DependencyState.MISSING,
                    error="Not found",
                ),
            ),
        )
        assert not report.runnable
        assert report.has_warnings
        assert "missing: #99" in report.summary()

    def test_unknown_blocks_running(self):
        """Unknown dependency blocks running."""
        report = DependencyReport(
            issue_number=1,
            unknown=(
                Dependency(
                    issue_number=50,
                    state=DependencyState.UNKNOWN,
                    error="Network error",
                ),
            ),
        )
        assert not report.runnable
        assert report.has_warnings


class MockIssueChecker:
    """Mock issue checker for testing."""

    def __init__(self, default_milestone: str | None = "M1"):
        self.issues: dict[int, str] = {}  # issue_number -> state
        self.error_on: set[int] = set()  # issues that raise errors
        self._default_milestone = default_milestone

    def get_dependency_issue_snapshot(
        self,
        issue_number: int,
        repo: str | None = None,
    ) -> DependencyIssueSnapshot | None:
        if issue_number in self.error_on:
            raise Exception("API error")
        state = self.issues.get(issue_number)
        if state is None:
            return None
        return DependencyIssueSnapshot(state=state, milestone=self._default_milestone)

    def get_issue_state(self, issue_number: int, repo: str | None = None) -> str | None:
        if issue_number in self.error_on:
            raise Exception("API error")
        return self.issues.get(issue_number)

    def get_issue_milestone(self, issue_number: int, repo: str | None = None) -> str | None:
        if issue_number in self.error_on:
            raise Exception("API error")
        return self._default_milestone


class CollectingEventSink:
    """Event sink that collects events."""

    def __init__(self):
        self.events = []

    def publish(self, event):
        self.events.append(event)


class TestDependencyEvaluator:
    """Tests for DependencyEvaluator."""

    @pytest.fixture
    def checker(self):
        return MockIssueChecker()

    @pytest.fixture
    def events(self):
        return CollectingEventSink()

    @pytest.fixture
    def evaluator(self, checker, events):
        return DependencyEvaluator(issue_checker=checker, events=events)

    def test_satisfied_if_closed(self, evaluator, checker):
        """Dependency is satisfied if issue is CLOSED."""
        checker.issues[10] = "closed"

        report = evaluator.evaluate(
            issue_number=1,
            issue_body="Depends-on: #10",
            source_milestone="M1",
        )

        assert report.runnable
        assert len(report.satisfied) == 1
        assert report.satisfied[0].issue_number == 10

    def test_unsatisfied_if_open(self, evaluator, checker):
        """Dependency is unsatisfied if issue is OPEN."""
        checker.issues[10] = "open"

        report = evaluator.evaluate(
            issue_number=1,
            issue_body="Depends-on: #10",
            source_milestone="M1",
        )

        assert not report.runnable
        assert len(report.unsatisfied) == 1

    def test_missing_on_404(self, evaluator, checker):
        """Dependency is missing if issue not found."""
        # Issue 99 not in checker.issues -> returns None

        report = evaluator.evaluate(
            issue_number=1,
            issue_body="Depends-on: #99",
            source_milestone="M1",
        )

        assert not report.runnable
        assert len(report.missing) == 1
        assert report.has_warnings

    def test_unknown_on_api_error(self, evaluator, checker):
        """Dependency is unknown on transient API error."""
        checker.error_on.add(10)

        report = evaluator.evaluate(
            issue_number=1,
            issue_body="Depends-on: #10",
            source_milestone="M1",
        )

        assert not report.runnable
        assert len(report.unknown) == 1
        assert report.has_warnings

    def test_mixed_dependencies(self, evaluator, checker):
        """Test with mix of satisfied, unsatisfied, and missing."""
        checker.issues[10] = "closed"  # satisfied
        checker.issues[20] = "open"    # unsatisfied
        # 30 not in issues -> missing

        report = evaluator.evaluate(
            issue_number=1,
            issue_body="""
            Depends-on: #10
            Depends-on: #20
            Depends-on: #30
            """,
            source_milestone="M1",
        )

        assert not report.runnable
        assert len(report.satisfied) == 1
        assert len(report.unsatisfied) == 1
        assert len(report.missing) == 1

    def test_emits_event(self, evaluator, events, checker):
        """Evaluator emits trace event."""
        checker.issues[10] = "closed"

        evaluator.evaluate(
            issue_number=1,
            issue_body="Depends-on: #10",
            source_milestone="M1",
        )

        assert len(events.events) == 1
        event = events.events[0]
        assert event.name == "dependencies.evaluated"
        assert event.data["issue_number"] == 1
        assert event.data["runnable"] is True

    def test_no_dependencies_is_runnable(self, evaluator):
        """Issue with no dependencies is runnable."""
        report = evaluator.evaluate(
            issue_number=1,
            issue_body="No dependencies here",
        )

        assert report.runnable
        assert len(report.all_dependencies) == 0


class TestDependencyGating:
    """Integration-style tests for dependency gating behavior.

    These tests prove the key invariants from the design docs.
    """

    @pytest.fixture
    def checker(self):
        return MockIssueChecker()

    @pytest.fixture
    def evaluator(self, checker):
        return DependencyEvaluator(issue_checker=checker, events=NullEventSink())

    def test_b_depends_on_a_open_is_blocked(self, evaluator, checker):
        """B depends on A(open) -> blocked."""
        checker.issues[100] = "open"  # A is open

        report = evaluator.evaluate(
            issue_number=200,  # B
            issue_body="Depends-on: #100",  # depends on A
            source_milestone="M1",
        )

        assert not report.runnable, "B should be blocked when A is open"

    def test_b_depends_on_a_closed_is_runnable(self, evaluator, checker):
        """B depends on A(closed) -> runnable."""
        checker.issues[100] = "closed"  # A is closed

        report = evaluator.evaluate(
            issue_number=200,  # B
            issue_body="Depends-on: #100",  # depends on A
            source_milestone="M1",
        )

        assert report.runnable, "B should be runnable when A is closed"

    def test_b_depends_on_a_missing_is_blocked_with_warning(self, evaluator, checker):
        """B depends on A(missing) -> blocked + warning."""
        # A (issue 100) not in checker -> missing

        report = evaluator.evaluate(
            issue_number=200,  # B
            issue_body="Depends-on: #100",  # depends on A
            source_milestone="M1",
        )

        assert not report.runnable, "B should be blocked when A is missing"
        assert report.has_warnings, "Missing dependency should emit warning"

    def test_b_depends_on_a_unknown_is_blocked_with_warning(self, evaluator, checker):
        """B depends on A(unknown) -> blocked + warning."""
        checker.error_on.add(100)  # A causes API error

        report = evaluator.evaluate(
            issue_number=200,  # B
            issue_body="Depends-on: #100",  # depends on A
            source_milestone="M1",
        )

        assert not report.runnable, "B should be blocked when A is unknown"
        assert report.has_warnings, "Unknown dependency should emit warning"


class TestPriorityAndDependenciesTogether:
    """Test that issues with both priority and dependencies are handled correctly."""

    def test_issue_with_priority_and_dependencies_parsed_correctly(self):
        """Issue with [Px-nnn] title and Depends-on body has both parsed."""
        from issue_orchestrator.domain.models import Issue

        # Create issue with priority in title AND dependency in body
        issue = Issue(
            number=50,
            title="[P1-005] Implement feature X",
            labels=["agent:backend"],
            state="open",
            body="Description here.\n\nDepends-on: #10\nDepends-on: #20",
        )

        # Verify dependencies are extracted from body
        deps = parse_dependencies(issue.body)
        assert deps == [(10, None), (20, None)], "Should parse both dependencies"

        # Verify dependency evaluator blocks when deps are open
        checker = MockIssueChecker()
        checker.issues[10] = "open"  # First dep is open
        checker.issues[20] = "closed"  # Second dep is closed

        evaluator = DependencyEvaluator(issue_checker=checker, events=NullEventSink())
        report = evaluator.evaluate(
            issue_number=issue.number,
            issue_body=issue.body,
            source_milestone="M1",
        )

        assert not report.runnable, "Should be blocked - #10 is still open"
        assert len(report.unsatisfied) == 1
        assert report.unsatisfied[0].issue_number == 10


class TestDependencyStateTransitions:
    """Test that dependency state changes are detected on re-evaluation."""

    def test_missing_dependency_becomes_available_and_closed(self):
        """Issue blocked by missing dep becomes runnable when dep appears and is closed."""
        checker = MockIssueChecker()
        evaluator = DependencyEvaluator(issue_checker=checker, events=NullEventSink())

        issue_body = "Depends-on: #100"

        # First evaluation: dependency not found (MISSING)
        report1 = evaluator.evaluate(issue_number=1, issue_body=issue_body, source_milestone="M1")
        assert not report1.runnable, "Should be blocked - dependency missing"
        assert len(report1.missing) == 1
        assert report1.missing[0].issue_number == 100

        # Dependency appears and is closed
        checker.issues[100] = "closed"

        # Second evaluation: dependency now satisfied
        report2 = evaluator.evaluate(issue_number=1, issue_body=issue_body, source_milestone="M1")
        assert report2.runnable, "Should be runnable - dependency now exists and is closed"
        assert len(report2.satisfied) == 1
        assert report2.satisfied[0].issue_number == 100

    def test_missing_dependency_becomes_available_but_open(self):
        """Issue blocked by missing dep stays blocked if dep appears but is open."""
        checker = MockIssueChecker()
        evaluator = DependencyEvaluator(issue_checker=checker, events=NullEventSink())

        issue_body = "Depends-on: #100"

        # First evaluation: dependency not found (MISSING)
        report1 = evaluator.evaluate(issue_number=1, issue_body=issue_body, source_milestone="M1")
        assert not report1.runnable
        assert len(report1.missing) == 1

        # Dependency appears but is still open
        checker.issues[100] = "open"

        # Second evaluation: dependency exists but unsatisfied
        report2 = evaluator.evaluate(issue_number=1, issue_body=issue_body, source_milestone="M1")
        assert not report2.runnable, "Should still be blocked - dependency is open"
        assert len(report2.unsatisfied) == 1
        assert report2.unsatisfied[0].issue_number == 100
