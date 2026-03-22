from __future__ import annotations

from pathlib import Path

from tests.unit.review_timeline_scenario import ReviewTimelineScenario


def test_story_review_start_cluster_keeps_round_started_phase_context(tmp_path: Path) -> None:
    scenario = ReviewTimelineScenario.create(tmp_path).with_reviewer_round(round_index=1)

    detail = scenario.render_issue_detail(
        scenario.review_started(),
        scenario.review_exchange_started(),
        scenario.review_round_started(round_index=1),
    )

    detail.assert_step_events("review_exchange.round_started")
    detail.assert_narrative("review_exchange.round_started", "Code review started")
    detail.assert_phase_scoped_review_artifacts(
        event_name="review_exchange.round_started",
        round_index=1,
        session_role="reviewer",
        transcript_role="reviewer",
    )


def test_review_rework_step_keeps_coder_phase_context_after_timeline_round_trip(tmp_path: Path) -> None:
    scenario = ReviewTimelineScenario.create(tmp_path).with_coder_round(round_index=2)

    detail = scenario.render_issue_detail(
        scenario.review_rework_started(round_index=2),
    )

    detail.assert_step_events("review.rework_started")
    detail.assert_phase_scoped_review_artifacts(
        event_name="review.rework_started",
        round_index=2,
        session_role="coder",
        transcript_role="coder",
    )


def test_review_round_completed_step_keeps_reviewer_phase_context_after_timeline_round_trip(
    tmp_path: Path,
) -> None:
    scenario = ReviewTimelineScenario.create(tmp_path).with_reviewer_round(round_index=2)

    detail = scenario.render_issue_detail(
        scenario.review_round_completed(round_index=2),
    )

    detail.assert_step_events("review_exchange.round_completed")
    detail.assert_phase_scoped_review_artifacts(
        event_name="review_exchange.round_completed",
        round_index=2,
        session_role="reviewer",
        transcript_role="reviewer",
    )


def test_review_approved_story_step_keeps_final_round_context(tmp_path: Path) -> None:
    scenario = ReviewTimelineScenario.create(tmp_path).with_reviewer_round(round_index=2)

    detail = scenario.render_issue_detail(
        scenario.review_round_completed(round_index=2),
        scenario.review_exchange_completed(),
        scenario.review_approved(rounds=2),
    )

    detail.assert_step_events("review.approved")
    detail.assert_phase_scoped_review_artifacts(
        event_name="review.approved",
        round_index=2,
        session_role="reviewer",
        transcript_role="reviewer",
    )


def test_review_changes_requested_story_step_keeps_final_round_context(tmp_path: Path) -> None:
    scenario = ReviewTimelineScenario.create(tmp_path).with_reviewer_round(round_index=2)

    detail = scenario.render_issue_detail(
        scenario.review_round_completed(round_index=2),
        scenario.review_exchange_completed(),
        scenario.review_changes_requested(rounds=2),
    )

    detail.assert_step_events("review.changes_requested")
    detail.assert_phase_scoped_review_artifacts(
        event_name="review.changes_requested",
        round_index=2,
        session_role="reviewer",
        transcript_role="reviewer",
    )
