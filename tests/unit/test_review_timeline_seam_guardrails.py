from __future__ import annotations

from pathlib import Path

import pytest

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


@pytest.mark.parametrize(
    ("artifact_kind", "artifact_label", "artifact_filename", "render_mode"),
    [
        (
            "prompt",
            "Prompt",
            "round-1-reviewer-attempt-1.prompt.md",
            "text",
        ),
        (
            "review_response",
            "Review Response",
            "round-1-reviewer-attempt-1.result.json",
            "json",
        ),
        (
            "chapter_sidecar",
            "Replay Chapters",
            "round-1-reviewer-attempt-1.chapters.json",
            "json",
        ),
    ],
)
def test_role_prompted_step_keeps_role_and_typed_artifact_after_timeline_round_trip(
    tmp_path: Path,
    artifact_kind: str,
    artifact_label: str,
    artifact_filename: str,
    render_mode: str,
) -> None:
    scenario = ReviewTimelineScenario.create(tmp_path).with_reviewer_round(round_index=1)
    artifact_path = scenario.run_dir / "review-exchange" / "turns" / artifact_filename
    artifact_path.parent.mkdir(parents=True, exist_ok=True)
    artifact_path.write_text(f"{artifact_kind} artifact", encoding="utf-8")

    detail = scenario.render_issue_detail(
        scenario.review_role_prompted(
            round_index=1,
            role="reviewer",
            artifact_refs=[
                {
                    "kind": artifact_kind,
                    "label": artifact_label,
                    "path": str(artifact_path),
                    "render_mode": render_mode,
                },
            ],
        ),
        view="ops",
    )

    detail.assert_step_events("review_exchange.role_prompted")
    detail.assert_phase_scoped_review_artifacts(
        event_name="review_exchange.role_prompted",
        round_index=1,
        session_role="reviewer",
        transcript_role="reviewer",
    )
    actions = detail.step("review_exchange.role_prompted").get("actions") or []
    artifact_actions = [
        action for action in actions
        if action.get("type") == "open_path" and action.get("path") == str(artifact_path)
    ]
    assert artifact_actions == [
        {
            "type": "open_path",
            "label": f"Open {artifact_label}",
            "path": str(artifact_path),
        }
    ]


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
