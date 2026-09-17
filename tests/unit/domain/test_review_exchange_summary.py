"""Tests for the typed review-exchange summary payload."""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from issue_orchestrator.domain.review_exchange_summary import (
    ReviewExchangeReason,
    ReviewExchangeStatus,
    ReviewExchangeSummaryArtifactRef,
    ReviewExchangeSummaryV1,
    ReviewExchangeTerminalState,
)


def _terminal() -> ReviewExchangeTerminalState:
    return ReviewExchangeTerminalState(
        status=ReviewExchangeStatus.OK,
        reason=ReviewExchangeReason.REVIEWER_OK,
    )


class TestReviewExchangeSummaryArtifactRef:
    def test_payload_round_trip_preserves_all_fields(self) -> None:
        artifact = ReviewExchangeSummaryArtifactRef.from_payload(
            {
                "type": "review_report",
                "label": "Review report",
                "value": "/tmp/review-report.md",
                "render_mode": "markdown",
            }
        )

        assert artifact.to_payload() == {
            "type": "review_report",
            "label": "Review report",
            "value": "/tmp/review-report.md",
            "render_mode": "markdown",
        }

    def test_rejects_missing_required_fields(self) -> None:
        with pytest.raises(ValueError, match="requires non-empty label"):
            ReviewExchangeSummaryArtifactRef.from_payload(
                {
                    "type": "review_report",
                    "value": "/tmp/review-report.md",
                }
            )


class TestReviewExchangeTerminalState:
    def test_accepts_only_known_status_reason_pairs(self) -> None:
        terminal = ReviewExchangeTerminalState(
            status=ReviewExchangeStatus.ERROR,
            reason=ReviewExchangeReason.REVIEWER_DECISION_INVALID,
        )

        assert terminal.status is ReviewExchangeStatus.ERROR
        assert terminal.reason is ReviewExchangeReason.REVIEWER_DECISION_INVALID

    def test_rejects_crossed_status_reason_pairs(self) -> None:
        with pytest.raises(ValueError, match="invalid review-exchange terminal state"):
            ReviewExchangeTerminalState(
                status=ReviewExchangeStatus.OK,
                reason=ReviewExchangeReason.CODER_NO_COMPLETION,
            )


class TestReviewExchangeSummaryV1:
    def test_payload_round_trip_preserves_typed_contract(self) -> None:
        summary = ReviewExchangeSummaryV1(
            completed_rounds=2,
            terminal=_terminal(),
            response_text="Approved.",
            timestamp="2026-06-04T10:15:00Z",
            head_sha="abc123",
            validation_passed=True,
            artifacts=(
                ReviewExchangeSummaryArtifactRef(
                    artifact_type="review_report",
                    label="Review report",
                    value="/tmp/review-report.md",
                    render_mode="markdown",
                ),
            ),
            detail="final reviewer decision",
        )

        recovered = ReviewExchangeSummaryV1.from_payload(summary.to_payload())

        assert recovered == summary
        assert recovered.status is ReviewExchangeStatus.OK
        assert recovered.reason is ReviewExchangeReason.REVIEWER_OK

    def test_is_frozen(self) -> None:
        summary = ReviewExchangeSummaryV1(
            completed_rounds=1,
            terminal=_terminal(),
            response_text=None,
            timestamp="2026-06-04T10:15:00Z",
        )

        with pytest.raises(FrozenInstanceError):
            summary.completed_rounds = 2  # type: ignore[misc]

    def test_with_review_subject_if_missing_returns_new_typed_summary(self) -> None:
        summary = ReviewExchangeSummaryV1(
            completed_rounds=1,
            terminal=_terminal(),
            response_text=None,
            timestamp="2026-06-04T10:15:00Z",
        )

        updated = summary.with_review_subject_if_missing(" abc123 ", " 123-fix ")

        assert updated is not summary
        assert updated.head_sha == "abc123"
        assert updated.branch_name == "123-fix"
        assert summary.head_sha is None
        assert summary.branch_name is None

    def test_a_review_subject_the_loop_recorded_is_never_overwritten(self) -> None:
        """What the exchange wrote is closer to the review than a later caller."""
        summary = ReviewExchangeSummaryV1(
            completed_rounds=1,
            terminal=_terminal(),
            response_text=None,
            timestamp="2026-06-04T10:15:00Z",
            head_sha="abc123",
            branch_name="123-fix",
        )

        assert (
            summary.with_review_subject_if_missing("later-sha", "renamed-since")
            is summary
        )

    def test_the_reviewed_branch_survives_a_payload_round_trip(self) -> None:
        """A replay reads the branch back off disk, so it must serialize."""
        summary = ReviewExchangeSummaryV1(
            completed_rounds=1,
            terminal=_terminal(),
            response_text=None,
            timestamp="2026-06-04T10:15:00Z",
            branch_name="123-the-branch-reviewed",
        )

        payload = summary.to_payload()

        assert payload["branch_name"] == "123-the-branch-reviewed"
        assert (
            ReviewExchangeSummaryV1.from_payload(payload).branch_name
            == "123-the-branch-reviewed"
        )

    def test_a_summary_with_no_branch_omits_the_key(self) -> None:
        summary = ReviewExchangeSummaryV1(
            completed_rounds=1,
            terminal=_terminal(),
            response_text=None,
            timestamp="2026-06-04T10:15:00Z",
        )

        assert "branch_name" not in summary.to_payload()

    def test_rejects_bool_completed_rounds(self) -> None:
        with pytest.raises(ValueError, match="requires int completed_rounds"):
            ReviewExchangeSummaryV1.from_payload(
                {
                    "completed_rounds": True,
                    "status": "ok",
                    "reason": "reviewer_ok",
                    "response_text": None,
                    "timestamp": "2026-06-04T10:15:00Z",
                }
            )

    def test_rejects_untyped_artifact_entries(self) -> None:
        with pytest.raises(TypeError, match="artifacts must contain"):
            ReviewExchangeSummaryV1(
                completed_rounds=1,
                terminal=_terminal(),
                response_text=None,
                timestamp="2026-06-04T10:15:00Z",
                artifacts=({"type": "review_report"},),  # type: ignore[arg-type]
            )
