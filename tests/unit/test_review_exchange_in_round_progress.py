"""In-round review-exchange progress surfacing (issue #6428).

During an in-round rework the Story timeline used to freeze on "Code review
started" for minutes while the reviewer had already responded and the coder
was reworking — an "information void". These tests pin the producer -> view
model boundary: while a review round is still open, the latest substate is
surfaced as a single transient ``in_round_progress`` row; once the round
closes, the completed Story view stays clean (no mechanic rows survive).
"""

from __future__ import annotations

from pathlib import Path

from tests.unit.review_timeline_scenario import ReviewTimelineScenario


def _open_rework_scenario(tmp_path: Path) -> ReviewTimelineScenario:
    return (
        ReviewTimelineScenario.create(tmp_path)
        .with_reviewer_round(round_index=1)
        .with_coder_round(round_index=1)
    )


def test_open_round_coder_reworking_surfaces_live_progress_row(tmp_path: Path) -> None:
    scenario = _open_rework_scenario(tmp_path)

    detail = scenario.render_issue_detail(
        scenario.review_started(),
        scenario.review_exchange_started(),
        scenario.review_round_started(round_index=1),
        scenario.review_role_prompted(round_index=1, role="reviewer"),
        scenario.review_role_feedback(
            round_index=1, role="reviewer", response_type="changes_requested"
        ),
        scenario.review_role_prompted(round_index=1, role="coder"),
    )

    # The frozen "Code review started" row is now followed by a live row.
    detail.assert_step_events(
        "review_exchange.round_started",
        "review_exchange.role_prompted",
    )
    progress = detail.step("review_exchange.role_prompted")
    assert progress["in_round_progress"] is True
    assert progress["narrative"] == "Coder running (round 1)"


def test_open_round_reviewer_requested_changes_surfaces_live_progress_row(
    tmp_path: Path,
) -> None:
    # Latest substate is the reviewer's changes_requested verdict — the coder
    # prompt has not been emitted yet.
    scenario = _open_rework_scenario(tmp_path)

    detail = scenario.render_issue_detail(
        scenario.review_started(),
        scenario.review_exchange_started(),
        scenario.review_round_started(round_index=1),
        scenario.review_role_prompted(round_index=1, role="reviewer"),
        scenario.review_role_feedback(
            round_index=1, role="reviewer", response_type="changes_requested"
        ),
    )

    detail.assert_step_events(
        "review_exchange.round_started",
        "review_exchange.role_feedback",
    )
    progress = detail.step("review_exchange.role_feedback")
    assert progress["in_round_progress"] is True
    assert progress["narrative"] == "Coder needs requested changes (round 1)"


def test_reviewer_opening_pass_does_not_duplicate_code_review_started(
    tmp_path: Path,
) -> None:
    # While the reviewer is still on its opening pass, "Code review started"
    # already represents the state — no redundant progress row is added.
    scenario = ReviewTimelineScenario.create(tmp_path).with_reviewer_round(round_index=1)

    detail = scenario.render_issue_detail(
        scenario.review_started(),
        scenario.review_exchange_started(),
        scenario.review_round_started(round_index=1),
        scenario.review_role_prompted(round_index=1, role="reviewer"),
    )

    detail.assert_step_events("review_exchange.round_started")
    assert detail.step("review_exchange.round_started").get("in_round_progress") in (
        None,
        False,
    )


def test_completed_round_stays_clean_no_progress_row(tmp_path: Path) -> None:
    # Once the round closes, the completed Story view collapses to a single
    # terminal row with no surviving mechanic / progress rows.
    scenario = _open_rework_scenario(tmp_path)

    detail = scenario.render_issue_detail(
        scenario.review_started(),
        scenario.review_exchange_started(),
        scenario.review_round_started(round_index=1),
        scenario.review_role_prompted(round_index=1, role="reviewer"),
        scenario.review_role_feedback(
            round_index=1, role="reviewer", response_type="changes_requested"
        ),
        scenario.review_role_prompted(round_index=1, role="coder"),
        scenario.review_role_feedback(round_index=1, role="coder", response_type="ok"),
        scenario.review_round_completed(round_index=1),
    )

    detail.assert_step_events("review_exchange.round_completed")
    terminal = detail.step("review_exchange.round_completed")
    assert terminal.get("in_round_progress") in (None, False)
