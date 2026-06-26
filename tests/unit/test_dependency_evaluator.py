"""Behavior-centric unit tests for DependencyEvaluator.

These tests focus on real behaviors and edge cases, particularly:
1. External ID resolution with IssueResolver
2. Missing resolver configuration
3. Non-int handle resolution (e.g., from non-GitHub resolvers)
4. Circular dependency and blocked issue detection patterns
5. State change handling

Coverage targets the uncovered lines (151-181, 190) in dependency_evaluator.py.
"""

import pytest
from unittest.mock import MagicMock

from issue_orchestrator.control.dependency_evaluator import DependencyEvaluator
from issue_orchestrator.domain.dependencies import (
    Dependency,
    DependencyReport,
    DependencyState,
    ParsedDependencyRef,
    parse_dependency_refs,
)
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.ports import NullEventSink
from issue_orchestrator.ports.repository_host import DependencyIssueSnapshot


# =============================================================================
# Test Fixtures
# =============================================================================


class MockIssueChecker:
    """Mock issue state checker for testing."""

    def __init__(self, default_milestone: str | None = "M1"):
        self.issues: dict[int, str] = {}  # issue_number -> state
        self.milestones: dict[int, str | None] = {}  # issue_number -> milestone
        self.cross_repo_issues: dict[tuple[int, str], str] = {}  # (number, repo) -> state
        self.cross_repo_milestones: dict[tuple[int, str], str | None] = {}  # (number, repo) -> milestone
        self.error_on: set[int] = set()  # issues that raise errors
        self._default_milestone = default_milestone  # Default milestone for issues not in milestones dict
        self.snapshot_calls: list[tuple[int, str | None]] = []
        self.state_calls: list[tuple[int, str | None]] = []
        self.milestone_calls: list[tuple[int, str | None]] = []

    def get_dependency_issue_snapshot(
        self,
        issue_number: int,
        repo: str | None = None,
    ) -> DependencyIssueSnapshot | None:
        self.snapshot_calls.append((issue_number, repo))
        if issue_number in self.error_on:
            raise Exception("Transient API error")
        if repo:
            state = self.cross_repo_issues.get((issue_number, repo))
            milestone = self.cross_repo_milestones.get((issue_number, repo), self._default_milestone)
        else:
            state = self.issues.get(issue_number)
            milestone = self.milestones.get(issue_number, self._default_milestone)
        if state is None:
            return None
        return DependencyIssueSnapshot(state=state, milestone=milestone)

    def get_issue_state(self, issue_number: int, repo: str | None = None) -> str | None:
        self.state_calls.append((issue_number, repo))
        if issue_number in self.error_on:
            raise Exception("Transient API error")
        if repo:
            return self.cross_repo_issues.get((issue_number, repo))
        return self.issues.get(issue_number)

    def get_issue_milestone(self, issue_number: int, repo: str | None = None) -> str | None:
        self.milestone_calls.append((issue_number, repo))
        if issue_number in self.error_on:
            raise Exception("Transient API error")
        if repo:
            return self.cross_repo_milestones.get((issue_number, repo), self._default_milestone)
        return self.milestones.get(issue_number, self._default_milestone)


class MockIssueResolver:
    """Mock IssueResolver that can resolve external IDs to issue numbers."""

    def __init__(self):
        self.index: dict[str, int | str | None] = {}  # external_id -> handle

    def resolve(self, key) -> int | str | None:
        """Resolve an IssueKey to a handle (int for GitHub, could be str for other stores)."""
        return self.index.get(key.external_id)

    def build_index(self) -> None:
        pass

    def invalidate(self, key) -> None:
        pass


class CollectingEventSink:
    """Event sink that collects events for verification."""

    def __init__(self):
        self.events = []

    def publish(self, event) -> None:
        self.events.append(event)

    def get_events_by_name(self, name: str) -> list:
        return [e for e in self.events if e.name == name]


@pytest.fixture
def checker():
    return MockIssueChecker()


@pytest.fixture
def events():
    return CollectingEventSink()


@pytest.fixture
def resolver():
    return MockIssueResolver()


@pytest.fixture
def evaluator(checker, events):
    """Basic evaluator without resolver (for issue number refs only)."""
    return DependencyEvaluator(issue_checker=checker, events=events)


@pytest.fixture
def evaluator_with_resolver(checker, events, resolver):
    """Evaluator with resolver configured (for external ID refs)."""
    return DependencyEvaluator(
        issue_checker=checker,
        events=events,
        issue_resolver=resolver,
        repo="owner/repo",
    )


# =============================================================================
# External ID Resolution Tests (Lines 151-186)
# =============================================================================


class TestExternalIdWithNoResolver:
    """Test external ID references when no resolver is configured (Lines 151-161)."""

    def test_external_id_without_resolver_returns_unknown(self, checker, events):
        """External ID dependency without resolver configured returns UNKNOWN state."""
        # No resolver configured
        evaluator = DependencyEvaluator(issue_checker=checker, events=events)

        report = evaluator.evaluate(
            issue_number=1,
            issue_body="Depends-on: M1-001",
            source_milestone="M1",
        )

        assert not report.runnable, "Should be blocked"
        assert len(report.unknown) == 1, "Should have one unknown dependency"
        dep = report.unknown[0]
        assert dep.external_id == "M1-001"
        assert dep.state == DependencyState.UNKNOWN
        assert "No resolver configured" in dep.error

    def test_external_id_with_resolver_but_no_repo_returns_unknown(self, checker, events, resolver):
        """External ID with resolver but no repo configured returns UNKNOWN."""
        # Resolver configured but no repo
        evaluator = DependencyEvaluator(
            issue_checker=checker,
            events=events,
            issue_resolver=resolver,
            repo=None,  # Missing repo
        )

        report = evaluator.evaluate(
            issue_number=1,
            issue_body="Depends-on: M1-002",
            source_milestone="M1",
        )

        assert not report.runnable
        assert len(report.unknown) == 1
        assert "No resolver configured" in report.unknown[0].error


class TestExternalIdResolution:
    """Test external ID resolution via IssueResolver (Lines 163-186)."""

    def test_external_id_resolved_to_closed_issue_is_satisfied(
        self, evaluator_with_resolver, checker, resolver
    ):
        """External ID that resolves to a closed issue is SATISFIED."""
        # Set up: M1-010 maps to issue #42 which is closed
        resolver.index["M1-010"] = 42
        checker.issues[42] = "closed"

        report = evaluator_with_resolver.evaluate(
            issue_number=1,
            issue_body="Depends-on: M1-010",
            source_milestone="M1",
        )

        assert report.runnable, "Should be runnable when dependency is satisfied"
        assert len(report.satisfied) == 1
        dep = report.satisfied[0]
        assert dep.external_id == "M1-010"
        assert dep.issue_number == 42
        assert dep.state == DependencyState.SATISFIED

    def test_external_id_resolved_to_open_issue_is_unsatisfied(
        self, evaluator_with_resolver, checker, resolver
    ):
        """External ID that resolves to an open issue is UNSATISFIED."""
        resolver.index["M2-005"] = 99
        checker.issues[99] = "open"

        report = evaluator_with_resolver.evaluate(
            issue_number=1,
            issue_body="Depends-on: M2-005",
            source_milestone="M1",
        )

        assert not report.runnable
        assert len(report.unsatisfied) == 1
        dep = report.unsatisfied[0]
        assert dep.external_id == "M2-005"
        assert dep.issue_number == 99

    def test_external_id_not_found_in_resolver_is_missing(
        self, evaluator_with_resolver, resolver
    ):
        """External ID that resolver cannot find returns MISSING (Lines 168-174)."""
        # M3-999 is not in the resolver index
        report = evaluator_with_resolver.evaluate(
            issue_number=1,
            issue_body="Depends-on: M3-999",
            source_milestone="M1",
        )

        assert not report.runnable
        assert len(report.missing) == 1
        dep = report.missing[0]
        assert dep.external_id == "M3-999"
        assert dep.issue_number is None
        assert dep.state == DependencyState.MISSING
        assert "Could not resolve" in dep.error

    def test_resolver_query_failure_returns_unknown_not_missing(
        self, evaluator_with_resolver, resolver
    ):
        """Infrastructure failure from resolver.resolve() (e.g. search API
        rate-limited or 5xx) must classify as UNKNOWN, not MISSING.

        Regression for PR #6356 review finding B2: collapsing query failures
        into MISSING re-creates the dependency_blocked failure mode the
        rewrite was meant to fix — a transient API hiccup would permanently
        stamp the dep as not-existing.
        """
        from issue_orchestrator.ports.repository_host import RepositoryHostError

        def _raise(_key):
            raise RepositoryHostError("rate limited")

        resolver.resolve = _raise  # type: ignore[assignment]

        report = evaluator_with_resolver.evaluate(
            issue_number=1,
            issue_body="Depends-on: M3-999",
            source_milestone="M1",
        )

        assert not report.runnable
        assert len(report.missing) == 0  # NOT classified as missing
        assert len(report.unknown) == 1
        dep = report.unknown[0]
        assert dep.external_id == "M3-999"
        assert dep.state == DependencyState.UNKNOWN
        assert "Resolver query failed" in dep.error

    def test_resolver_programming_error_propagates(
        self, evaluator_with_resolver, resolver
    ):
        """Non-RepositoryHostError exceptions (programming bugs) must
        propagate rather than getting laundered into UNKNOWN/MISSING.
        Fail-fast over silent miscategorization.
        """
        def _raise(_key):
            raise TypeError("bad call")

        resolver.resolve = _raise  # type: ignore[assignment]

        with pytest.raises(TypeError):
            evaluator_with_resolver.evaluate(
                issue_number=1,
                issue_body="Depends-on: M3-999",
                source_milestone="M1",
            )

    def test_non_int_handle_from_resolver_returns_unknown(
        self, checker, events, resolver
    ):
        """Resolver returning non-int handle (e.g., string path) returns UNKNOWN (Lines 177-186)."""
        # Simulate a file-based resolver returning a path string
        resolver.index["M4-001"] = "/path/to/issue.json"  # Non-int handle

        evaluator = DependencyEvaluator(
            issue_checker=checker,
            events=events,
            issue_resolver=resolver,
            repo="owner/repo",
        )

        report = evaluator.evaluate(
            issue_number=1,
            issue_body="Depends-on: M4-001",
            source_milestone="M1",
        )

        assert not report.runnable
        assert len(report.unknown) == 1
        dep = report.unknown[0]
        assert dep.external_id == "M4-001"
        assert "non-int handle" in dep.error.lower()
        assert "str" in dep.error


# =============================================================================
# Dependency Graph Evaluation Behaviors
# =============================================================================


class TestDependencyGraphEvaluation:
    """Test dependency graph evaluation patterns."""

    def test_multiple_dependencies_all_satisfied(self, evaluator, checker):
        """All dependencies satisfied means issue is runnable."""
        checker.issues[10] = "closed"
        checker.issues[20] = "closed"
        checker.issues[30] = "closed"

        report = evaluator.evaluate(
            issue_number=1,
            issue_body="""
            Depends-on: #10
            Depends-on: #20
            Depends-on: #30
            """,
            source_milestone="M1",
        )

        assert report.runnable
        assert len(report.satisfied) == 3
        assert len(report.all_dependencies) == 3
        assert checker.snapshot_calls == [(10, None), (20, None), (30, None)]
        assert checker.state_calls == []
        assert checker.milestone_calls == []

    def test_single_unsatisfied_blocks_even_with_many_satisfied(self, evaluator, checker):
        """One unsatisfied dependency blocks the issue."""
        checker.issues[10] = "closed"
        checker.issues[20] = "open"  # Blocker
        checker.issues[30] = "closed"

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
        assert len(report.satisfied) == 2
        assert len(report.unsatisfied) == 1
        assert report.unsatisfied[0].issue_number == 20

    def test_mixed_state_dependencies(self, evaluator, checker):
        """Mixed states: satisfied, unsatisfied, missing, unknown."""
        checker.issues[10] = "closed"   # satisfied
        checker.issues[20] = "open"     # unsatisfied
        # #30 not in checker -> missing
        checker.error_on.add(40)        # unknown (API error)

        report = evaluator.evaluate(
            issue_number=1,
            issue_body="""
            Depends-on: #10
            Depends-on: #20
            Depends-on: #30
            Depends-on: #40
            """,
            source_milestone="M1",
        )

        assert not report.runnable
        assert len(report.satisfied) == 1
        assert len(report.unsatisfied) == 1
        assert len(report.missing) == 1
        assert len(report.unknown) == 1
        assert report.has_warnings

    def test_blocking_dependencies_property(self, evaluator, checker):
        """blocking_dependencies returns all non-satisfied dependencies."""
        checker.issues[10] = "closed"
        checker.issues[20] = "open"
        # #30 missing

        report = evaluator.evaluate(
            issue_number=1,
            issue_body="""
            Depends-on: #10
            Depends-on: #20
            Depends-on: #30
            """,
            source_milestone="M1",
        )

        blocking = report.blocking_dependencies
        assert len(blocking) == 2
        assert all(d.blocks_running for d in blocking)


# =============================================================================
# Cross-Repository Dependencies
# =============================================================================


class TestCrossRepoDependencies:
    """Test cross-repository dependency references."""

    def test_cross_repo_dependency_satisfied(self, evaluator, checker):
        """Cross-repo dependency (owner/repo#123) can be satisfied."""
        checker.cross_repo_issues[(456, "acme/widgets")] = "closed"

        report = evaluator.evaluate(
            issue_number=1,
            issue_body="Depends-on: acme/widgets#456",
            source_milestone="M1",
        )

        assert report.runnable
        assert len(report.satisfied) == 1
        dep = report.satisfied[0]
        assert dep.issue_number == 456
        assert dep.repository == "acme/widgets"

    def test_cross_repo_dependency_unsatisfied(self, evaluator, checker):
        """Cross-repo dependency that is open blocks the issue."""
        checker.cross_repo_issues[(789, "other/project")] = "open"

        report = evaluator.evaluate(
            issue_number=1,
            issue_body="Depends-on: other/project#789",
            source_milestone="M1",
        )

        assert not report.runnable
        assert len(report.unsatisfied) == 1

    def test_cross_repo_dependency_not_found(self, evaluator, checker):
        """Cross-repo dependency not found returns MISSING."""
        # Issue not in cross_repo_issues -> returns None

        report = evaluator.evaluate(
            issue_number=1,
            issue_body="Depends-on: unknown/repo#999",
            source_milestone="M1",
        )

        assert not report.runnable
        assert len(report.missing) == 1
        dep = report.missing[0]
        assert dep.repository == "unknown/repo"
        assert dep.issue_number == 999


# =============================================================================
# Blocked Issue Identification
# =============================================================================


class TestBlockedIssueIdentification:
    """Test patterns for identifying blocked issues."""

    def test_issue_blocked_by_open_dependency(self, evaluator, checker):
        """Issue is blocked when dependency is open."""
        checker.issues[100] = "open"

        report = evaluator.evaluate(
            issue_number=200,
            issue_body="Depends-on: #100",
            source_milestone="M1",
        )

        assert not report.runnable
        assert "#100" in report.summary()
        assert "waiting on" in report.summary()

    def test_issue_blocked_by_missing_dependency(self, evaluator, checker):
        """Issue is blocked when dependency is missing (with warning)."""
        # Issue 999 doesn't exist

        report = evaluator.evaluate(
            issue_number=200,
            issue_body="Depends-on: #999",
            source_milestone="M1",
        )

        assert not report.runnable
        assert report.has_warnings
        assert "missing" in report.summary()

    def test_issue_blocked_by_api_error(self, evaluator, checker):
        """Issue is blocked when API error checking dependency (with warning)."""
        checker.error_on.add(100)

        report = evaluator.evaluate(
            issue_number=200,
            issue_body="Depends-on: #100",
            source_milestone="M1",
        )

        assert not report.runnable
        assert report.has_warnings
        assert "unknown" in report.summary()


# =============================================================================
# State Changes Affecting Dependencies
# =============================================================================


class TestStateChanges:
    """Test re-evaluation when dependency states change."""

    def test_dependency_transitions_from_open_to_closed(self, checker, events):
        """Dependency becoming closed unblocks the issue."""
        evaluator = DependencyEvaluator(issue_checker=checker, events=events)
        issue_body = "Depends-on: #100"

        # Initially open
        checker.issues[100] = "open"
        report1 = evaluator.evaluate(issue_number=1, issue_body=issue_body, source_milestone="M1")
        assert not report1.runnable

        # Dependency closed
        checker.issues[100] = "closed"
        report2 = evaluator.evaluate(issue_number=1, issue_body=issue_body, source_milestone="M1")
        assert report2.runnable

    def test_dependency_transitions_from_missing_to_closed(self, checker, events):
        """Missing dependency becoming available and closed unblocks."""
        evaluator = DependencyEvaluator(issue_checker=checker, events=events)
        issue_body = "Depends-on: #100"

        # Initially missing
        report1 = evaluator.evaluate(issue_number=1, issue_body=issue_body, source_milestone="M1")
        assert not report1.runnable
        assert len(report1.missing) == 1

        # Issue created and closed
        checker.issues[100] = "closed"
        report2 = evaluator.evaluate(issue_number=1, issue_body=issue_body, source_milestone="M1")
        assert report2.runnable
        assert len(report2.satisfied) == 1

    def test_dependency_transitions_from_unknown_to_satisfied(self, checker, events):
        """Transient error resolving to satisfied."""
        evaluator = DependencyEvaluator(issue_checker=checker, events=events)
        issue_body = "Depends-on: #100"

        # Initially error
        checker.error_on.add(100)
        report1 = evaluator.evaluate(issue_number=1, issue_body=issue_body, source_milestone="M1")
        assert not report1.runnable
        assert len(report1.unknown) == 1

        # Error resolved
        checker.error_on.remove(100)
        checker.issues[100] = "closed"
        report2 = evaluator.evaluate(issue_number=1, issue_body=issue_body, source_milestone="M1")
        assert report2.runnable

    def test_multiple_dependency_chain_unblocks(self, checker, events):
        """Chain of dependencies: A <- B <- C, when A closes, B becomes runnable."""
        evaluator = DependencyEvaluator(issue_checker=checker, events=events)

        # Issue B depends on A
        checker.issues[1] = "open"  # A is open
        report_b = evaluator.evaluate(
            issue_number=2,
            issue_body="Depends-on: #1",  # B depends on A
            source_milestone="M1",
        )
        assert not report_b.runnable

        # A gets closed
        checker.issues[1] = "closed"
        report_b2 = evaluator.evaluate(
            issue_number=2,
            issue_body="Depends-on: #1",
            source_milestone="M1",
        )
        assert report_b2.runnable


# =============================================================================
# Event Emission Tests
# =============================================================================


class TestEventEmission:
    """Test that events are properly emitted."""

    def test_emits_dependencies_evaluated_event(self, evaluator, checker, events):
        """Evaluator emits dependencies.evaluated event with correct data."""
        checker.issues[10] = "closed"
        checker.issues[20] = "open"

        evaluator.evaluate(
            issue_number=1,
            issue_body="""
            Depends-on: #10
            Depends-on: #20
            """,
            source_milestone="M1",
        )

        assert len(events.events) == 1
        event = events.events[0]
        assert event.name == "dependencies.evaluated"
        assert event.data["issue_number"] == 1
        assert event.data["runnable"] is False
        assert event.data["satisfied_count"] == 1
        assert event.data["unsatisfied_count"] == 1
        assert event.data["missing_count"] == 0
        assert event.data["unknown_count"] == 0

    def test_event_includes_summary(self, evaluator, checker, events):
        """Event includes human-readable summary."""
        checker.issues[10] = "open"

        evaluator.evaluate(
            issue_number=1,
            issue_body="Depends-on: #10",
            source_milestone="M1",
        )

        event = events.events[0]
        assert "summary" in event.data
        assert "#10" in event.data["summary"]


# =============================================================================
# Dependency Parsing Edge Cases
# =============================================================================


class TestDependencyParsingEdgeCases:
    """Test edge cases in dependency parsing."""

    def test_external_id_case_insensitive(self):
        """External IDs are normalized to uppercase."""
        refs = parse_dependency_refs("Depends-on: m1-001")
        assert len(refs) == 1
        assert refs[0].external_id == "M1-001"

    def test_depends_on_case_insensitive(self):
        """Depends-on keyword is case insensitive."""
        refs = parse_dependency_refs("DEPENDS-ON: #123")
        assert len(refs) == 1
        assert refs[0].issue_number == 123

    def test_multiple_mixed_references(self):
        """Mix of issue refs and external IDs in same body."""
        refs = parse_dependency_refs("""
            Depends-on: #10
            Depends-on: M1-005
            Depends-on: other/repo#20
            Depends-on: M2-010
        """)
        assert len(refs) == 4
        assert refs[0].issue_number == 10
        assert refs[1].external_id == "M1-005"
        assert refs[2].issue_number == 20
        assert refs[2].repository == "other/repo"
        assert refs[3].external_id == "M2-010"

    def test_no_dependencies_returns_empty(self):
        """Body with no dependencies returns empty list."""
        refs = parse_dependency_refs("This issue has no dependencies")
        assert refs == []


# =============================================================================
# Dependency Display Reference
# =============================================================================


class TestDependencyDisplayRef:
    """Test the display_ref property of Dependency."""

    def test_display_ref_with_issue_number(self):
        """Display ref for issue number dependency."""
        dep = Dependency(issue_number=123, state=DependencyState.SATISFIED)
        assert dep.display_ref == "#123"

    def test_display_ref_with_external_id(self):
        """Display ref shows both logical key and #number when both are known."""
        dep = Dependency(
            issue_number=123,
            external_id="M1-001",
            state=DependencyState.SATISFIED,
        )
        assert dep.display_ref == "M1-001 · #123"

    def test_display_ref_with_external_id_only(self):
        """Display ref falls back to external_id alone when issue number missing."""
        dep = Dependency(
            issue_number=None,
            external_id="M1-001",
            state=DependencyState.UNSATISFIED,
        )
        assert dep.display_ref == "M1-001"

    def test_display_ref_with_cross_repo(self):
        """Display ref for cross-repo dependency."""
        dep = Dependency(
            issue_number=456,
            repository="other/repo",
            state=DependencyState.UNSATISFIED,
        )
        assert dep.display_ref == "other/repo#456"

    def test_display_ref_with_cross_repo_and_external_id(self):
        """Display ref combines logical key with cross-repo number when both known."""
        dep = Dependency(
            issue_number=456,
            repository="other/repo",
            external_id="M2-007",
            state=DependencyState.UNSATISFIED,
        )
        assert dep.display_ref == "M2-007 · other/repo#456"

    def test_display_ref_unknown(self):
        """Display ref when nothing is set."""
        dep = Dependency(issue_number=None, state=DependencyState.UNKNOWN)
        assert dep.display_ref == "(unknown)"


# =============================================================================
# Report Summary Tests
# =============================================================================


class TestDependencyReportSummary:
    """Test DependencyReport summary generation."""

    def test_summary_no_dependencies(self):
        """Summary for no dependencies."""
        report = DependencyReport(issue_number=1)
        assert report.summary() == "No dependencies"

    def test_summary_all_satisfied(self):
        """Summary when all dependencies are satisfied."""
        report = DependencyReport(
            issue_number=1,
            satisfied=(
                Dependency(issue_number=10, state=DependencyState.SATISFIED),
                Dependency(issue_number=20, state=DependencyState.SATISFIED),
            ),
        )
        assert "2 dependencies satisfied" in report.summary()

    def test_summary_blocked_with_multiple_issues(self):
        """Summary shows all blocking dependencies."""
        report = DependencyReport(
            issue_number=1,
            unsatisfied=(
                Dependency(issue_number=10, state=DependencyState.UNSATISFIED),
                Dependency(issue_number=20, state=DependencyState.UNSATISFIED),
            ),
        )
        summary = report.summary()
        assert "Blocked" in summary
        assert "#10" in summary
        assert "#20" in summary

    def test_summary_includes_all_problem_types(self):
        """Summary includes unsatisfied, missing, and unknown."""
        report = DependencyReport(
            issue_number=1,
            unsatisfied=(
                Dependency(issue_number=10, state=DependencyState.UNSATISFIED),
            ),
            missing=(
                Dependency(issue_number=20, state=DependencyState.MISSING),
            ),
            unknown=(
                Dependency(issue_number=30, state=DependencyState.UNKNOWN),
            ),
        )
        summary = report.summary()
        assert "waiting on" in summary
        assert "missing" in summary
        assert "unknown" in summary


# =============================================================================
# Cross-Milestone Validation Tests
# =============================================================================


class TestCrossMilestoneValidation:
    """Test milestone-scoped dependency validation.

    Dependencies are valid if:
    - Source issue HAS a milestone, AND
    - Dependency is in same milestone as source, OR
    - Dependency is in the foundation milestone (default "M0")
    """

    @pytest.fixture
    def checker_with_milestones(self):
        """Checker with milestone data."""
        checker = MockIssueChecker()
        # Issue states
        checker.issues[10] = "closed"
        checker.issues[20] = "closed"
        checker.issues[30] = "closed"
        checker.issues[40] = "closed"
        checker.issues[50] = "open"
        # Milestones
        checker.milestones[10] = "M0"  # Foundation
        checker.milestones[20] = "M1"
        checker.milestones[30] = "M2"
        checker.milestones[40] = "M2"
        checker.milestones[50] = "M1"
        return checker

    @pytest.fixture
    def evaluator_with_foundation(self, checker_with_milestones, events):
        """Evaluator with default foundation milestone (M0)."""
        return DependencyEvaluator(
            issue_checker=checker_with_milestones,
            events=events,
            foundation_milestone="M0",
        )

    def test_same_milestone_dependency_allowed(self, evaluator_with_foundation, checker_with_milestones):
        """Dependency within same milestone is allowed."""
        # Issue in M2 depends on another M2 issue
        report = evaluator_with_foundation.evaluate(
            issue_number=100,
            issue_body="Depends-on: #40",  # M2 -> M2
            source_milestone="M2",
        )

        assert report.runnable
        assert len(report.satisfied) == 1
        assert len(report.cross_milestone) == 0

    def test_foundation_milestone_dependency_allowed(self, evaluator_with_foundation, checker_with_milestones):
        """Dependency on foundation milestone is allowed from any milestone."""
        # Issue in M2 depends on M0 (foundation)
        report = evaluator_with_foundation.evaluate(
            issue_number=100,
            issue_body="Depends-on: #10",  # M2 -> M0
            source_milestone="M2",
        )

        assert report.runnable
        assert len(report.satisfied) == 1
        assert len(report.cross_milestone) == 0

    def test_cross_milestone_dependency_blocked(self, evaluator_with_foundation, checker_with_milestones):
        """Dependency across milestones (not foundation) is blocked."""
        # Issue in M2 depends on M1 issue (cross-milestone violation)
        report = evaluator_with_foundation.evaluate(
            issue_number=100,
            issue_body="Depends-on: #20",  # M2 -> M1
            source_milestone="M2",
        )

        assert not report.runnable
        assert len(report.cross_milestone) == 1
        dep = report.cross_milestone[0]
        assert dep.state == DependencyState.CROSS_MILESTONE
        assert dep.issue_number == 20
        assert dep.milestone == "M1"
        assert "cross-milestone" in report.summary().lower()

    def test_no_source_milestone_with_dependencies_blocked(self, evaluator_with_foundation, checker_with_milestones):
        """Issue without milestone that declares dependencies is blocked."""
        # Issue with no milestone depends on a closed issue
        report = evaluator_with_foundation.evaluate(
            issue_number=100,
            issue_body="Depends-on: #10",
            source_milestone=None,  # No milestone
        )

        assert not report.runnable
        assert len(report.cross_milestone) == 1
        dep = report.cross_milestone[0]
        assert dep.state == DependencyState.CROSS_MILESTONE
        assert "no milestone" in dep.error.lower()

    def test_custom_foundation_milestone(self, checker_with_milestones, events):
        """Custom foundation milestone works correctly."""
        # Use "M1" as foundation instead of "M0"
        checker_with_milestones.milestones[60] = "M1"  # Our custom foundation
        checker_with_milestones.issues[60] = "closed"

        evaluator = DependencyEvaluator(
            issue_checker=checker_with_milestones,
            events=events,
            foundation_milestone="M1",  # Custom foundation
        )

        # Issue in M2 depends on M1 (now the foundation)
        report = evaluator.evaluate(
            issue_number=100,
            issue_body="Depends-on: #60",  # M2 -> M1 (foundation)
            source_milestone="M2",
        )

        assert report.runnable
        assert len(report.satisfied) == 1
        assert len(report.cross_milestone) == 0

    def test_multiple_dependencies_mixed_milestone_status(self, evaluator_with_foundation, checker_with_milestones):
        """Multiple dependencies with mixed milestone validity."""
        # Issue in M2 depends on:
        # - #10 (M0 - foundation, OK)
        # - #40 (M2 - same, OK)
        # - #20 (M1 - cross-milestone, BLOCKED)
        report = evaluator_with_foundation.evaluate(
            issue_number=100,
            issue_body="""
            Depends-on: #10
            Depends-on: #40
            Depends-on: #20
            """,
            source_milestone="M2",
        )

        assert not report.runnable
        assert len(report.satisfied) == 2  # #10 and #40
        assert len(report.cross_milestone) == 1  # #20

    def test_cross_milestone_blocking_dependencies(self, evaluator_with_foundation, checker_with_milestones):
        """Cross-milestone deps are included in blocking_dependencies."""
        report = evaluator_with_foundation.evaluate(
            issue_number=100,
            issue_body="Depends-on: #20",  # M2 -> M1
            source_milestone="M2",
        )

        blocking = report.blocking_dependencies
        assert len(blocking) == 1
        assert blocking[0].state == DependencyState.CROSS_MILESTONE

    def test_cross_milestone_has_warnings(self, evaluator_with_foundation, checker_with_milestones):
        """Cross-milestone deps trigger has_warnings flag."""
        report = evaluator_with_foundation.evaluate(
            issue_number=100,
            issue_body="Depends-on: #20",
            source_milestone="M2",
        )

        assert report.has_warnings

    def test_no_milestone_validation_without_source_milestone_and_no_deps(self, evaluator_with_foundation):
        """Issue without milestone and no dependencies is fine."""
        report = evaluator_with_foundation.evaluate(
            issue_number=100,
            issue_body="No dependencies here",
            source_milestone=None,
        )

        assert report.runnable
        assert len(report.all_dependencies) == 0

    def test_dependency_without_milestone_from_milestoned_issue(self, evaluator_with_foundation, checker_with_milestones):
        """Dependency that has no milestone is treated as cross-milestone violation."""
        # Add an issue without a milestone
        checker_with_milestones.issues[99] = "closed"
        checker_with_milestones.milestones[99] = None  # No milestone

        report = evaluator_with_foundation.evaluate(
            issue_number=100,
            issue_body="Depends-on: #99",
            source_milestone="M2",
        )

        assert not report.runnable
        assert len(report.cross_milestone) == 1
        dep = report.cross_milestone[0]
        assert dep.milestone is None
        assert "has no milestone" in dep.error.lower()

    def test_cross_repo_milestone_validation(self, checker_with_milestones, events):
        """Cross-repo dependencies also validate milestones."""
        # Add cross-repo issue with milestone
        checker_with_milestones.cross_repo_issues[(200, "other/repo")] = "closed"
        checker_with_milestones.cross_repo_milestones[(200, "other/repo")] = "M1"

        evaluator = DependencyEvaluator(
            issue_checker=checker_with_milestones,
            events=events,
            foundation_milestone="M0",
        )

        # Issue in M2 depends on other/repo#200 (in M1) - cross-milestone
        report = evaluator.evaluate(
            issue_number=100,
            issue_body="Depends-on: other/repo#200",
            source_milestone="M2",
        )

        assert not report.runnable
        assert len(report.cross_milestone) == 1
        dep = report.cross_milestone[0]
        assert dep.repository == "other/repo"
        assert dep.milestone == "M1"

    def test_cross_repo_foundation_milestone_allowed(self, checker_with_milestones, events):
        """Cross-repo dependency in foundation milestone is allowed."""
        checker_with_milestones.cross_repo_issues[(200, "other/repo")] = "closed"
        checker_with_milestones.cross_repo_milestones[(200, "other/repo")] = "M0"

        evaluator = DependencyEvaluator(
            issue_checker=checker_with_milestones,
            events=events,
            foundation_milestone="M0",
        )

        report = evaluator.evaluate(
            issue_number=100,
            issue_body="Depends-on: other/repo#200",
            source_milestone="M2",
        )

        assert report.runnable
        assert len(report.satisfied) == 1

    def test_summary_shows_cross_milestone_details(self, evaluator_with_foundation, checker_with_milestones):
        """Summary includes cross-milestone dependency details."""
        report = evaluator_with_foundation.evaluate(
            issue_number=100,
            issue_body="Depends-on: #20",
            source_milestone="M2",
        )

        summary = report.summary()
        assert "cross-milestone" in summary.lower()
        assert "#20" in summary

    def test_all_dependencies_includes_cross_milestone(self, evaluator_with_foundation, checker_with_milestones):
        """all_dependencies property includes cross-milestone deps."""
        report = evaluator_with_foundation.evaluate(
            issue_number=100,
            issue_body="""
            Depends-on: #10
            Depends-on: #20
            """,
            source_milestone="M2",
        )

        all_deps = report.all_dependencies
        assert len(all_deps) == 2
        states = {d.state for d in all_deps}
        assert DependencyState.SATISFIED in states
        assert DependencyState.CROSS_MILESTONE in states
