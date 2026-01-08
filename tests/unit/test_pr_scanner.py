"""Behavior-centric unit tests for PRScanner.

Tests the PR scanning and detection logic using mock adapters at port boundaries.
Follows the testing patterns in tests/unit/CLAUDE.md:
- Mock at port boundaries (MockGitHubAdapter, MockEventSink)
- No internal patches
- Focus on behaviors and edge cases
"""

import pytest
from unittest.mock import MagicMock

from issue_orchestrator.control.pr_scanner import PRScanner, ScanResult
from issue_orchestrator.infra.config import Config
from issue_orchestrator.domain.models import PendingReview, PendingRework
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.ports.pull_request_tracker import PRInfo
from issue_orchestrator.events import EventName
from tests.conftest import MockEventSink, MockGitHubAdapter


# =============================================================================
# Fixtures
# =============================================================================


@pytest.fixture
def mock_config():
    """Create a mock config for testing."""
    config = Config()
    config.repo = "owner/repo"
    config.max_concurrent_sessions = 3
    config.code_review_agent = "agent:reviewer"
    config.code_review_label = "needs-code-review"
    config.code_reviewed_label = "code-reviewed"
    config.max_rework_cycles = 2
    return config


@pytest.fixture
def mock_repository():
    """Create a mock repository that implements RepositoryScanner protocol."""
    return MockGitHubAdapter()


@pytest.fixture
def mock_events():
    """Create a mock event sink."""
    return MockEventSink()


@pytest.fixture
def scanner(mock_config, mock_repository, mock_events):
    """Create a PRScanner instance for testing."""
    return PRScanner(
        config=mock_config,
        repository=mock_repository,
        events=mock_events,
    )


def make_pr_info(
    number: int,
    branch: str = "feature-branch",
    body: str = "",
    labels: list[str] | None = None,
    state: str = "open",
) -> PRInfo:
    """Create a PRInfo for testing."""
    return PRInfo(
        number=number,
        title=f"PR #{number}",
        url=f"https://github.com/owner/repo/pull/{number}",
        branch=branch,
        body=body,
        state=state,
        labels=labels or [],
    )


def make_pending_review(
    issue_number: int,
    pr_number: int,
    branch: str = "feature-branch",
) -> PendingReview:
    """Create a PendingReview for testing."""
    return PendingReview(
        issue_key=FakeIssueKey(name=str(issue_number)),
        pr_number=pr_number,
        pr_url=f"https://github.com/owner/repo/pull/{pr_number}",
        branch_name=branch,
        _issue_number=issue_number,
    )


def make_pending_rework(
    issue_number: int,
    agent_type: str = "agent:developer",
    rework_cycle: int = 1,
) -> PendingRework:
    """Create a PendingRework for testing."""
    return PendingRework(
        issue_key=FakeIssueKey(name=str(issue_number)),
        agent_type=agent_type,
        rework_cycle=rework_cycle,
    )


# =============================================================================
# Test: scan_for_reviews
# =============================================================================


class TestScanForReviewsBasic:
    """Tests for basic review scanning behavior."""

    def test_returns_empty_when_no_code_review_agent(self, mock_repository, mock_events):
        """Returns empty list when code review is not configured."""
        config = Config()
        config.code_review_agent = None  # Not configured
        scanner = PRScanner(config=config, repository=mock_repository, events=mock_events)

        # Add PRs that would otherwise be found
        mock_repository.prs["feature-1"] = [
            make_pr_info(100, labels=["needs-code-review"])
        ]

        result = scanner.scan_for_reviews(
            already_queued=[],
            active_sessions=[],
        )

        assert result == []

    def test_returns_empty_when_no_code_review_label(self, mock_repository, mock_events):
        """Returns empty list when code review label is not configured."""
        config = Config()
        config.code_review_agent = "agent:reviewer"
        config.code_review_label = None  # Not configured
        scanner = PRScanner(config=config, repository=mock_repository, events=mock_events)

        result = scanner.scan_for_reviews(
            already_queued=[],
            active_sessions=[],
        )

        assert result == []

    def test_finds_prs_with_review_label(self, scanner, mock_repository):
        """Finds PRs that have the code review label."""
        # Add a PR with the needs-code-review label
        pr = make_pr_info(100, branch="42-feature", body="Closes #42", labels=["needs-code-review"])
        mock_repository.prs["42-feature"] = [pr]

        result = scanner.scan_for_reviews(
            already_queued=[],
            active_sessions=[],
        )

        assert len(result) == 1
        assert result[0].pr_number == 100
        assert result[0].issue_number == 42
        assert result[0].branch_name == "42-feature"

    def test_extracts_issue_number_from_closes_pattern(self, scanner, mock_repository):
        """Extracts issue number from 'Closes #N' pattern in PR body."""
        pr = make_pr_info(
            100,
            branch="feature",
            body="This PR fixes a bug.\n\nCloses #123",
            labels=["needs-code-review"],
        )
        mock_repository.prs["feature"] = [pr]

        result = scanner.scan_for_reviews(
            already_queued=[],
            active_sessions=[],
        )

        assert len(result) == 1
        assert result[0].issue_number == 123

    def test_extracts_issue_number_case_insensitive(self, scanner, mock_repository):
        """Extracts issue number from 'closes' (lowercase) pattern."""
        pr = make_pr_info(
            100,
            branch="feature",
            body="closes #456",
            labels=["needs-code-review"],
        )
        mock_repository.prs["feature"] = [pr]

        result = scanner.scan_for_reviews(
            already_queued=[],
            active_sessions=[],
        )

        assert len(result) == 1
        assert result[0].issue_number == 456

    def test_fallback_to_pr_number_when_no_closes_pattern(self, scanner, mock_repository):
        """Falls back to PR number as issue number when no Closes pattern."""
        pr = make_pr_info(
            100,
            branch="feature",
            body="Just a PR with no issue reference",
            labels=["needs-code-review"],
        )
        mock_repository.prs["feature"] = [pr]

        result = scanner.scan_for_reviews(
            already_queued=[],
            active_sessions=[],
        )

        assert len(result) == 1
        assert result[0].issue_number == 100  # Falls back to PR number


class TestScanForReviewsFiltering:
    """Tests for filtering logic in review scanning."""

    def test_skips_already_queued_prs(self, scanner, mock_repository):
        """Skips PRs that are already in the review queue."""
        pr = make_pr_info(100, branch="feature", body="Closes #42", labels=["needs-code-review"])
        mock_repository.prs["feature"] = [pr]

        # Already queued
        already_queued = [make_pending_review(42, 100)]

        result = scanner.scan_for_reviews(
            already_queued=already_queued,
            active_sessions=[],
        )

        assert result == []

    def test_skips_actively_reviewed_prs(self, scanner, mock_repository):
        """Skips PRs that are currently being reviewed (active session)."""
        pr = make_pr_info(100, branch="feature", body="Closes #42", labels=["needs-code-review"])
        mock_repository.prs["feature"] = [pr]

        # Active review session
        active_sessions = ["review-100"]  # Session name includes PR number

        result = scanner.scan_for_reviews(
            already_queued=[],
            active_sessions=active_sessions,
        )

        assert result == []

    def test_includes_prs_not_matching_active_sessions(self, scanner, mock_repository):
        """Includes PRs when active sessions don't match."""
        pr = make_pr_info(100, branch="feature", body="Closes #42", labels=["needs-code-review"])
        mock_repository.prs["feature"] = [pr]

        # Different active sessions
        active_sessions = ["review-999", "issue-42"]  # Different PR and non-review session

        result = scanner.scan_for_reviews(
            already_queued=[],
            active_sessions=active_sessions,
        )

        assert len(result) == 1
        assert result[0].pr_number == 100

    def test_finds_multiple_prs(self, scanner, mock_repository):
        """Finds multiple PRs needing review."""
        pr1 = make_pr_info(100, branch="feature-1", body="Closes #1", labels=["needs-code-review"])
        pr2 = make_pr_info(101, branch="feature-2", body="Closes #2", labels=["needs-code-review"])
        pr3 = make_pr_info(102, branch="feature-3", body="Closes #3", labels=["needs-code-review"])
        mock_repository.prs["feature-1"] = [pr1]
        mock_repository.prs["feature-2"] = [pr2]
        mock_repository.prs["feature-3"] = [pr3]

        result = scanner.scan_for_reviews(
            already_queued=[],
            active_sessions=[],
        )

        assert len(result) == 3
        pr_numbers = {r.pr_number for r in result}
        assert pr_numbers == {100, 101, 102}

    def test_partial_filtering_of_multiple_prs(self, scanner, mock_repository):
        """Filters some PRs while returning others."""
        pr1 = make_pr_info(100, branch="feature-1", body="Closes #1", labels=["needs-code-review"])
        pr2 = make_pr_info(101, branch="feature-2", body="Closes #2", labels=["needs-code-review"])
        pr3 = make_pr_info(102, branch="feature-3", body="Closes #3", labels=["needs-code-review"])
        mock_repository.prs["feature-1"] = [pr1]
        mock_repository.prs["feature-2"] = [pr2]
        mock_repository.prs["feature-3"] = [pr3]

        # PR 100 is already queued, PR 101 is being reviewed
        already_queued = [make_pending_review(1, 100)]
        active_sessions = ["review-101"]

        result = scanner.scan_for_reviews(
            already_queued=already_queued,
            active_sessions=active_sessions,
        )

        # Only PR 102 should be returned
        assert len(result) == 1
        assert result[0].pr_number == 102


class TestScanForReviewsEvents:
    """Tests for event emission in review scanning."""

    def test_emits_event_when_reviews_found(self, scanner, mock_repository, mock_events):
        """Emits SCANNER_REVIEWS_FOUND event when reviews are discovered."""
        pr = make_pr_info(100, branch="feature", body="Closes #42", labels=["needs-code-review"])
        mock_repository.prs["feature"] = [pr]

        scanner.scan_for_reviews(
            already_queued=[],
            active_sessions=[],
        )

        events = mock_events.get_events_by_name(EventName.SCANNER_REVIEWS_FOUND)
        assert len(events) == 1
        assert events[0].data["count"] == 1

    def test_no_event_when_no_reviews_found(self, scanner, mock_events):
        """Does not emit event when no reviews are found."""
        # No PRs configured
        result = scanner.scan_for_reviews(
            already_queued=[],
            active_sessions=[],
        )

        assert result == []
        events = mock_events.get_events_by_name(EventName.SCANNER_REVIEWS_FOUND)
        assert len(events) == 0

    def test_no_event_when_all_prs_filtered(self, scanner, mock_repository, mock_events):
        """Does not emit event when all PRs are filtered out."""
        pr = make_pr_info(100, branch="feature", body="Closes #42", labels=["needs-code-review"])
        mock_repository.prs["feature"] = [pr]

        # All are already queued
        already_queued = [make_pending_review(42, 100)]

        result = scanner.scan_for_reviews(
            already_queued=already_queued,
            active_sessions=[],
        )

        assert result == []
        events = mock_events.get_events_by_name(EventName.SCANNER_REVIEWS_FOUND)
        assert len(events) == 0


# =============================================================================
# Test: scan_for_reworks
# =============================================================================


class TestScanForReworksBasic:
    """Tests for basic rework scanning behavior."""

    def test_returns_empty_when_no_code_review_agent(self, mock_repository, mock_events):
        """Returns empty lists when code review is not configured."""
        config = Config()
        config.code_review_agent = None  # Not configured
        scanner = PRScanner(config=config, repository=mock_repository, events=mock_events)

        result, escalations = scanner.scan_for_reworks(
            already_queued=[],
            active_sessions=[],
        )

        assert result == []
        assert escalations == []

    def test_finds_prs_with_rework_label(self, scanner, mock_repository):
        """Finds PRs that have the needs-rework label."""
        pr = make_pr_info(
            100,
            branch="42-feature",
            body="Closes #42",
            labels=["needs-rework", "agent:developer"],
        )
        mock_repository.prs["42-feature"] = [pr]

        result, escalations = scanner.scan_for_reworks(
            already_queued=[],
            active_sessions=[],
        )

        assert len(result) == 1
        assert result[0].issue_key.stable_id() == "42"
        assert result[0].agent_type == "agent:developer"
        assert result[0].rework_cycle == 1  # First rework cycle
        assert escalations == []

    def test_extracts_rework_cycle_from_labels(self, scanner, mock_repository, mock_config):
        """Extracts rework cycle count from labels."""
        # Set high max to ensure we don't hit escalation
        mock_config.max_rework_cycles = 5

        pr = make_pr_info(
            100,
            branch="42-feature",
            body="Closes #42",
            labels=["needs-rework", "agent:developer", "rework-cycle-2"],
        )
        mock_repository.prs["42-feature"] = [pr]

        result, escalations = scanner.scan_for_reworks(
            already_queued=[],
            active_sessions=[],
        )

        assert len(result) == 1
        # rework-cycle-2 means this was the 2nd attempt, so next is cycle 3
        assert result[0].rework_cycle == 3

    def test_skips_pr_without_agent_label(self, scanner, mock_repository):
        """Skips PRs that don't have an agent label."""
        pr = make_pr_info(
            100,
            branch="42-feature",
            body="Closes #42",
            labels=["needs-rework"],  # No agent: label
        )
        mock_repository.prs["42-feature"] = [pr]

        result, escalations = scanner.scan_for_reworks(
            already_queued=[],
            active_sessions=[],
        )

        assert result == []
        assert escalations == []


class TestScanForReworksFiltering:
    """Tests for filtering logic in rework scanning."""

    def test_skips_already_queued_reworks(self, scanner, mock_repository):
        """Skips PRs that are already queued for rework."""
        pr = make_pr_info(
            100,
            branch="42-feature",
            body="Closes #42",
            labels=["needs-rework", "agent:developer"],
        )
        mock_repository.prs["42-feature"] = [pr]

        # Already queued
        already_queued = [make_pending_rework(42)]

        result, escalations = scanner.scan_for_reworks(
            already_queued=already_queued,
            active_sessions=[],
        )

        assert result == []
        assert escalations == []

    def test_skips_actively_worked_issues(self, scanner, mock_repository):
        """Skips PRs whose issues are currently being worked on."""
        pr = make_pr_info(
            100,
            branch="42-feature",
            body="Closes #42",
            labels=["needs-rework", "agent:developer"],
        )
        mock_repository.prs["42-feature"] = [pr]

        # Issue 42 is being actively worked on
        active_sessions = [42]

        result, escalations = scanner.scan_for_reworks(
            already_queued=[],
            active_sessions=active_sessions,
        )

        assert result == []
        assert escalations == []

    def test_finds_multiple_reworks(self, scanner, mock_repository):
        """Finds multiple PRs needing rework."""
        pr1 = make_pr_info(
            100,
            branch="1-feature",
            body="Closes #1",
            labels=["needs-rework", "agent:developer"],
        )
        pr2 = make_pr_info(
            101,
            branch="2-feature",
            body="Closes #2",
            labels=["needs-rework", "agent:web"],
        )
        mock_repository.prs["1-feature"] = [pr1]
        mock_repository.prs["2-feature"] = [pr2]

        result, escalations = scanner.scan_for_reworks(
            already_queued=[],
            active_sessions=[],
        )

        assert len(result) == 2
        assert escalations == []


class TestScanForReworksEscalation:
    """Tests for escalation logic in rework scanning."""

    def test_escalates_when_max_rework_cycles_exceeded(self, scanner, mock_repository, mock_config):
        """Escalates to human when max rework cycles are exceeded."""
        mock_config.max_rework_cycles = 2

        # PR with rework-cycle-2 label means next would be cycle 3
        pr = make_pr_info(
            100,
            branch="42-feature",
            body="Closes #42",
            labels=["needs-rework", "agent:developer", "rework-cycle-2"],
        )
        mock_repository.prs["42-feature"] = [pr]

        result, escalations = scanner.scan_for_reworks(
            already_queued=[],
            active_sessions=[],
        )

        # Should escalate instead of queueing
        assert result == []
        assert len(escalations) == 1
        pr_number, issue_number, rework_cycle = escalations[0]
        assert pr_number == 100
        assert issue_number == 42
        assert rework_cycle == 3

    def test_no_escalation_at_max_cycle(self, scanner, mock_repository, mock_config):
        """Does not escalate when exactly at max rework cycles."""
        mock_config.max_rework_cycles = 2

        # rework-cycle-1 means next is cycle 2 (at max, not exceeding)
        pr = make_pr_info(
            100,
            branch="42-feature",
            body="Closes #42",
            labels=["needs-rework", "agent:developer", "rework-cycle-1"],
        )
        mock_repository.prs["42-feature"] = [pr]

        result, escalations = scanner.scan_for_reworks(
            already_queued=[],
            active_sessions=[],
        )

        # Should queue for rework, not escalate
        assert len(result) == 1
        assert result[0].rework_cycle == 2
        assert escalations == []

    def test_mixed_reworks_and_escalations(self, scanner, mock_repository, mock_config):
        """Handles mix of reworks and escalations."""
        mock_config.max_rework_cycles = 2

        # PR1: needs rework (first cycle)
        pr1 = make_pr_info(
            100,
            branch="1-feature",
            body="Closes #1",
            labels=["needs-rework", "agent:developer"],
        )
        # PR2: exceeded max cycles (should escalate)
        pr2 = make_pr_info(
            101,
            branch="2-feature",
            body="Closes #2",
            labels=["needs-rework", "agent:developer", "rework-cycle-3"],
        )
        # PR3: at max cycles (should still rework)
        pr3 = make_pr_info(
            102,
            branch="3-feature",
            body="Closes #3",
            labels=["needs-rework", "agent:developer", "rework-cycle-1"],
        )
        mock_repository.prs["1-feature"] = [pr1]
        mock_repository.prs["2-feature"] = [pr2]
        mock_repository.prs["3-feature"] = [pr3]

        result, escalations = scanner.scan_for_reworks(
            already_queued=[],
            active_sessions=[],
        )

        # PR1 and PR3 should be queued for rework
        assert len(result) == 2
        rework_issues = {int(r.issue_key.stable_id()) for r in result}
        assert rework_issues == {1, 3}

        # PR2 should be escalated
        assert len(escalations) == 1
        assert escalations[0][0] == 101  # PR number
        assert escalations[0][1] == 2    # Issue number


class TestScanForReworksEvents:
    """Tests for event emission in rework scanning."""

    def test_emits_event_when_reworks_found(self, scanner, mock_repository, mock_events):
        """Emits SCANNER_REWORKS_FOUND event when reworks are discovered."""
        pr = make_pr_info(
            100,
            branch="42-feature",
            body="Closes #42",
            labels=["needs-rework", "agent:developer"],
        )
        mock_repository.prs["42-feature"] = [pr]

        scanner.scan_for_reworks(
            already_queued=[],
            active_sessions=[],
        )

        events = mock_events.get_events_by_name(EventName.SCANNER_REWORKS_FOUND)
        assert len(events) == 1
        assert events[0].data["reworks"] == 1
        assert events[0].data["escalations"] == 0

    def test_emits_event_with_escalation_count(self, scanner, mock_repository, mock_events, mock_config):
        """Includes escalation count in event."""
        mock_config.max_rework_cycles = 1

        # Will be escalated
        pr = make_pr_info(
            100,
            branch="42-feature",
            body="Closes #42",
            labels=["needs-rework", "agent:developer", "rework-cycle-1"],
        )
        mock_repository.prs["42-feature"] = [pr]

        scanner.scan_for_reworks(
            already_queued=[],
            active_sessions=[],
        )

        events = mock_events.get_events_by_name(EventName.SCANNER_REWORKS_FOUND)
        assert len(events) == 1
        assert events[0].data["reworks"] == 0
        assert events[0].data["escalations"] == 1

    def test_no_event_when_no_reworks_found(self, scanner, mock_events):
        """Does not emit event when no reworks are found."""
        result, escalations = scanner.scan_for_reworks(
            already_queued=[],
            active_sessions=[],
        )

        assert result == []
        assert escalations == []
        events = mock_events.get_events_by_name(EventName.SCANNER_REWORKS_FOUND)
        assert len(events) == 0


# =============================================================================
# Test: Internal helper methods
# =============================================================================


class TestExtractIssueNumber:
    """Tests for _extract_issue_number helper method."""

    def test_extracts_from_closes_pattern(self, scanner):
        """Extracts issue number from 'Closes #N' pattern."""
        result = scanner._extract_issue_number("Fixes something\n\nCloses #123", fallback=999)
        assert result == 123

    def test_case_insensitive(self, scanner):
        """Handles case variations of 'closes'."""
        result1 = scanner._extract_issue_number("closes #100", fallback=999)
        result2 = scanner._extract_issue_number("CLOSES #200", fallback=999)
        result3 = scanner._extract_issue_number("Closes #300", fallback=999)

        assert result1 == 100
        assert result2 == 200
        assert result3 == 300

    def test_returns_fallback_when_no_match(self, scanner):
        """Returns fallback when no Closes pattern found."""
        result = scanner._extract_issue_number("No issue reference here", fallback=42)
        assert result == 42

    def test_empty_body(self, scanner):
        """Handles empty PR body."""
        result = scanner._extract_issue_number("", fallback=1)
        assert result == 1


class TestGetReworkCycleFromLabels:
    """Tests for _get_rework_cycle_from_labels helper method."""

    def test_returns_1_when_no_rework_label(self, scanner):
        """Returns 1 (first rework) when no rework-cycle-N label."""
        result = scanner._get_rework_cycle_from_labels(["needs-rework", "agent:developer"])
        assert result == 1

    def test_extracts_cycle_and_increments(self, scanner):
        """Extracts cycle number and returns next cycle."""
        result = scanner._get_rework_cycle_from_labels(["rework-cycle-1"])
        assert result == 2  # Next cycle

        result = scanner._get_rework_cycle_from_labels(["rework-cycle-5"])
        assert result == 6  # Next cycle

    def test_ignores_non_matching_labels(self, scanner):
        """Ignores labels that don't match rework-cycle-N pattern."""
        result = scanner._get_rework_cycle_from_labels([
            "rework-needed",
            "cycle-1",
            "rework-cycle-3",  # This one matches
            "other-label",
        ])
        assert result == 4  # 3 + 1

    def test_empty_labels(self, scanner):
        """Returns 1 for empty labels list."""
        result = scanner._get_rework_cycle_from_labels([])
        assert result == 1


class TestExtractAgentType:
    """Tests for _extract_agent_type helper method."""

    def test_extracts_agent_label(self, scanner):
        """Extracts agent type from labels."""
        result = scanner._extract_agent_type(["needs-rework", "agent:developer", "priority:high"])
        assert result == "agent:developer"

    def test_returns_first_agent_label(self, scanner):
        """Returns first agent label if multiple present."""
        result = scanner._extract_agent_type(["agent:web", "agent:mobile"])
        assert result == "agent:web"

    def test_returns_none_when_no_agent_label(self, scanner):
        """Returns None when no agent: label present."""
        result = scanner._extract_agent_type(["needs-rework", "priority:high"])
        assert result is None

    def test_empty_labels(self, scanner):
        """Returns None for empty labels list."""
        result = scanner._extract_agent_type([])
        assert result is None


# =============================================================================
# Test: Edge cases and error scenarios
# =============================================================================


class TestEdgeCases:
    """Tests for edge cases and unusual scenarios."""

    def test_pr_with_empty_body(self, scanner, mock_repository):
        """Handles PR with empty body gracefully."""
        pr = make_pr_info(100, branch="feature", body="", labels=["needs-code-review"])
        mock_repository.prs["feature"] = [pr]

        result = scanner.scan_for_reviews(
            already_queued=[],
            active_sessions=[],
        )

        assert len(result) == 1
        # Falls back to PR number
        assert result[0].issue_number == 100

    def test_pr_with_branch_name_fallback(self, scanner, mock_repository):
        """Uses branch name from PR even when finding reworks."""
        pr = make_pr_info(
            100,
            branch="",  # Empty branch
            body="Closes #42",
            labels=["needs-rework", "agent:developer"],
        )
        mock_repository.prs[""] = [pr]

        result, escalations = scanner.scan_for_reworks(
            already_queued=[],
            active_sessions=[],
        )

        assert len(result) == 1
        # Uses fallback branch name: "{issue_number}-rework"
        # Note: The actual branch_name is from pr.branch, but we accept empty

    def test_multiple_closes_patterns_uses_first(self, scanner):
        """Uses first Closes pattern when multiple present."""
        body = "Closes #100\n\nAlso Closes #200"
        result = scanner._extract_issue_number(body, fallback=999)
        assert result == 100

    def test_review_session_name_pattern(self, scanner, mock_repository):
        """Correctly matches review-N session names."""
        pr = make_pr_info(100, branch="feature", body="Closes #42", labels=["needs-code-review"])
        mock_repository.prs["feature"] = [pr]

        # Test various session name patterns
        # "review-100" should block PR 100
        result = scanner.scan_for_reviews(
            already_queued=[],
            active_sessions=["review-100"],
        )
        assert len(result) == 0

        # "review-99" should NOT block PR 100
        result = scanner.scan_for_reviews(
            already_queued=[],
            active_sessions=["review-99"],
        )
        assert len(result) == 1

        # "issue-100" is not a review session
        result = scanner.scan_for_reviews(
            already_queued=[],
            active_sessions=["issue-100"],
        )
        assert len(result) == 1

    def test_high_rework_cycle_numbers(self, scanner, mock_repository, mock_config):
        """Handles high rework cycle numbers correctly."""
        mock_config.max_rework_cycles = 100

        pr = make_pr_info(
            100,
            branch="42-feature",
            body="Closes #42",
            labels=["needs-rework", "agent:developer", "rework-cycle-99"],
        )
        mock_repository.prs["42-feature"] = [pr]

        result, escalations = scanner.scan_for_reworks(
            already_queued=[],
            active_sessions=[],
        )

        # rework-cycle-99 means next is 100, which is at max (not exceeding)
        assert len(result) == 1
        assert result[0].rework_cycle == 100
        assert escalations == []

    def test_zero_max_rework_cycles(self, scanner, mock_repository, mock_config):
        """When max_rework_cycles is 0, all reworks should escalate."""
        mock_config.max_rework_cycles = 0

        pr = make_pr_info(
            100,
            branch="42-feature",
            body="Closes #42",
            labels=["needs-rework", "agent:developer"],
        )
        mock_repository.prs["42-feature"] = [pr]

        result, escalations = scanner.scan_for_reworks(
            already_queued=[],
            active_sessions=[],
        )

        # First rework (cycle 1) exceeds max of 0
        assert result == []
        assert len(escalations) == 1


class TestScanResult:
    """Tests for ScanResult dataclass."""

    def test_scan_result_creation(self):
        """ScanResult can be created with all fields."""
        reviews = [make_pending_review(1, 100)]
        reworks = [make_pending_rework(2)]
        escalations = [(101, 3, 5)]

        result = ScanResult(
            reviews_to_queue=reviews,
            reworks_to_queue=reworks,
            escalations=escalations,
        )

        assert len(result.reviews_to_queue) == 1
        assert len(result.reworks_to_queue) == 1
        assert len(result.escalations) == 1

    def test_scan_result_empty(self):
        """ScanResult can be created with empty lists."""
        result = ScanResult(
            reviews_to_queue=[],
            reworks_to_queue=[],
            escalations=[],
        )

        assert result.reviews_to_queue == []
        assert result.reworks_to_queue == []
        assert result.escalations == []


# =============================================================================
# Integration-style tests (still unit tests, but testing components together)
# =============================================================================


class TestScannerIntegration:
    """Integration-style tests for PRScanner."""

    def test_full_review_workflow(self, scanner, mock_repository, mock_events):
        """Tests complete review discovery workflow."""
        # Setup: 3 PRs with review label
        prs = [
            make_pr_info(100, branch="1-feat", body="Closes #1", labels=["needs-code-review"]),
            make_pr_info(101, branch="2-feat", body="Closes #2", labels=["needs-code-review"]),
            make_pr_info(102, branch="3-feat", body="Closes #3", labels=["needs-code-review"]),
        ]
        mock_repository.prs["1-feat"] = [prs[0]]
        mock_repository.prs["2-feat"] = [prs[1]]
        mock_repository.prs["3-feat"] = [prs[2]]

        # Scenario: PR 100 already queued, PR 101 being reviewed
        already_queued = [make_pending_review(1, 100)]
        active_sessions = ["review-101"]

        result = scanner.scan_for_reviews(
            already_queued=already_queued,
            active_sessions=active_sessions,
        )

        # Only PR 102 should be discovered
        assert len(result) == 1
        assert result[0].pr_number == 102
        assert result[0].issue_number == 3

        # Event should be emitted
        events = mock_events.get_events_by_name(EventName.SCANNER_REVIEWS_FOUND)
        assert len(events) == 1
        assert events[0].data["count"] == 1

    def test_full_rework_workflow(self, scanner, mock_repository, mock_events, mock_config):
        """Tests complete rework discovery workflow with escalations."""
        mock_config.max_rework_cycles = 2

        # Setup: 3 PRs with rework label at different cycle stages
        prs = [
            # First rework attempt
            make_pr_info(100, branch="1-feat", body="Closes #1", labels=["needs-rework", "agent:dev"]),
            # Second rework attempt (at max)
            make_pr_info(101, branch="2-feat", body="Closes #2", labels=["needs-rework", "agent:dev", "rework-cycle-1"]),
            # Third attempt (exceeds max, should escalate)
            make_pr_info(102, branch="3-feat", body="Closes #3", labels=["needs-rework", "agent:dev", "rework-cycle-2"]),
        ]
        mock_repository.prs["1-feat"] = [prs[0]]
        mock_repository.prs["2-feat"] = [prs[1]]
        mock_repository.prs["3-feat"] = [prs[2]]

        result, escalations = scanner.scan_for_reworks(
            already_queued=[],
            active_sessions=[],
        )

        # PRs 100 and 101 should be queued for rework
        assert len(result) == 2
        rework_issues = {int(r.issue_key.stable_id()) for r in result}
        assert rework_issues == {1, 2}

        # PR 102 should be escalated
        assert len(escalations) == 1
        assert escalations[0] == (102, 3, 3)  # (pr_number, issue_number, rework_cycle)

        # Event should be emitted with both counts
        events = mock_events.get_events_by_name(EventName.SCANNER_REWORKS_FOUND)
        assert len(events) == 1
        assert events[0].data["reworks"] == 2
        assert events[0].data["escalations"] == 1
