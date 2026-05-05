"""Tests for _detail_from_data() — contextual detail extraction from enriched events."""

from issue_orchestrator.timeline import _detail_from_data


class TestBlockedDetail:
    def test_includes_attempted(self) -> None:
        data = {"attempted": "Tried rebasing onto main"}
        detail = _detail_from_data("session.blocked", data, "Merge conflict")
        assert detail is not None
        assert "Tried rebasing" in detail

    def test_includes_blocked_by(self) -> None:
        data = {"blocked_by": [10, 20]}
        detail = _detail_from_data("issue.blocked", data, "Depends on other issues")
        assert detail is not None
        assert "#10" in detail
        assert "#20" in detail

    def test_skips_attempted_when_same_as_summary(self) -> None:
        data = {"attempted": "Merge conflict"}
        detail = _detail_from_data("session.blocked", data, "Merge conflict")
        # "Merge conflict" is already in summary, so it's skipped
        assert detail is None

    def test_both_attempted_and_blocked_by(self) -> None:
        data = {"attempted": "Tried 3 approaches", "blocked_by": [42]}
        detail = _detail_from_data("session.blocked", data, "blocked")
        assert detail is not None
        assert "Tried 3 approaches" in detail
        assert "#42" in detail


class TestTimeoutDetail:
    def test_runtime_and_limit(self) -> None:
        data = {"runtime_minutes": 45.2, "timeout_minutes": 45}
        detail = _detail_from_data("session.timeout", data, None)
        assert detail is not None
        assert "45 min" in detail
        assert "limit: 45" in detail

    def test_runtime_only(self) -> None:
        data = {"runtime_minutes": 30.0}
        detail = _detail_from_data("session.timeout", data, None)
        assert detail is not None
        assert "30 min" in detail

    def test_with_problems(self) -> None:
        data = {"runtime_minutes": 10.0, "problems": "Build kept failing"}
        detail = _detail_from_data("session.failed", data, None)
        assert detail is not None
        assert "Build kept failing" in detail


class TestCompletedDetail:
    def test_implementation(self) -> None:
        data = {"implementation": "Added retry logic with exponential backoff"}
        detail = _detail_from_data("session.completed", data, "completed")
        assert detail is not None
        assert "retry logic" in detail

    def test_problems(self) -> None:
        data = {"implementation": "Done", "problems": "Flaky test in CI"}
        detail = _detail_from_data("session.completed", data, "completed")
        assert detail is not None
        assert "Flaky test" in detail

    def test_skips_implementation_when_in_summary(self) -> None:
        data = {"implementation": "All good"}
        detail = _detail_from_data("session.completed", data, "All good")
        assert detail is None


class TestReviewDetail:
    def test_changes_requested_with_issues(self) -> None:
        data = {"review_issues": "Missing tests for edge cases"}
        detail = _detail_from_data("review.changes_requested", data, "Changes requested")
        assert detail is not None
        assert "Missing tests" in detail

    def test_changes_requested_with_risk(self) -> None:
        data = {"review_issues": "Missing tests", "risk_level": "high"}
        detail = _detail_from_data("review.changes_requested", data, "x")
        assert detail is not None
        assert "Risk: high" in detail

    # `test_approved_with_summary` removed — the corresponding
    # `_detail_from_data` branch read `data["review_summary"]`, but no
    # production emitter populates that key. Approval text reaches the
    # user via `_summary_from_data` (which reads `data["summary"]`).
    # See the persistent-session goldens series for the audit that
    # surfaced this dead code.

    def test_escalated_with_rework_info(self) -> None:
        data = {"rework_cycle": 3, "max_rework_cycles": 3}
        detail = _detail_from_data("review.escalated", data, "Escalated")
        assert detail is not None
        assert "3/3" in detail

    def test_comment_added_uses_excerpt(self) -> None:
        data = {"comment_excerpt": "Please extract this helper and add a test for edge-case labels."}
        detail = _detail_from_data("review.comment_added", data, "Posted review comment")
        assert detail is not None
        assert "extract this helper" in detail


class TestNeedsHumanDetail:
    def test_question(self) -> None:
        data = {"question": "Which API endpoint should I use?"}
        detail = _detail_from_data("issue.needs_human", data, "Needs human input")
        assert detail is not None
        assert "Which API endpoint" in detail


class TestValidationFailedDetail:
    def test_validation_reason(self) -> None:
        data = {"validation_reason": "3 lint errors found"}
        detail = _detail_from_data("session.validation_failed", data, "Validation failed")
        assert detail is not None
        assert "3 lint errors" in detail


class TestNoDetail:
    def test_unknown_event(self) -> None:
        detail = _detail_from_data("some.unknown_event", {}, None)
        assert detail is None

    def test_empty_data(self) -> None:
        detail = _detail_from_data("session.blocked", {}, None)
        assert detail is None

    def test_started_event(self) -> None:
        detail = _detail_from_data("session.started", {}, None)
        assert detail is None


class TestTruncation:
    def test_long_detail_truncated(self) -> None:
        data = {"attempted": "x" * 300}
        detail = _detail_from_data("session.blocked", data, "reason")
        assert detail is not None
        assert len(detail) <= 200
        assert detail.endswith("\u2026")

    def test_review_comment_allows_longer_detail(self) -> None:
        comment = "x" * 350
        data = {"comment_excerpt": comment}
        detail = _detail_from_data("review.comment_added", data, "Posted review comment")
        assert detail is not None
        assert detail == comment
