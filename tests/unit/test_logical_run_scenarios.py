"""Scenario tests for logical-run projection semantics.

These tests encode operator-facing expectations as concrete timeline scenarios.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from issue_orchestrator.domain.logical_event_semantics import enrich_logical_semantics
from issue_orchestrator.timeline import TIMELINE_SCHEMA_VERSION
from issue_orchestrator.view_models.issue_detail import build_issue_detail_view_model


def _evt(event: str, **kw: object) -> dict[str, object]:
    base: dict[str, object] = {
        "event": event,
        "timestamp": "2026-02-09T20:15:00Z",
        "status": "started",
        "step": event.split(".")[-1] if "." in event else event,
        "phase": "in_progress",
    }
    base.update(kw)
    return base


@dataclass(frozen=True)
class LogicalRunScenario:
    name: str
    events: list[dict[str, Any]]
    expected_run_count: int
    expected_cycles_per_run: list[int]
    expected_review_events_in_latest_run: int
    expected_latest_session_run_ids: list[str] | None = None


SCENARIOS: list[LogicalRunScenario] = [
    LogicalRunScenario(
        name="single_code_attempt_no_review",
        events=[
            _evt("session.started", timestamp="2026-02-09T10:00:00Z", run_id="run-1", rework_cycle=0),
            _evt("session.completed", timestamp="2026-02-09T10:20:00Z", status="completed", run_id="run-1", rework_cycle=0),
        ],
        expected_run_count=1,
        expected_cycles_per_run=[1],
        expected_review_events_in_latest_run=0,
    ),
    LogicalRunScenario(
        name="code_then_review_same_logical_run",
        events=[
            _evt("session.started", timestamp="2026-02-09T10:00:00Z", run_id="code-1", rework_cycle=0),
            _evt("session.completed", timestamp="2026-02-09T10:20:00Z", status="completed", run_id="code-1", rework_cycle=0),
            _evt("review.started", timestamp="2026-02-09T10:21:00Z", run_id="review-1", rework_cycle=0, task="review"),
            _evt("review.approved", timestamp="2026-02-09T10:25:00Z", status="completed", run_id="review-1", rework_cycle=0),
        ],
        expected_run_count=1,
        expected_cycles_per_run=[1],
        expected_review_events_in_latest_run=2,
    ),
    LogicalRunScenario(
        name="code_review_rework_chain_single_logical_run",
        events=[
            _evt("session.started", timestamp="2026-02-09T10:00:00Z", run_id="code-1", rework_cycle=0),
            _evt("session.completed", timestamp="2026-02-09T10:20:00Z", status="completed", run_id="code-1", rework_cycle=0),
            _evt("review.started", timestamp="2026-02-09T10:21:00Z", run_id="review-1", rework_cycle=0, task="review"),
            _evt("review.changes_requested", timestamp="2026-02-09T10:23:00Z", status="failed", run_id="review-1", rework_cycle=0),
            _evt("rework.started", timestamp="2026-02-09T10:30:00Z", run_id="rework-1", rework_cycle=1),
            _evt("session.completed", timestamp="2026-02-09T10:45:00Z", status="completed", run_id="rework-1", rework_cycle=1),
        ],
        expected_run_count=1,
        expected_cycles_per_run=[2],
        expected_review_events_in_latest_run=2,
    ),
    LogicalRunScenario(
        name="local_loop_two_review_rounds_split_into_two_cycles",
        events=[
            _evt("session.started", timestamp="2026-02-09T10:00:00Z", run_id="code-1", rework_cycle=0),
            _evt("session.completed", timestamp="2026-02-09T10:20:00Z", status="completed", run_id="code-1", rework_cycle=0),
            _evt("review.started", timestamp="2026-02-09T10:21:00Z", run_id="exchange-1", task="review"),
            _evt("review_exchange.started", timestamp="2026-02-09T10:21:30Z", run_id="exchange-1", task="review"),
            _evt(
                "review_exchange.round_started",
                timestamp="2026-02-09T10:22:00Z",
                run_id="exchange-1",
                task="review",
                round_index=1,
            ),
            _evt(
                "review_exchange.round_completed",
                timestamp="2026-02-09T10:23:00Z",
                run_id="exchange-1",
                task="review",
                round_index=1,
                reviewer_response_type="changes_requested",
                summary="round 1 changes_requested",
            ),
            _evt(
                "review.rework_started",
                timestamp="2026-02-09T10:24:00Z",
                run_id="exchange-1",
                task="rework",
                round_index=1,
            ),
            _evt(
                "review.rework_completed",
                timestamp="2026-02-09T10:27:00Z",
                run_id="exchange-1",
                task="rework",
                round_index=1,
            ),
            _evt(
                "review_exchange.round_started",
                timestamp="2026-02-09T10:28:00Z",
                run_id="exchange-1",
                task="review",
                round_index=2,
            ),
            _evt(
                "review_exchange.round_completed",
                timestamp="2026-02-09T10:29:00Z",
                run_id="exchange-1",
                task="review",
                round_index=2,
                reviewer_response_type="ok",
                summary="round 2 ok",
            ),
            _evt(
                "review_exchange.completed",
                timestamp="2026-02-09T10:29:30Z",
                run_id="exchange-1",
                task="review",
                rounds=2,
                summary="2 rounds complete",
            ),
            _evt(
                "review.approved",
                timestamp="2026-02-09T10:30:00Z",
                run_id="exchange-1",
                task="review",
                rounds=2,
                status="completed",
            ),
        ],
        expected_run_count=1,
        expected_cycles_per_run=[2],
        expected_review_events_in_latest_run=3,
        expected_latest_session_run_ids=["code-1", "exchange-1"],
    ),
    LogicalRunScenario(
        name="terminal_block_then_new_attempt_creates_new_logical_run",
        events=[
            _evt("session.started", timestamp="2026-02-09T10:00:00Z", run_id="run-a", rework_cycle=0),
            _evt("session.completed", timestamp="2026-02-09T10:20:00Z", status="completed", run_id="run-a", rework_cycle=0),
            _evt("issue.blocked", timestamp="2026-02-09T10:30:00Z", status="failed"),
            _evt("session.started", timestamp="2026-02-09T11:00:00Z", run_id="run-b", rework_cycle=0),
            _evt("session.completed", timestamp="2026-02-09T11:20:00Z", status="completed", run_id="run-b", rework_cycle=0),
        ],
        expected_run_count=2,
        expected_cycles_per_run=[1, 1],
        expected_review_events_in_latest_run=0,
    ),
    LogicalRunScenario(
        name="restart_mid_run_keeps_single_logical_run",
        events=[
            _evt("session.started", timestamp="2026-02-09T10:00:00Z", run_id="code-1", rework_cycle=0),
            _evt("session.completed", timestamp="2026-02-09T10:20:00Z", status="completed", run_id="code-1", rework_cycle=0),
            _evt("orchestrator.restarted", timestamp="2026-02-09T10:21:00Z"),
            _evt("review.started", timestamp="2026-02-09T10:22:00Z", run_id="review-1", rework_cycle=0, task="review"),
            _evt("review.approved", timestamp="2026-02-09T10:24:00Z", status="completed", run_id="review-1", rework_cycle=0),
        ],
        expected_run_count=1,
        expected_cycles_per_run=[1],
        expected_review_events_in_latest_run=2,
        expected_latest_session_run_ids=["code-1", "review-1"],
    ),
    LogicalRunScenario(
        name="manual_unblock_after_terminal_starts_new_logical_run",
        events=[
            _evt("session.started", timestamp="2026-02-09T10:00:00Z", run_id="run-a", rework_cycle=0),
            _evt("session.completed", timestamp="2026-02-09T10:10:00Z", status="completed", run_id="run-a", rework_cycle=0),
            _evt("issue.blocked", timestamp="2026-02-09T10:12:00Z", status="failed"),
            _evt("issue.unblocked", timestamp="2026-02-09T10:20:00Z"),
            _evt("session.started", timestamp="2026-02-09T10:25:00Z", run_id="run-b", rework_cycle=0),
            _evt("session.completed", timestamp="2026-02-09T10:35:00Z", status="completed", run_id="run-b", rework_cycle=0),
        ],
        expected_run_count=2,
        expected_cycles_per_run=[1, 1],
        expected_review_events_in_latest_run=0,
        expected_latest_session_run_ids=["run-b"],
    ),
    LogicalRunScenario(
        name="mixed_legacy_and_signal_events_split_logical_runs",
        events=[
            _evt("session.started", timestamp="2026-02-09T10:00:00Z", run_id="legacy-1"),
            _evt("session.completed", timestamp="2026-02-09T10:10:00Z", status="completed", run_id="legacy-1"),
            _evt("session.started", timestamp="2026-02-09T10:20:00Z", run_id="signal-1", rework_cycle=0),
            _evt("session.completed", timestamp="2026-02-09T10:30:00Z", status="completed", run_id="signal-1", rework_cycle=0),
        ],
        expected_run_count=1,
        expected_cycles_per_run=[1],
        expected_review_events_in_latest_run=0,
        expected_latest_session_run_ids=None,
    ),
    LogicalRunScenario(
        name="duplicate_start_events_do_not_create_extra_logical_runs",
        events=[
            _evt("session.started", timestamp="2026-02-09T10:00:00Z", run_id="dup-1", rework_cycle=0),
            _evt("session.started", timestamp="2026-02-09T10:01:00Z", run_id="dup-1", rework_cycle=0),
            _evt("session.completed", timestamp="2026-02-09T10:10:00Z", status="completed", run_id="dup-1", rework_cycle=0),
        ],
        expected_run_count=1,
        expected_cycles_per_run=[1],
        expected_review_events_in_latest_run=0,
        expected_latest_session_run_ids=["dup-1"],
    ),
    LogicalRunScenario(
        name="out_of_order_timestamps_keep_grouping_deterministic",
        events=[
            _evt("session.started", timestamp="2026-02-09T10:05:00Z", run_id="code-1", rework_cycle=0),
            _evt("review.started", timestamp="2026-02-09T10:04:00Z", run_id="review-1", rework_cycle=0, task="review"),
            _evt("session.completed", timestamp="2026-02-09T10:06:00Z", status="completed", run_id="code-1", rework_cycle=0),
            _evt("review.approved", timestamp="2026-02-09T10:07:00Z", status="completed", run_id="review-1", rework_cycle=0),
        ],
        expected_run_count=1,
        expected_cycles_per_run=[1],
        expected_review_events_in_latest_run=2,
        expected_latest_session_run_ids=["code-1", "review-1"],
    ),
    LogicalRunScenario(
        name="approved_then_start_without_requeue_stays_same_logical_run",
        events=[
            _evt("session.started", timestamp="2026-02-09T10:00:00Z", run_id="code-1", rework_cycle=0),
            _evt("session.completed", timestamp="2026-02-09T10:15:00Z", status="completed", run_id="code-1", rework_cycle=0),
            _evt("review.started", timestamp="2026-02-09T10:16:00Z", run_id="review-1", rework_cycle=0, task="review"),
            _evt("review.approved", timestamp="2026-02-09T10:18:00Z", status="completed", run_id="review-1", rework_cycle=0),
            _evt("session.started", timestamp="2026-02-09T10:20:00Z", run_id="code-2", rework_cycle=0),
            _evt("session.completed", timestamp="2026-02-09T10:30:00Z", status="completed", run_id="code-2", rework_cycle=0),
        ],
        expected_run_count=1,
        expected_cycles_per_run=[1],
        expected_review_events_in_latest_run=2,
        expected_latest_session_run_ids=["code-1", "review-1", "code-2"],
    ),
    LogicalRunScenario(
        # pr-pending label churns naturally during the PR lifecycle (e.g.
        # PR-feedback rework after CR comments lands, label is removed and
        # later re-added). That churn must not split a single continuous
        # issue lifecycle into multiple logical runs.
        name="pr_pending_removed_then_new_start_stays_single_logical_run",
        events=[
            _evt("session.started", timestamp="2026-02-09T10:00:00Z", run_id="code-1", rework_cycle=0),
            _evt("session.completed", timestamp="2026-02-09T10:15:00Z", status="completed", run_id="code-1", rework_cycle=0),
            _evt("review.started", timestamp="2026-02-09T10:16:00Z", run_id="review-1", rework_cycle=0, task="review"),
            _evt("review.approved", timestamp="2026-02-09T10:18:00Z", status="completed", run_id="review-1", rework_cycle=0),
            _evt("issue.labels_changed", timestamp="2026-02-09T10:19:00Z", removed=["pr-pending"]),
            _evt("session.started", timestamp="2026-02-09T10:20:00Z", run_id="code-2", rework_cycle=0),
            _evt("session.completed", timestamp="2026-02-09T10:30:00Z", status="completed", run_id="code-2", rework_cycle=0),
        ],
        expected_run_count=1,
        expected_cycles_per_run=[1],
        expected_review_events_in_latest_run=2,
        expected_latest_session_run_ids=["code-1", "review-1", "code-2"],
    ),
    LogicalRunScenario(
        # cleanup.completed is a routine between-phases artifact, not a real
        # terminator. A subsequent session.started (e.g. PR-feedback rework)
        # is the next phase of the same run, not a new run.
        name="cleanup_completed_then_new_start_stays_single_logical_run",
        events=[
            _evt("session.started", timestamp="2026-02-09T10:00:00Z", run_id="code-1", rework_cycle=0),
            _evt("session.completed", timestamp="2026-02-09T10:15:00Z", status="completed", run_id="code-1", rework_cycle=0),
            _evt("review.started", timestamp="2026-02-09T10:16:00Z", run_id="review-1", rework_cycle=0, task="review"),
            _evt("review.approved", timestamp="2026-02-09T10:18:00Z", status="completed", run_id="review-1", rework_cycle=0),
            _evt("cleanup.completed", timestamp="2026-02-09T10:19:00Z"),
            _evt("claim.acquired", timestamp="2026-02-09T14:00:00Z"),
            _evt("session.started", timestamp="2026-02-09T14:01:00Z", run_id="code-2", rework_cycle=0),
            _evt("session.completed", timestamp="2026-02-09T14:20:00Z", status="completed", run_id="code-2", rework_cycle=0),
        ],
        expected_run_count=1,
        expected_cycles_per_run=[1],
        expected_review_events_in_latest_run=2,
        expected_latest_session_run_ids=["code-1", "review-1", "code-2"],
    ),
    LogicalRunScenario(
        name="mixed_missing_and_present_run_ids_stays_single_logical_run",
        events=[
            _evt("session.started", timestamp="2026-02-09T10:00:00Z", run_id="code-1", rework_cycle=0),
            _evt("session.completed", timestamp="2026-02-09T10:10:00Z", status="completed", rework_cycle=0),
            _evt("review.started", timestamp="2026-02-09T10:11:00Z", run_id="review-1", rework_cycle=0, task="review"),
            _evt("review.approved", timestamp="2026-02-09T10:12:00Z", status="completed", rework_cycle=0),
        ],
        expected_run_count=1,
        expected_cycles_per_run=[1],
        expected_review_events_in_latest_run=2,
        expected_latest_session_run_ids=["code-1", "review-1"],
    ),
    LogicalRunScenario(
        name="multiple_terminal_signals_split_only_on_next_start",
        events=[
            _evt("session.started", timestamp="2026-02-09T10:00:00Z", run_id="run-a", rework_cycle=0),
            _evt("session.completed", timestamp="2026-02-09T10:10:00Z", status="completed", run_id="run-a", rework_cycle=0),
            _evt("issue.completed", timestamp="2026-02-09T10:11:00Z", status="completed"),
            _evt("issue.blocked", timestamp="2026-02-09T10:12:00Z", status="failed"),
            _evt("session.started", timestamp="2026-02-09T10:20:00Z", run_id="run-b", rework_cycle=0),
            _evt("session.completed", timestamp="2026-02-09T10:30:00Z", status="completed", run_id="run-b", rework_cycle=0),
        ],
        expected_run_count=2,
        expected_cycles_per_run=[1, 1],
        expected_review_events_in_latest_run=0,
        expected_latest_session_run_ids=["run-b"],
    ),
    LogicalRunScenario(
        name="rapid_duplicate_transitions_do_not_create_extra_logical_run",
        events=[
            _evt("session.started", timestamp="2026-02-09T10:00:00Z", run_id="run-1", rework_cycle=0),
            _evt("session.completed", timestamp="2026-02-09T10:05:00Z", status="completed", run_id="run-1", rework_cycle=0),
            _evt("session.completed", timestamp="2026-02-09T10:05:01Z", status="completed", run_id="run-1", rework_cycle=0),
            _evt("review.started", timestamp="2026-02-09T10:05:02Z", run_id="review-1", rework_cycle=0, task="review"),
            _evt("review.approved", timestamp="2026-02-09T10:06:00Z", status="completed", run_id="review-1", rework_cycle=0),
        ],
        expected_run_count=1,
        expected_cycles_per_run=[1],
        expected_review_events_in_latest_run=2,
        expected_latest_session_run_ids=["run-1", "review-1"],
    ),
    LogicalRunScenario(
        name="review_exchange_halt_failed_then_restart_creates_new_run",
        events=[
            _evt("session.started", timestamp="2026-02-09T08:38:39Z", run_id="code-1", rework_cycle=0),
            _evt("review.started", timestamp="2026-02-09T08:42:02Z", run_id="review-1", rework_cycle=0, task="review"),
            _evt("review.changes_requested", timestamp="2026-02-09T08:51:14Z", status="failed", run_id="review-1", rework_cycle=0),
            _evt("session.failed", timestamp="2026-02-09T08:52:33Z", status="failed", run_id="code-1"),
            _evt("validation.completed", timestamp="2026-02-09T08:52:34Z", status="completed"),
            _evt("session.started", timestamp="2026-02-09T10:06:57Z", run_id="code-2", rework_cycle=0),
            _evt("review.started", timestamp="2026-02-09T10:11:31Z", run_id="review-2", rework_cycle=0, task="review"),
            _evt("review.changes_requested", timestamp="2026-02-09T10:16:45Z", status="failed", run_id="review-2", rework_cycle=0),
            _evt("session.failed", timestamp="2026-02-09T10:19:23Z", status="failed", run_id="code-2"),
            _evt("validation.completed", timestamp="2026-02-09T10:19:24Z", status="completed"),
            _evt("session.started", timestamp="2026-02-09T10:28:40Z", run_id="code-3", rework_cycle=0),
        ],
        expected_run_count=3,
        expected_cycles_per_run=[1, 1, 1],
        expected_review_events_in_latest_run=0,
        expected_latest_session_run_ids=["code-3"],
    ),
    LogicalRunScenario(
        name="long_history_latest_run_derived_from_tail_activity",
        events=[
            _evt("session.started", timestamp="2026-02-09T08:00:00Z", run_id="old-1", rework_cycle=0),
            _evt("session.completed", timestamp="2026-02-09T08:10:00Z", status="completed", run_id="old-1", rework_cycle=0),
            _evt("issue.blocked", timestamp="2026-02-09T08:11:00Z", status="failed"),
            _evt("session.started", timestamp="2026-02-09T09:00:00Z", run_id="old-2", rework_cycle=0),
            _evt("session.completed", timestamp="2026-02-09T09:10:00Z", status="completed", run_id="old-2", rework_cycle=0),
            _evt("issue.blocked", timestamp="2026-02-09T09:11:00Z", status="failed"),
            _evt("session.started", timestamp="2026-02-09T10:00:00Z", run_id="new-1", rework_cycle=0),
            _evt("session.completed", timestamp="2026-02-09T10:10:00Z", status="completed", run_id="new-1", rework_cycle=0),
        ],
        expected_run_count=3,
        expected_cycles_per_run=[1, 1, 1],
        expected_review_events_in_latest_run=0,
        expected_latest_session_run_ids=["new-1"],
    ),
    LogicalRunScenario(
        name="replayed_start_events_without_ids_are_treated_as_new_attempt",
        events=[
            _evt("session.started", timestamp="2026-02-09T08:00:00Z", run_id="old-1", rework_cycle=0),
            _evt("session.completed", timestamp="2026-02-09T08:10:00Z", status="completed", run_id="old-1", rework_cycle=0),
            _evt("issue.blocked", timestamp="2026-02-09T08:11:00Z", status="failed"),
            _evt("session.started", timestamp="2026-02-09T09:00:00Z", run_id="new-1", rework_cycle=0),
            _evt("session.completed", timestamp="2026-02-09T09:10:00Z", status="completed", run_id="new-1", rework_cycle=0),
            # Replay/noisy append from prior run.
            _evt("session.started", timestamp="2026-02-09T09:11:00Z", run_id="old-1", rework_cycle=0),
            _evt("session.completed", timestamp="2026-02-09T09:12:00Z", status="completed", run_id="old-1", rework_cycle=0),
        ],
        expected_run_count=2,
        expected_cycles_per_run=[1, 1],
        expected_review_events_in_latest_run=0,
        expected_latest_session_run_ids=["new-1", "old-1"],
    ),
]


def _events_with_semantics(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    enriched: list[dict[str, Any]] = []
    previous_event_name: str | None = None
    previous_data: dict[str, Any] | None = None
    for event in events:
        entry = dict(event)
        semantics = enrich_logical_semantics(
            event_name=str(entry.get("event") or ""),
            event_data=entry,
            previous_event_name=previous_event_name,
            previous_data=previous_data,
        )
        entry["timeline_schema_version"] = TIMELINE_SCHEMA_VERSION
        entry["event_intent"] = semantics.event_intent
        entry["review_oriented"] = semantics.review_oriented
        entry["logical_run"] = semantics.logical_run
        entry["logical_cycle"] = semantics.logical_cycle
        entry["logical_phase"] = semantics.logical_phase
        entry["_logical_restart_pending"] = semantics.restart_pending
        enriched.append(entry)
        previous_event_name = str(entry.get("event") or "")
        previous_data = entry
    return enriched


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[s.name for s in SCENARIOS])
def test_logical_run_scenarios(scenario: LogicalRunScenario) -> None:
    events = _events_with_semantics(scenario.events)
    payload = build_issue_detail_view_model(
        issue_number=4057,
        title="Scenario Test",
        issue_url="https://github.com/test/repo/issues/4057",
        events=events,
        phase_toc=[],
        cycles=[],
    )

    assert payload["run_count"] == scenario.expected_run_count
    runs = payload["runs"]
    assert [len(run["cycles"]) for run in runs] == scenario.expected_cycles_per_run

    latest_run = runs[-1]
    review_events = sum(
        1
        for cycle in latest_run["cycles"]
        for step in cycle.get("steps", [])
        if str(step.get("event", "")).startswith("review.")
    )
    assert review_events == scenario.expected_review_events_in_latest_run
    if scenario.expected_latest_session_run_ids is not None:
        assert latest_run["session_run_ids"] == scenario.expected_latest_session_run_ids

    # Invariant: cycle_in_run must be contiguous and run-local.
    for run in runs:
        assert [c["cycle_in_run"] for c in run["cycles"]] == list(range(1, len(run["cycles"]) + 1))

    # UX invariant: only the latest run may appear in progress.
    for run in runs[:-1]:
        assert str(run.get("outcome", "")).lower() != "in progress"
        for cycle in run.get("cycles", []):
            assert str(cycle.get("outcome", "")).lower() != "in progress"


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[f"restart_{s.name}" for s in SCENARIOS])
def test_logical_run_projection_is_restart_stable(scenario: LogicalRunScenario) -> None:
    """Projection must be deterministic across repeated recomputation (restart-safe)."""
    events = _events_with_semantics(scenario.events)
    payload_a = build_issue_detail_view_model(
        issue_number=4057,
        title="Scenario Test",
        issue_url="https://github.com/test/repo/issues/4057",
        events=events,
        phase_toc=[],
        cycles=[],
    )
    payload_b = build_issue_detail_view_model(
        issue_number=4057,
        title="Scenario Test",
        issue_url="https://github.com/test/repo/issues/4057",
        events=events,
        phase_toc=[],
        cycles=[],
    )
    assert payload_a["run_count"] == payload_b["run_count"]
    assert payload_a["runs"] == payload_b["runs"]
