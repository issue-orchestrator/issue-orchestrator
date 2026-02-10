"""Unit tests for the reconciliation module."""

import pytest

from issue_orchestrator.control.reconciliation import (
    ExternalSnapshot,
    ExpectedState,
    ReconciliationRequired,
    ReconciliationResult,
    check_reconciliation,
    require_reconciliation,
)


class TestExternalSnapshot:
    """Tests for ExternalSnapshot dataclass."""

    def test_for_issue_creates_snapshot(self):
        """Test creating snapshot for an issue."""
        snapshot = ExternalSnapshot.for_issue(42, {"bug", "priority:high"})

        assert snapshot.number == 42
        assert snapshot.labels == frozenset({"bug", "priority:high"})
        assert snapshot.pr_state is None

    def test_for_pr_creates_snapshot(self):
        """Test creating snapshot for a PR."""
        snapshot = ExternalSnapshot.for_pr(123, {"needs-review"}, "open")

        assert snapshot.number == 123
        assert snapshot.labels == frozenset({"needs-review"})
        assert snapshot.pr_state == "open"

    def test_labels_match_with_same_labels(self):
        """Test labels_match returns True for identical labels."""
        snap1 = ExternalSnapshot.for_issue(1, {"a", "b"})
        snap2 = ExternalSnapshot.for_issue(2, {"a", "b"})  # Different issue, same labels

        assert snap1.labels_match(snap2)

    def test_labels_match_with_different_labels(self):
        """Test labels_match returns False for different labels."""
        snap1 = ExternalSnapshot.for_issue(1, {"a", "b"})
        snap2 = ExternalSnapshot.for_issue(1, {"a", "c"})

        assert not snap1.labels_match(snap2)

    def test_contains_labels_subset(self):
        """Test contains_labels with subset."""
        snapshot = ExternalSnapshot.for_issue(1, {"a", "b", "c"})

        assert snapshot.contains_labels({"a", "b"})
        assert snapshot.contains_labels({"a"})
        assert snapshot.contains_labels(set())

    def test_contains_labels_missing(self):
        """Test contains_labels with missing label."""
        snapshot = ExternalSnapshot.for_issue(1, {"a", "b"})

        assert not snapshot.contains_labels({"a", "c"})

    def test_excludes_labels_no_intersection(self):
        """Test excludes_labels with no intersection."""
        snapshot = ExternalSnapshot.for_issue(1, {"a", "b"})

        assert snapshot.excludes_labels({"c", "d"})
        assert snapshot.excludes_labels(set())

    def test_excludes_labels_with_intersection(self):
        """Test excludes_labels with intersection."""
        snapshot = ExternalSnapshot.for_issue(1, {"a", "b"})

        assert not snapshot.excludes_labels({"b", "c"})

    def test_is_immutable(self):
        """Test that snapshot is frozen (immutable)."""
        snapshot = ExternalSnapshot.for_issue(1, {"a"})

        with pytest.raises(AttributeError):
            snapshot.number = 2  # type: ignore


class TestExpectedState:
    """Tests for ExpectedState dataclass."""

    def test_with_labels_creates_state(self):
        """Test creating expected state with labels."""
        expected = ExpectedState.with_labels(
            required={"in-progress"},
            forbidden={"blocked"},
        )

        assert expected.required_labels == frozenset({"in-progress"})
        assert expected.forbidden_labels == frozenset({"blocked"})

    def test_is_satisfied_by_all_requirements_met(self):
        """Test satisfaction when all requirements met."""
        expected = ExpectedState.with_labels(
            required={"in-progress"},
            forbidden={"blocked"},
        )
        snapshot = ExternalSnapshot.for_issue(1, {"in-progress", "bug"})

        satisfied, reason = expected.is_satisfied_by(snapshot)

        assert satisfied
        assert reason == ""

    def test_is_satisfied_by_missing_required(self):
        """Test failure when required label missing."""
        expected = ExpectedState.with_labels(
            required={"in-progress", "bug"},
        )
        snapshot = ExternalSnapshot.for_issue(1, {"in-progress"})

        satisfied, reason = expected.is_satisfied_by(snapshot)

        assert not satisfied
        assert "bug" in reason
        assert "Missing required" in reason

    def test_is_satisfied_by_has_forbidden(self):
        """Test failure when forbidden label present."""
        expected = ExpectedState.with_labels(
            forbidden={"blocked"},
        )
        snapshot = ExternalSnapshot.for_issue(1, {"in-progress", "blocked"})

        satisfied, reason = expected.is_satisfied_by(snapshot)

        assert not satisfied
        assert "blocked" in reason
        assert "forbidden" in reason.lower()

    def test_is_satisfied_by_pr_state_match(self):
        """Test PR state check when matching."""
        expected = ExpectedState(required_pr_state="open")
        snapshot = ExternalSnapshot.for_pr(1, set(), "open")

        satisfied, _reason = expected.is_satisfied_by(snapshot)

        assert satisfied

    def test_is_satisfied_by_pr_state_mismatch(self):
        """Test PR state check when not matching."""
        expected = ExpectedState(required_pr_state="open")
        snapshot = ExternalSnapshot.for_pr(1, set(), "closed")

        satisfied, reason = expected.is_satisfied_by(snapshot)

        assert not satisfied
        assert "PR state" in reason


class TestReconciliationRequired:
    """Tests for ReconciliationRequired exception."""

    def test_exception_has_context(self):
        """Test exception includes context in message."""
        expected = ExternalSnapshot.for_issue(42, {"in-progress"})
        actual = ExternalSnapshot.for_issue(42, {"blocked"})

        exc = ReconciliationRequired(
            entity_type="issue",
            entity_id=42,
            expected=expected,
            actual=actual,
            reason="Label changed by human",
        )

        assert exc.entity_type == "issue"
        assert exc.entity_id == 42
        assert "42" in str(exc)
        assert "Label changed by human" in str(exc)

    def test_exception_can_be_raised(self):
        """Test exception can be raised and caught."""
        with pytest.raises(ReconciliationRequired) as exc_info:
            raise ReconciliationRequired(
                entity_type="issue",
                entity_id=42,
                expected=ExternalSnapshot.for_issue(42, {"a"}),
                actual=ExternalSnapshot.for_issue(42, {"b"}),
            )

        assert exc_info.value.entity_id == 42


class TestCheckReconciliation:
    """Tests for check_reconciliation function."""

    def test_returns_success_when_satisfied(self):
        """Test returns success result when state matches."""
        expected = ExpectedState.with_labels(required={"in-progress"})
        actual = ExternalSnapshot.for_issue(1, {"in-progress", "bug"})

        result = check_reconciliation(expected, actual)

        assert result.passed
        assert result.reason == ""

    def test_returns_failure_when_not_satisfied(self):
        """Test returns failure result when state doesn't match."""
        expected = ExpectedState.with_labels(required={"in-progress"})
        actual = ExternalSnapshot.for_issue(1, {"blocked"})

        result = check_reconciliation(expected, actual)

        assert not result.passed
        assert "in-progress" in result.reason


class TestRequireReconciliation:
    """Tests for require_reconciliation function."""

    def test_does_not_raise_when_satisfied(self):
        """Test does not raise when state matches."""
        expected = ExpectedState.with_labels(required={"in-progress"})
        actual = ExternalSnapshot.for_issue(1, {"in-progress"})

        # Should not raise
        require_reconciliation(expected, actual)

    def test_raises_when_not_satisfied(self):
        """Test raises ReconciliationRequired when state doesn't match."""
        expected = ExpectedState.with_labels(required={"in-progress"})
        actual = ExternalSnapshot.for_issue(42, {"blocked"})

        with pytest.raises(ReconciliationRequired) as exc_info:
            require_reconciliation(expected, actual, entity_type="issue")

        assert exc_info.value.entity_id == 42
        assert exc_info.value.entity_type == "issue"


class TestReconciliationResult:
    """Tests for ReconciliationResult dataclass."""

    def test_success_creates_passed_result(self):
        """Test success factory creates passed result."""
        expected = ExpectedState.with_labels(required={"a"})
        actual = ExternalSnapshot.for_issue(1, {"a"})

        result = ReconciliationResult.success(expected, actual)

        assert result.passed
        assert result.reason == ""

    def test_failure_creates_failed_result(self):
        """Test failure factory creates failed result."""
        expected = ExpectedState.with_labels(required={"a"})
        actual = ExternalSnapshot.for_issue(1, {"b"})

        result = ReconciliationResult.failure(expected, actual, "Labels don't match")

        assert not result.passed
        assert result.reason == "Labels don't match"


class TestHelperFunctions:
    """Tests for helper functions get_pause_label and build_expected_for_mutation."""

    def test_get_pause_label_with_default_prefix(self):
        """Test get_pause_label returns correct label with default prefix."""
        from issue_orchestrator.control.reconciliation import get_pause_label

        label = get_pause_label()
        assert label == "io:needs-reconcile"

    def test_get_pause_label_with_custom_prefix(self):
        """Test get_pause_label returns correct label with custom prefix."""
        from issue_orchestrator.control.reconciliation import get_pause_label

        label = get_pause_label("myprefix")
        assert label == "myprefix:needs-reconcile"

    def test_build_expected_for_mutation_forbids_pause_label(self):
        """Test build_expected_for_mutation includes pause label in forbidden."""
        from issue_orchestrator.control.reconciliation import build_expected_for_mutation

        expected = build_expected_for_mutation()

        # Should forbid the pause label by default
        assert "io:needs-reconcile" in expected.forbidden_labels

    def test_build_expected_for_mutation_with_required(self):
        """Test build_expected_for_mutation includes required labels."""
        from issue_orchestrator.control.reconciliation import build_expected_for_mutation

        expected = build_expected_for_mutation(required={"in-progress"})

        assert "in-progress" in expected.required_labels
        assert "io:needs-reconcile" in expected.forbidden_labels

    def test_build_expected_for_mutation_with_forbidden(self):
        """Test build_expected_for_mutation merges forbidden labels."""
        from issue_orchestrator.control.reconciliation import build_expected_for_mutation

        expected = build_expected_for_mutation(forbidden={"blocked"})

        # Should have both the custom forbidden and the pause label
        assert "blocked" in expected.forbidden_labels
        assert "io:needs-reconcile" in expected.forbidden_labels

    def test_build_expected_for_mutation_with_custom_prefix(self):
        """Test build_expected_for_mutation uses custom prefix."""
        from issue_orchestrator.control.reconciliation import build_expected_for_mutation

        expected = build_expected_for_mutation(prefix="custom")

        assert "custom:needs-reconcile" in expected.forbidden_labels
        assert "io:needs-reconcile" not in expected.forbidden_labels


class TestFailureTypeClassification:
    """Tests for SYSTEMIC vs ISSUE_LOCAL failure classification."""

    def test_systemic_failure_type_on_timeout(self):
        """Test that verification timeout creates SYSTEMIC failure type."""
        from issue_orchestrator.adapters.github.http_client import GitHubHttpError
        from issue_orchestrator.ports.verification import FailureType

        error = GitHubHttpError(
            "Timed out verifying write",
            failure_type=FailureType.SYSTEMIC,
        )

        assert error.is_systemic()
        assert not error.is_issue_local()
        assert error.failure_type == FailureType.SYSTEMIC

    def test_issue_local_failure_type_on_predicate_false(self):
        """Test that predicate false creates ISSUE_LOCAL failure type with issue number."""
        from issue_orchestrator.adapters.github.http_client import GitHubHttpError
        from issue_orchestrator.ports.verification import FailureType

        error = GitHubHttpError(
            "Failed to verify write",
            failure_type=FailureType.ISSUE_LOCAL,
            issue_number=42,
        )

        assert error.is_issue_local()
        assert not error.is_systemic()
        assert error.failure_type == FailureType.ISSUE_LOCAL
        assert error.issue_number == 42

    def test_failure_type_none_by_default(self):
        """Test that failure_type is None by default (unclassified)."""
        from issue_orchestrator.adapters.github.http_client import GitHubHttpError

        error = GitHubHttpError("Some error")

        assert not error.is_systemic()
        assert not error.is_issue_local()
        assert error.failure_type is None

    def test_systemic_failure_should_trigger_pause(self):
        """Test that systemic failures are classified for orchestrator pause.

        Per Phase 4 spec: SYSTEMIC -> orchestrator.pause + probe + resume
        """
        from issue_orchestrator.adapters.github.http_client import GitHubHttpError
        from issue_orchestrator.ports.verification import FailureType

        error = GitHubHttpError(
            "API timeout",
            failure_type=FailureType.SYSTEMIC,
        )

        # Systemic failures indicate infrastructure problems
        # The orchestrator should pause, probe, and resume
        assert error.is_systemic()
        # No issue_number because it affects all operations
        assert error.issue_number is None

    def test_issue_local_failure_should_trigger_needs_reconcile(self):
        """Test that issue-local failures are classified for needs-reconcile label.

        Per Phase 4 spec: ISSUE_LOCAL -> apply needs-reconcile label for that issue and skip
        """
        from issue_orchestrator.adapters.github.http_client import GitHubHttpError
        from issue_orchestrator.ports.verification import FailureType

        error = GitHubHttpError(
            "Write didn't take effect",
            failure_type=FailureType.ISSUE_LOCAL,
            issue_number=123,
        )

        # Issue-local failures affect only one issue
        # The orchestrator should apply needs-reconcile label and skip
        assert error.is_issue_local()
        assert error.issue_number == 123
