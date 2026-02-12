"""Tests for the issue story synthesis in issue_detail.py."""

from __future__ import annotations

import pytest

from issue_orchestrator.view_models.issue_detail import (
    IssueStoryContext,
    build_issue_detail_view_model,
    _annotate_lifecycle,
    _build_blocked_detail,
    _build_journey_cycles,
    _build_journey_steps,
    _build_previous_cycles,
    _build_status_explanation,
    _collect_cycle_artifacts,
    _derive_cycle_outcome,
    _event_to_narrative,
    _format_time_label,
    filter_last_run_cycles,
)


# ── Helpers ──────────────────────────────────────────────────────────────

def _ctx(**overrides: object) -> IssueStoryContext:
    defaults: dict[str, object] = {
        "flow_stage": "queued",
        "active_runtime_minutes": None,
        "active_task_kind": None,
        "labels": (),
        "dependency_summary": None,
        "current_rework_cycle": 0,
        "max_rework_cycles": 3,
        "pr_url": None,
        "pr_number": None,
    }
    defaults.update(overrides)
    return IssueStoryContext(**defaults)  # type: ignore[arg-type]


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


# ── Status Explanation ───────────────────────────────────────────────────

class TestStatusExplanation:

    def test_running_code_session(self):
        ctx = _ctx(flow_stage="in_progress", active_runtime_minutes=14, active_task_kind="code")
        assert _build_status_explanation(ctx, []) == "Code session in progress (14 min)"

    def test_running_review_session(self):
        ctx = _ctx(flow_stage="in_progress", active_runtime_minutes=5, active_task_kind="review")
        assert _build_status_explanation(ctx, []) == "Code review in progress (5 min)"

    def test_running_rework_session(self):
        ctx = _ctx(flow_stage="in_progress", active_runtime_minutes=8, active_task_kind="rework")
        assert _build_status_explanation(ctx, []) == "Rework session in progress (8 min)"

    def test_running_triage_session(self):
        ctx = _ctx(flow_stage="in_progress", active_runtime_minutes=2, active_task_kind="triage")
        assert _build_status_explanation(ctx, []) == "Triage review in progress (2 min)"

    def test_queued_no_dependency(self):
        ctx = _ctx(flow_stage="queued")
        assert _build_status_explanation(ctx, []) == "Waiting for an available slot"

    def test_queued_with_dependency(self):
        ctx = _ctx(flow_stage="queued", dependency_summary="#123: Fix auth bug")
        result = _build_status_explanation(ctx, [])
        assert "waiting on" in result.lower() or "#123" in result

    def test_awaiting_merge_with_pr(self):
        ctx = _ctx(flow_stage="awaiting_merge", pr_number=4085)
        result = _build_status_explanation(ctx, [])
        assert "PR #4085" in result
        assert "merge" in result.lower()

    def test_awaiting_merge_no_pr(self):
        ctx = _ctx(flow_stage="awaiting_merge")
        assert _build_status_explanation(ctx, []) == "Awaiting merge"

    def test_done_with_summary(self):
        ctx = _ctx(flow_stage="done")
        events = [_evt("session.completed", summary="Implemented retry logic")]
        result = _build_status_explanation(ctx, events)
        assert "Completed" in result
        assert "retry logic" in result

    def test_done_no_summary(self):
        ctx = _ctx(flow_stage="done")
        assert _build_status_explanation(ctx, []) == "Completed"

    def test_no_context_fallback(self):
        events = [_evt("session.started", summary="Starting work")]
        result = _build_status_explanation(None, events)
        assert result  # Should produce something, not empty

    def test_no_context_no_events(self):
        result = _build_status_explanation(None, [])
        assert result == "No events recorded"


class TestBlockedExplanation:

    def test_session_timeout(self):
        ctx = _ctx(flow_stage="blocked", labels=("blocked-failed",))
        events = [_evt("session.timeout")]
        result = _build_status_explanation(ctx, events)
        assert "timed out" in result.lower()
        assert "agent-done" in result.lower()

    def test_validation_failed_from_event(self):
        ctx = _ctx(flow_stage="blocked", labels=("validation-failed",))
        events = [_evt("session.validation_failed", summary="tests did not pass")]
        result = _build_status_explanation(ctx, events)
        assert "validation failed" in result.lower()

    def test_validation_failed_from_label(self):
        ctx = _ctx(flow_stage="blocked", labels=("validation-failed",))
        result = _build_status_explanation(ctx, [])
        assert "validation failed" in result.lower()

    def test_agent_self_reported_blocked(self):
        ctx = _ctx(flow_stage="blocked", labels=("blocked",))
        events = [_evt("session.blocked", summary="Cannot access the database")]
        result = _build_status_explanation(ctx, events)
        assert "agent reported blocked" in result.lower()
        assert "database" in result.lower()

    def test_needs_human_with_summary(self):
        ctx = _ctx(flow_stage="blocked", labels=("blocked-needs-human",))
        events = [_evt("issue.needs_human", summary="Which API version to use?")]
        result = _build_status_explanation(ctx, events)
        assert "human" in result.lower()
        assert "API version" in result

    def test_needs_human_no_event(self):
        ctx = _ctx(flow_stage="blocked", labels=("blocked-needs-human",))
        result = _build_status_explanation(ctx, [])
        assert "human" in result.lower()

    def test_rework_limit_exceeded(self):
        ctx = _ctx(
            flow_stage="blocked",
            labels=("blocked-needs-human",),
            current_rework_cycle=3,
            max_rework_cycles=3,
        )
        events = [_evt("review.escalated")]
        result = _build_status_explanation(ctx, events)
        assert "rework limit" in result.lower() or "cycle 3/3" in result

    def test_session_failed(self):
        ctx = _ctx(flow_stage="blocked", labels=("blocked-failed",))
        events = [_evt("session.failed", summary="Process crashed")]
        result = _build_status_explanation(ctx, events)
        assert "failed" in result.lower()
        assert "crashed" in result.lower()

    def test_generic_blocked(self):
        ctx = _ctx(flow_stage="blocked", labels=("blocked",))
        result = _build_status_explanation(ctx, [])
        assert result == "Blocked"


# ── Journey Steps ────────────────────────────────────────────────────────

class TestJourneySteps:

    def test_returns_all_events_with_day_field(self):
        events = [
            _evt("session.started", timestamp="2026-02-08T10:00:00Z"),
            _evt("session.completed", timestamp="2026-02-08T10:30:00Z"),
            _evt("session.started", timestamp="2026-02-09T20:00:00Z"),
            _evt("session.completed", timestamp="2026-02-09T20:30:00Z"),
        ]
        steps = _build_journey_steps(events, "2026-02-09")
        # All events returned with day field for UI filtering
        assert len(steps) == 4
        assert steps[0]["day"] == "2026-02-08"
        assert steps[2]["day"] == "2026-02-09"

    def test_day_label_includes_date_for_non_today(self):
        events = [
            _evt("session.started", timestamp="2026-02-07T10:00:00Z"),
            _evt("session.completed", timestamp="2026-02-09T10:30:00Z"),
        ]
        steps = _build_journey_steps(events, "2026-02-09")
        assert len(steps) == 2
        # Non-today event includes date in time label
        assert "Feb" in steps[0]["time_label"]
        # Today event has just time
        assert "Feb" not in steps[1]["time_label"]

    def test_skips_noisy_events(self):
        events = [
            _evt("session.started", timestamp="2026-02-09T10:00:00Z"),
            _evt("issue.labels_changed", timestamp="2026-02-09T10:01:00Z"),
            _evt("observation.scan", timestamp="2026-02-09T10:02:00Z"),
            _evt("tick.completed", timestamp="2026-02-09T10:03:00Z"),
            _evt("session.no_output", timestamp="2026-02-09T10:04:00Z"),
            _evt("session.no_completion_record", timestamp="2026-02-09T10:04:30Z"),
            _evt("stale.in_progress_detected", timestamp="2026-02-09T10:05:00Z"),
            _evt("pr.view_changed", timestamp="2026-02-09T10:06:00Z"),
            _evt("session.completed", timestamp="2026-02-09T10:30:00Z"),
        ]
        steps = _build_journey_steps(events, "2026-02-09")
        event_names = {s["event"] for s in steps}
        assert "issue.labels_changed" not in event_names
        assert "observation.scan" not in event_names
        assert "tick.completed" not in event_names
        assert "session.no_output" not in event_names
        assert "session.no_completion_record" not in event_names
        assert "stale.in_progress_detected" not in event_names
        assert "pr.view_changed" not in event_names
        assert "session.started" in event_names
        assert "session.completed" in event_names

    def test_empty_events(self):
        assert _build_journey_steps([], "2026-02-09") == []

    def test_time_label_format(self):
        events = [_evt("session.started", timestamp="2026-02-09T20:15:00Z")]
        steps = _build_journey_steps(events, "2026-02-09")
        assert steps[0]["time_label"]  # Should have some time string

    def test_detail_flows_through(self):
        events = [_evt("session.blocked", timestamp="2026-02-09T20:15:00Z",
                        detail="Tried rebasing onto main")]
        steps = _build_journey_steps(events, "2026-02-09")
        assert steps[0]["detail"] == "Tried rebasing onto main"

    def test_detail_omitted_when_absent(self):
        events = [_evt("session.started", timestamp="2026-02-09T20:15:00Z")]
        steps = _build_journey_steps(events, "2026-02-09")
        assert "detail" not in steps[0]


# ── Event Narrative ──────────────────────────────────────────────────────

class TestEventNarrative:

    def test_session_started(self):
        assert _event_to_narrative(_evt("session.started")) == "Code session started"

    def test_session_completed_with_summary(self):
        result = _event_to_narrative(_evt("session.completed", summary="Implemented auth"))
        assert result == "Agent completed: Implemented auth"

    def test_session_completed_no_summary(self):
        result = _event_to_narrative(_evt("session.completed"))
        assert result == "Agent completed"

    def test_review_changes_requested(self):
        result = _event_to_narrative(
            _evt("review.changes_requested", summary="Missing tests")
        )
        assert "Reviewer requested changes" in result
        assert "Missing tests" in result

    def test_review_approved(self):
        result = _event_to_narrative(_evt("review.approved"))
        assert "Reviewer approved" in result

    def test_pr_merged(self):
        assert _event_to_narrative(_evt("review.merged")) == "PR merged"

    def test_unknown_event_fallback(self):
        result = _event_to_narrative(_evt("custom.something", summary="did a thing"))
        assert "something" in result.lower() or "thing" in result.lower()

    def test_unknown_event_no_summary(self):
        result = _event_to_narrative(_evt("custom.something"))
        assert result  # Should produce something


# ── Previous Cycles ──────────────────────────────────────────────────────

class TestPreviousCycles:

    def test_cycles_before_today(self):
        cycles = [
            {
                "cycle": 1,
                "start": "2026-02-07T10:00:00Z",
                "end": "2026-02-07T10:30:00Z",
                "status": "completed",
                "events": [_evt("session.completed", summary="First attempt")],
            },
            {
                "cycle": 2,
                "start": "2026-02-09T20:00:00Z",
                "end": "2026-02-09T20:30:00Z",
                "status": "started",
                "events": [],
            },
        ]
        result = _build_previous_cycles(cycles, "2026-02-09")
        assert len(result) == 1
        assert result[0]["cycle"] == 1
        assert result[0]["outcome"] == "completed"
        assert result[0]["summary"] == "First attempt"

    def test_all_today_returns_empty(self):
        cycles = [
            {
                "cycle": 1,
                "start": "2026-02-09T20:00:00Z",
                "end": "2026-02-09T20:30:00Z",
                "status": "started",
                "events": [],
            },
        ]
        assert _build_previous_cycles(cycles, "2026-02-09") == []

    def test_no_cycles(self):
        assert _build_previous_cycles([], "2026-02-09") == []

    def test_duration_label(self):
        cycles = [
            {
                "cycle": 1,
                "start": "2026-02-07T10:00:00Z",
                "end": "2026-02-07T10:45:00Z",
                "status": "completed",
                "events": [],
            },
        ]
        result = _build_previous_cycles(cycles, "2026-02-09")
        assert result[0]["duration_label"] == "45 min"

    def test_pr_url_extraction(self):
        cycles = [
            {
                "cycle": 1,
                "start": "2026-02-07T10:00:00Z",
                "end": "2026-02-07T10:30:00Z",
                "status": "completed",
                "events": [
                    _evt("issue.pr_created", artifacts=[
                        {"type": "pull_request", "value": "https://github.com/org/repo/pull/42"},
                    ]),
                ],
            },
        ]
        result = _build_previous_cycles(cycles, "2026-02-09")
        assert result[0]["pr_url"] == "https://github.com/org/repo/pull/42"


# ── Blocked Detail ───────────────────────────────────────────────────────

class TestBlockedDetail:

    def test_not_blocked_returns_none(self):
        ctx = _ctx(flow_stage="queued")
        assert _build_blocked_detail(ctx, []) is None

    def test_no_context_returns_none(self):
        assert _build_blocked_detail(None, []) is None

    def test_blocked_returns_detail(self):
        ctx = _ctx(flow_stage="blocked", labels=("blocked-failed",))
        events = [_evt("session.failed", summary="Process crashed")]
        detail = _build_blocked_detail(ctx, events)
        assert detail is not None
        assert "reason" in detail
        assert "labels" in detail
        assert "blocked-failed" in detail["labels"]

    def test_rework_info_included(self):
        ctx = _ctx(
            flow_stage="blocked",
            labels=("blocked-needs-human",),
            current_rework_cycle=3,
            max_rework_cycles=3,
        )
        detail = _build_blocked_detail(ctx, [])
        assert detail is not None
        assert detail["rework_info"] is not None
        assert "3/3" in detail["rework_info"]
        assert "limit reached" in detail["rework_info"]


# ── Full Builder ─────────────────────────────────────────────────────────

class TestBuildIssueDetailViewModel:

    def test_backward_compat_no_context(self):
        """Builder works without context — all story fields have defaults."""
        result = build_issue_detail_view_model(
            issue_number=42,
            title="Test Issue",
            issue_url="https://github.com/test/repo/issues/42",
            events=[],
            phase_toc=[],
            cycles=[],
        )
        assert result["issue_number"] == 42
        assert "status_explanation" in result
        assert "journey_steps" in result
        assert "journey_cycles" in result
        assert "lifecycle_count" in result
        assert "previous_cycles" in result
        assert "previous_cycles_count" in result
        assert "raw_events_count" in result
        assert "blocked_detail" in result
        # Legacy fields still present
        assert "summary" in result
        assert "cycles" in result
        assert "events" in result

    def test_with_context(self):
        ctx = _ctx(flow_stage="in_progress", active_runtime_minutes=10, active_task_kind="code")
        result = build_issue_detail_view_model(
            issue_number=42,
            title="Test Issue",
            issue_url="https://github.com/test/repo/issues/42",
            events=[_evt("session.started", timestamp="2026-02-09T20:00:00Z")],
            phase_toc=[],
            cycles=[],
            context=ctx,
        )
        assert "Code session in progress (10 min)" == result["status_explanation"]
        assert result["raw_events_count"] == 1
        assert result["blocked_detail"] is None

    def test_journey_cycles_present_in_payload(self):
        """journey_cycles and lifecycle_count appear in builder output."""
        events = [
            _evt("session.started", timestamp="2026-02-09T14:10:00Z", agent="agent:backend"),
            _evt("session.failed", timestamp="2026-02-09T14:12:00Z", summary="No PR"),
        ]
        result = build_issue_detail_view_model(
            issue_number=42,
            title="Test",
            issue_url="https://github.com/test/repo/issues/42",
            events=events,
            phase_toc=[],
            cycles=[],
        )
        assert len(result["journey_cycles"]) == 1
        assert result["lifecycle_count"] == 1


# ── Time Label Formatting ────────────────────────────────────────────────

class TestFormatTimeLabel:

    def test_iso_timestamp(self):
        result = _format_time_label("2026-02-09T20:15:00Z")
        assert result  # Should produce some time string

    def test_empty_string(self):
        assert _format_time_label("") == ""

    def test_none(self):
        assert _format_time_label(None) == ""


# ── Lifecycle Detection ─────────────────────────────────────────────────

class TestLifecycleAnnotation:

    def test_single_lifecycle_single_iteration(self):
        events = [
            _evt("session.started", timestamp="2026-02-09T14:10:00Z"),
            _evt("session.completed", timestamp="2026-02-09T14:30:00Z"),
        ]
        annotated = _annotate_lifecycle(events)
        assert annotated[0][1] == 1  # lifecycle
        assert annotated[0][2] == 1  # iteration
        assert annotated[1][1] == 1
        assert annotated[1][2] == 1

    def test_iteration_increments_on_rework(self):
        events = [
            _evt("session.started", timestamp="2026-02-09T14:10:00Z"),
            _evt("session.completed", timestamp="2026-02-09T14:30:00Z"),
            _evt("review.changes_requested", timestamp="2026-02-09T14:35:00Z"),
            _evt("rework.started", timestamp="2026-02-09T14:40:00Z"),
            _evt("session.completed", timestamp="2026-02-09T15:00:00Z"),
        ]
        annotated = _annotate_lifecycle(events)
        # First iteration
        assert annotated[0][2] == 1  # session.started → iteration 1
        # After rework.started → iteration 2
        assert annotated[3][2] == 2  # rework.started
        assert annotated[4][2] == 2  # session.completed in iteration 2

    def test_multi_lifecycle_block_unblock(self):
        events = [
            _evt("session.started", timestamp="2026-02-09T14:10:00Z"),
            _evt("session.failed", timestamp="2026-02-09T14:12:00Z"),
            _evt("issue.blocked", timestamp="2026-02-09T14:12:01Z"),
            _evt("issue.unblocked", timestamp="2026-02-10T09:00:00Z"),
            _evt("session.started", timestamp="2026-02-10T09:05:00Z"),
            _evt("session.completed", timestamp="2026-02-10T09:30:00Z"),
        ]
        annotated = _annotate_lifecycle(events)
        # First lifecycle
        assert annotated[0][1] == 1  # lifecycle 1
        assert annotated[1][1] == 1
        assert annotated[2][1] == 1  # issue.blocked still lifecycle 1
        # issue.unblocked starts lifecycle 2
        assert annotated[3][1] == 2
        # session.started in new lifecycle → iteration 1
        assert annotated[4][1] == 2
        assert annotated[4][2] == 1

    def test_terminal_then_new_session_starts_new_lifecycle(self):
        """After a terminal event, a new session.started creates a new lifecycle."""
        events = [
            _evt("session.started", timestamp="2026-02-09T14:10:00Z"),
            _evt("issue.completed", timestamp="2026-02-09T14:30:00Z"),
            _evt("session.started", timestamp="2026-02-10T09:00:00Z"),
        ]
        annotated = _annotate_lifecycle(events)
        assert annotated[0][1] == 1
        assert annotated[2][1] == 2  # new lifecycle after terminal

    def test_no_events_returns_empty(self):
        assert _annotate_lifecycle([]) == []


# ── Journey Cycles ──────────────────────────────────────────────────────

class TestJourneyCycles:

    def test_single_cycle(self):
        events = [
            _evt("session.started", timestamp="2026-02-09T14:10:00Z", agent="agent:backend"),
            _evt("session.completed", timestamp="2026-02-09T14:30:00Z"),
        ]
        cycles = _build_journey_cycles(events, "2026-02-09")
        assert len(cycles) == 1
        c = cycles[0]
        assert c["cycle"] == 1
        assert c["lifecycle"] == 1
        assert c["iteration"] == 1
        assert c["agent"] == "backend"
        assert "Completed" in c["outcome"]
        assert len(c["steps"]) == 2

    def test_multiple_iterations_create_separate_cycles(self):
        events = [
            _evt("session.started", timestamp="2026-02-09T14:10:00Z"),
            _evt("session.completed", timestamp="2026-02-09T14:30:00Z"),
            _evt("review.changes_requested", timestamp="2026-02-09T14:35:00Z"),
            _evt("rework.started", timestamp="2026-02-09T14:40:00Z"),
            _evt("session.completed", timestamp="2026-02-09T15:00:00Z"),
            _evt("review.approved", timestamp="2026-02-09T15:05:00Z"),
        ]
        cycles = _build_journey_cycles(events, "2026-02-09")
        assert len(cycles) == 2
        assert cycles[0]["iteration"] == 1
        assert cycles[1]["iteration"] == 2

    def test_cycle_outcome_failed(self):
        events = [
            _evt("session.started", timestamp="2026-02-09T14:10:00Z"),
            _evt("session.failed", timestamp="2026-02-09T14:12:00Z", summary="No PR"),
        ]
        cycles = _build_journey_cycles(events, "2026-02-09")
        assert "Failed" in cycles[0]["outcome"]

    def test_cycle_outcome_timeout(self):
        events = [
            _evt("session.started", timestamp="2026-02-09T14:10:00Z"),
            _evt("session.timeout", timestamp="2026-02-09T14:40:00Z"),
        ]
        cycles = _build_journey_cycles(events, "2026-02-09")
        assert "Timed out" in cycles[0]["outcome"]

    def test_cycle_outcome_changes_requested(self):
        events = [
            _evt("session.started", timestamp="2026-02-09T14:10:00Z"),
            _evt("session.completed", timestamp="2026-02-09T14:30:00Z"),
            _evt("review.changes_requested", timestamp="2026-02-09T14:35:00Z"),
        ]
        cycles = _build_journey_cycles(events, "2026-02-09")
        assert "Changes Requested" in cycles[0]["outcome"]

    def test_cycle_outcome_approved(self):
        events = [
            _evt("session.started", timestamp="2026-02-09T14:10:00Z"),
            _evt("session.completed", timestamp="2026-02-09T14:30:00Z"),
            _evt("review.approved", timestamp="2026-02-09T14:35:00Z"),
        ]
        cycles = _build_journey_cycles(events, "2026-02-09")
        assert "Approved" in cycles[0]["outcome"]

    def test_cycle_outcome_escalated(self):
        events = [
            _evt("session.started", timestamp="2026-02-09T14:10:00Z"),
            _evt("review.escalated", timestamp="2026-02-09T14:35:00Z"),
        ]
        cycles = _build_journey_cycles(events, "2026-02-09")
        assert "Escalated" in cycles[0]["outcome"]

    def test_rework_prefix_on_iteration_gt_1(self):
        events = [
            _evt("session.started", timestamp="2026-02-09T14:10:00Z"),
            _evt("review.changes_requested", timestamp="2026-02-09T14:35:00Z"),
            _evt("rework.started", timestamp="2026-02-09T14:40:00Z"),
            _evt("session.failed", timestamp="2026-02-09T14:42:00Z", summary="Crashed"),
        ]
        cycles = _build_journey_cycles(events, "2026-02-09")
        assert len(cycles) == 2
        # First cycle: iteration 1, no rework prefix
        assert not cycles[0]["outcome"].startswith("Rework")
        # Second cycle: iteration 2, has rework prefix
        assert cycles[1]["outcome"].startswith("Rework")

    def test_expanded_flag_only_last_cycle(self):
        events = [
            _evt("session.started", timestamp="2026-02-09T14:10:00Z"),
            _evt("session.failed", timestamp="2026-02-09T14:12:00Z"),
            _evt("session.started", timestamp="2026-02-09T14:15:00Z"),
            _evt("session.completed", timestamp="2026-02-09T14:30:00Z"),
        ]
        cycles = _build_journey_cycles(events, "2026-02-09")
        assert len(cycles) == 2
        assert cycles[0]["expanded"] is False
        assert cycles[1]["expanded"] is True

    def test_agent_extraction(self):
        events = [
            _evt("session.started", timestamp="2026-02-09T14:10:00Z", agent="agent:frontend"),
            _evt("session.completed", timestamp="2026-02-09T14:30:00Z"),
        ]
        cycles = _build_journey_cycles(events, "2026-02-09")
        assert cycles[0]["agent"] == "frontend"

    def test_agent_without_prefix(self):
        events = [
            _evt("session.started", timestamp="2026-02-09T14:10:00Z", agent="backend"),
            _evt("session.completed", timestamp="2026-02-09T14:30:00Z"),
        ]
        cycles = _build_journey_cycles(events, "2026-02-09")
        assert cycles[0]["agent"] == "backend"

    def test_no_agent(self):
        events = [
            _evt("session.started", timestamp="2026-02-09T14:10:00Z"),
            _evt("session.completed", timestamp="2026-02-09T14:30:00Z"),
        ]
        cycles = _build_journey_cycles(events, "2026-02-09")
        assert cycles[0]["agent"] == ""

    def test_empty_events_returns_empty_cycles(self):
        assert _build_journey_cycles([], "2026-02-09") == []

    def test_noisy_events_filtered_from_steps(self):
        events = [
            _evt("session.started", timestamp="2026-02-09T14:10:00Z"),
            _evt("observation.scan", timestamp="2026-02-09T14:11:00Z"),
            _evt("issue.labels_changed", timestamp="2026-02-09T14:12:00Z"),
            _evt("session.completed", timestamp="2026-02-09T14:30:00Z"),
        ]
        cycles = _build_journey_cycles(events, "2026-02-09")
        assert len(cycles) == 1
        step_events = {s["event"] for s in cycles[0]["steps"]}
        assert "observation.scan" not in step_events
        assert "issue.labels_changed" not in step_events
        assert "session.started" in step_events

    def test_time_label_includes_date(self):
        events = [
            _evt("session.started", timestamp="2026-02-09T14:10:00Z"),
            _evt("session.completed", timestamp="2026-02-09T14:30:00Z"),
        ]
        cycles = _build_journey_cycles(events, "2026-02-09")
        # Cycle header time_label always includes date
        assert "Feb" in cycles[0]["time_label"]

    def test_terminal_cycle_uses_rich_status(self):
        """When issue.blocked is the last event, use rich status explanation."""
        ctx = _ctx(
            flow_stage="blocked",
            labels=("blocked-needs-human",),
            current_rework_cycle=9,
            max_rework_cycles=10,
        )
        events = [
            _evt("rework.started", timestamp="2026-02-11T07:05:00Z"),
            _evt("session.failed", timestamp="2026-02-11T07:07:00Z",
                 summary="Session ended without PR"),
            _evt("issue.blocked", timestamp="2026-02-11T07:07:01Z",
                 summary="Rework limit approaching"),
        ]
        cycles = _build_journey_cycles(events, "2026-02-11", context=ctx)
        assert len(cycles) == 1
        # Should have a rich outcome, not just "Blocked: ..."
        outcome = cycles[0]["outcome"]
        assert outcome  # not empty

    def test_orphan_events_without_session_start(self):
        """Events without session.started get grouped into one fallback cycle."""
        events = [
            _evt("review.started", timestamp="2026-02-09T14:10:00Z"),
            _evt("review.approved", timestamp="2026-02-09T14:30:00Z"),
        ]
        cycles = _build_journey_cycles(events, "2026-02-09")
        # Orphaned events still get rendered so they're not lost
        assert len(cycles) == 1
        assert cycles[0]["lifecycle"] == 0
        assert cycles[0]["iteration"] == 0


# ── Cycle Outcome Derivation ────────────────────────────────────────────

class TestCycleOutcome:

    def test_failed_with_summary(self):
        events = [_evt("session.failed", summary="No PR or status")]
        result = _derive_cycle_outcome(events, 1, None)
        assert "Failed" in result
        assert "No PR" in result

    def test_completed_session(self):
        events = [_evt("session.completed")]
        assert _derive_cycle_outcome(events, 1, None) == "Completed"

    def test_changes_requested(self):
        events = [
            _evt("session.completed"),
            _evt("review.changes_requested"),
        ]
        assert _derive_cycle_outcome(events, 1, None) == "Changes Requested"

    def test_approved(self):
        events = [_evt("review.approved")]
        assert _derive_cycle_outcome(events, 1, None) == "Approved"

    def test_merged(self):
        events = [_evt("review.merged")]
        assert _derive_cycle_outcome(events, 1, None) == "Merged"

    def test_needs_human(self):
        events = [_evt("issue.needs_human", summary="Need API key")]
        result = _derive_cycle_outcome(events, 1, None)
        assert "Needs human" in result
        assert "API key" in result

    def test_rework_prefix(self):
        events = [_evt("session.failed", summary="Crashed")]
        result = _derive_cycle_outcome(events, 2, None)
        assert result.startswith("Rework")
        assert "Failed" in result

    def test_no_rework_prefix_iteration_1(self):
        events = [_evt("session.failed")]
        result = _derive_cycle_outcome(events, 1, None)
        assert not result.startswith("Rework")

    def test_in_progress_no_outcome_events(self):
        events = [_evt("session.started")]
        assert _derive_cycle_outcome(events, 1, None) == "In progress"


# ── Artifact Collection ─────────────────────────────────────────────────

class TestArtifactCollection:

    def test_pr_url_from_pr_created(self):
        events = [
            _evt("issue.pr_created", artifacts=[
                {"type": "pull_request", "value": "https://github.com/org/repo/pull/42"},
            ]),
        ]
        artifacts = _collect_cycle_artifacts(events)
        assert artifacts["pr_url"] == "https://github.com/org/repo/pull/42"
        assert artifacts["pr_number"] == 42

    def test_review_feedback_flag(self):
        events = [
            _evt("review.changes_requested"),
        ]
        artifacts = _collect_cycle_artifacts(events)
        assert artifacts["has_review_feedback"] is True

    def test_no_artifacts(self):
        events = [_evt("session.started")]
        artifacts = _collect_cycle_artifacts(events)
        assert artifacts["pr_url"] is None
        assert artifacts["pr_number"] is None
        assert artifacts["log_url"] is None
        assert artifacts["has_review_feedback"] is False

    def test_log_url_from_transcript_artifact(self):
        events = [
            _evt("session.completed", artifacts=[
                {"type": "transcript", "value": "https://example.com/log/123"},
            ]),
        ]
        artifacts = _collect_cycle_artifacts(events)
        assert artifacts["log_url"] == "https://example.com/log/123"


# ── Signal-based Journey Cycles (rework_cycle present) ──────────────────

class TestSignalJourneyCycles:
    """Tests for the new signal-based cycle grouping using rework_cycle."""

    def test_single_cycle_rework_cycle_none(self):
        """Events with rework_cycle=None go to cycle 1."""
        events = [
            _evt("session.started", timestamp="2026-02-09T14:10:00Z",
                 agent="agent:backend", rework_cycle=None),
            _evt("session.completed", timestamp="2026-02-09T14:30:00Z",
                 rework_cycle=None),
        ]
        cycles = _build_journey_cycles(events, "2026-02-09")
        assert len(cycles) == 1
        assert cycles[0]["cycle"] == 1
        assert cycles[0]["iteration"] == 1  # (rework_cycle or 0) + 1
        assert cycles[0]["agent"] == "backend"
        assert "Completed" in cycles[0]["outcome"]

    def test_rework_cycle_0_and_1_create_two_cycles(self):
        """rework_cycle=0 → cycle 1, rework_cycle=1 → cycle 2."""
        events = [
            _evt("session.started", timestamp="2026-02-09T14:10:00Z",
                 agent="agent:backend", rework_cycle=0),
            _evt("session.completed", timestamp="2026-02-09T14:30:00Z",
                 rework_cycle=0),
            _evt("review.changes_requested", timestamp="2026-02-09T14:35:00Z",
                 rework_cycle=0, reviewer_agent="agent:reviewer"),
            _evt("rework.started", timestamp="2026-02-09T14:40:00Z",
                 rework_cycle=1, agent="agent:backend"),
            _evt("session.completed", timestamp="2026-02-09T15:00:00Z",
                 rework_cycle=1),
            _evt("review.approved", timestamp="2026-02-09T15:05:00Z",
                 rework_cycle=1, reviewer_agent="agent:reviewer"),
        ]
        cycles = _build_journey_cycles(events, "2026-02-09")
        assert len(cycles) == 2
        assert cycles[0]["iteration"] == 1
        assert cycles[1]["iteration"] == 2
        assert cycles[0]["expanded"] is False
        assert cycles[1]["expanded"] is True

    def test_reviewer_agent_extracted(self):
        """reviewer_agent from review.approved appears in cycle data."""
        events = [
            _evt("session.started", timestamp="2026-02-09T14:10:00Z",
                 rework_cycle=None),
            _evt("review.approved", timestamp="2026-02-09T14:35:00Z",
                 rework_cycle=None, reviewer_agent="agent:reviewer-bot"),
        ]
        cycles = _build_journey_cycles(events, "2026-02-09")
        assert len(cycles) == 1
        assert cycles[0]["reviewer_agent"] == "reviewer-bot"

    def test_retry_count_within_cycle(self):
        """Multiple session.started events within one cycle count retries."""
        events = [
            _evt("session.started", timestamp="2026-02-09T14:10:00Z",
                 rework_cycle=None),
            _evt("session.failed", timestamp="2026-02-09T14:12:00Z",
                 rework_cycle=None),
            _evt("session.started", timestamp="2026-02-09T14:15:00Z",
                 rework_cycle=None),
            _evt("session.completed", timestamp="2026-02-09T14:30:00Z",
                 rework_cycle=None),
        ]
        cycles = _build_journey_cycles(events, "2026-02-09")
        # All events have rework_cycle=None → all in cycle 1
        assert len(cycles) == 1
        assert cycles[0]["retry_count"] == 1  # 2 starts - 1

    def test_lifecycle_boundary_detection(self):
        """Terminal events + unblock create new lifecycle in signal path."""
        events = [
            _evt("session.started", timestamp="2026-02-09T14:10:00Z",
                 rework_cycle=None),
            _evt("session.failed", timestamp="2026-02-09T14:12:00Z",
                 rework_cycle=None),
            _evt("issue.blocked", timestamp="2026-02-09T14:12:01Z",
                 rework_cycle=None),
            _evt("issue.unblocked", timestamp="2026-02-10T09:00:00Z",
                 rework_cycle=None),
            _evt("session.started", timestamp="2026-02-10T09:05:00Z",
                 rework_cycle=None),
            _evt("session.completed", timestamp="2026-02-10T09:30:00Z",
                 rework_cycle=None),
        ]
        cycles = _build_journey_cycles(events, "2026-02-10")
        # All events share rework_cycle=None → single cycle, but lifecycle changes
        assert len(cycles) == 1
        # Lifecycle is assigned to the first event; the first event in the cycle
        # is in lifecycle 1 since the terminal event comes LATER
        assert cycles[0]["lifecycle"] == 1

    def test_backward_compat_no_rework_cycle(self):
        """Events without any rework_cycle use legacy annotated path."""
        events = [
            _evt("session.started", timestamp="2026-02-09T14:10:00Z"),
            _evt("session.completed", timestamp="2026-02-09T14:30:00Z"),
            _evt("review.changes_requested", timestamp="2026-02-09T14:35:00Z"),
            _evt("rework.started", timestamp="2026-02-09T14:40:00Z"),
            _evt("session.completed", timestamp="2026-02-09T15:00:00Z"),
        ]
        cycles = _build_journey_cycles(events, "2026-02-09")
        # Legacy path still creates multiple cycles from iteration detection
        assert len(cycles) == 2

    def test_empty_events_signal_path(self):
        """Empty events returns empty cycles even with signal check."""
        assert _build_journey_cycles([], "2026-02-09") == []

    def test_outcome_derivation_per_cycle(self):
        """Each cycle gets its own outcome from its last significant event."""
        events = [
            _evt("session.started", timestamp="2026-02-09T14:10:00Z",
                 rework_cycle=0, agent="agent:backend"),
            _evt("session.failed", timestamp="2026-02-09T14:12:00Z",
                 rework_cycle=0, summary="No PR"),
            _evt("rework.started", timestamp="2026-02-09T14:40:00Z",
                 rework_cycle=1),
            _evt("session.completed", timestamp="2026-02-09T15:00:00Z",
                 rework_cycle=1),
        ]
        cycles = _build_journey_cycles(events, "2026-02-09")
        assert len(cycles) == 2
        assert "Failed" in cycles[0]["outcome"]
        assert "Completed" in cycles[1]["outcome"]

    def test_noisy_events_filtered_in_signal_path(self):
        """Noise events are filtered from signal-based cycles."""
        events = [
            _evt("session.started", timestamp="2026-02-09T14:10:00Z",
                 rework_cycle=None),
            _evt("observation.scan", timestamp="2026-02-09T14:11:00Z",
                 rework_cycle=None),
            _evt("issue.labels_changed", timestamp="2026-02-09T14:12:00Z",
                 rework_cycle=None),
            _evt("session.completed", timestamp="2026-02-09T14:30:00Z",
                 rework_cycle=None),
        ]
        cycles = _build_journey_cycles(events, "2026-02-09")
        assert len(cycles) == 1
        step_events = {s["event"] for s in cycles[0]["steps"]}
        assert "observation.scan" not in step_events
        assert "issue.labels_changed" not in step_events

    def test_mixed_rework_cycles_group_correctly(self):
        """Events with rework_cycle=0, 1, 2 create three cycles."""
        events = [
            _evt("session.started", timestamp="2026-02-09T10:00:00Z",
                 rework_cycle=0, agent="agent:backend"),
            _evt("session.completed", timestamp="2026-02-09T10:30:00Z",
                 rework_cycle=0),
            _evt("review.changes_requested", timestamp="2026-02-09T10:35:00Z",
                 rework_cycle=0),
            _evt("rework.started", timestamp="2026-02-09T11:00:00Z",
                 rework_cycle=1),
            _evt("session.completed", timestamp="2026-02-09T11:30:00Z",
                 rework_cycle=1),
            _evt("review.changes_requested", timestamp="2026-02-09T11:35:00Z",
                 rework_cycle=1),
            _evt("rework.started", timestamp="2026-02-09T12:00:00Z",
                 rework_cycle=2),
            _evt("session.completed", timestamp="2026-02-09T12:30:00Z",
                 rework_cycle=2),
            _evt("review.approved", timestamp="2026-02-09T12:35:00Z",
                 rework_cycle=2),
        ]
        cycles = _build_journey_cycles(events, "2026-02-09")
        assert len(cycles) == 3
        assert cycles[0]["iteration"] == 1
        assert cycles[1]["iteration"] == 2
        assert cycles[2]["iteration"] == 3
        assert "Changes Requested" in cycles[0]["outcome"]
        assert "Changes Requested" in cycles[1]["outcome"]
        assert "Approved" in cycles[2]["outcome"]

    # ── Last-run filter tests (lifecycle filtering) ──

    def test_last_run_filter_excludes_prior_lifecycles_legacy(self):
        """Legacy path: cycles before a block event are excluded by last-run filter."""
        events = [
            # Lifecycle 1: coding → block
            _evt("session.started", timestamp="2026-02-08T10:00:00Z", agent="agent:backend"),
            _evt("session.completed", timestamp="2026-02-08T10:30:00Z"),
            _evt("session.started", timestamp="2026-02-08T11:00:00Z", agent="agent:backend"),
            _evt("session.failed", timestamp="2026-02-08T11:30:00Z"),
            _evt("issue.blocked", timestamp="2026-02-08T11:35:00Z"),
            # Lifecycle 2: resumed after unblock
            _evt("session.started", timestamp="2026-02-09T10:00:00Z", agent="agent:backend"),
            _evt("session.completed", timestamp="2026-02-09T10:30:00Z"),
        ]
        cycles = _build_journey_cycles(events, "2026-02-09")
        # Multiple cycles across 2 lifecycles
        assert len(cycles) >= 2
        lifecycles = {c["lifecycle"] for c in cycles}
        assert len(lifecycles) == 2, f"Expected 2 lifecycles, got {lifecycles}"

        # Filter to last run
        filtered = filter_last_run_cycles(cycles)
        assert len(filtered) < len(cycles), "Last-run filter should exclude earlier cycles"
        assert all(c["lifecycle"] == max(lifecycles) for c in filtered)

    def test_last_run_filter_no_terminal_shows_all(self):
        """Without terminal events, all cycles are lifecycle 1 → filter shows all."""
        events = [
            _evt("session.started", timestamp="2026-02-09T10:00:00Z", agent="agent:backend"),
            _evt("session.completed", timestamp="2026-02-09T10:30:00Z"),
            _evt("session.started", timestamp="2026-02-09T11:00:00Z", agent="agent:backend"),
            _evt("session.completed", timestamp="2026-02-09T11:30:00Z"),
        ]
        cycles = _build_journey_cycles(events, "2026-02-09")
        filtered = filter_last_run_cycles(cycles)
        assert len(filtered) == len(cycles), "No terminal events → all cycles shown"

    def test_last_run_filter_manual_unblock_no_event(self):
        """Block event followed by sessions with no issue.unblocked event (manual unblock).

        Simulates the #4070 scenario: issue was blocked, user removed labels
        manually in GitHub, orchestrator resumed. The issue.blocked event sets
        needs_new_lifecycle, and the next session.started bumps lifecycle even
        without an issue.unblocked event.
        """
        events = [
            # Lifecycle 1: many coding sessions
            _evt("session.started", timestamp="2026-02-08T10:00:00Z", agent="agent:backend"),
            _evt("session.completed", timestamp="2026-02-08T10:30:00Z"),
            _evt("session.started", timestamp="2026-02-08T11:00:00Z", agent="agent:backend"),
            _evt("session.completed", timestamp="2026-02-08T11:30:00Z"),
            _evt("session.started", timestamp="2026-02-08T12:00:00Z", agent="agent:backend"),
            _evt("session.failed", timestamp="2026-02-08T12:30:00Z"),
            _evt("session.started", timestamp="2026-02-08T13:00:00Z", agent="agent:backend"),
            _evt("session.completed", timestamp="2026-02-08T13:30:00Z"),
            # Block — sets needs_new_lifecycle
            _evt("issue.blocked", timestamp="2026-02-08T14:00:00Z"),
            # --- Manual label removal in GitHub, no issue.unblocked event ---
            # Lifecycle 2: resumed sessions
            _evt("session.started", timestamp="2026-02-09T10:00:00Z", agent="agent:backend"),
            _evt("session.completed", timestamp="2026-02-09T10:30:00Z"),
            _evt("session.started", timestamp="2026-02-09T11:00:00Z", agent="agent:backend"),
            _evt("session.completed", timestamp="2026-02-09T11:30:00Z"),
        ]
        cycles = _build_journey_cycles(events, "2026-02-09")
        lifecycles = {c["lifecycle"] for c in cycles}
        assert len(lifecycles) == 2, f"Expected 2 lifecycles, got {lifecycles}"

        filtered = filter_last_run_cycles(cycles)
        # Should only show cycles from lifecycle 2 (after the block)
        assert len(filtered) < len(cycles), "Last-run should exclude pre-block cycles"
        assert all(c["lifecycle"] == max(lifecycles) for c in filtered)
        # Specifically: only the 2 post-block sessions
        assert len(filtered) == 2

    def test_last_run_filter_many_blocks_picks_latest(self):
        """Multiple block→resume cycles: filter shows only the latest lifecycle."""
        events = [
            # Lifecycle 1
            _evt("session.started", timestamp="2026-02-07T10:00:00Z", agent="agent:backend"),
            _evt("session.failed", timestamp="2026-02-07T10:30:00Z"),
            _evt("issue.blocked", timestamp="2026-02-07T10:35:00Z"),
            # Lifecycle 2
            _evt("session.started", timestamp="2026-02-08T10:00:00Z", agent="agent:backend"),
            _evt("session.failed", timestamp="2026-02-08T10:30:00Z"),
            _evt("issue.blocked", timestamp="2026-02-08T10:35:00Z"),
            # Lifecycle 3 (latest)
            _evt("session.started", timestamp="2026-02-09T10:00:00Z", agent="agent:backend"),
            _evt("session.completed", timestamp="2026-02-09T10:30:00Z"),
        ]
        cycles = _build_journey_cycles(events, "2026-02-09")
        lifecycles = {c["lifecycle"] for c in cycles}
        assert len(lifecycles) == 3, f"Expected 3 lifecycles, got {lifecycles}"

        filtered = filter_last_run_cycles(cycles)
        assert all(c["lifecycle"] == 3 for c in filtered)
        assert len(filtered) == 1  # Only the last coding session

    def test_legacy_signal_boundary_creates_lifecycle_split(self):
        """Mixed legacy (no rework_cycle) + signal events get separate lifecycles."""
        events = [
            # Legacy era — no rework_cycle key
            _evt("session.started", timestamp="2026-02-08T10:00:00Z", agent="agent:backend"),
            _evt("session.completed", timestamp="2026-02-08T10:30:00Z"),
            _evt("issue.blocked", timestamp="2026-02-08T10:35:00Z"),
            # Signal era — rework_cycle present
            _evt("session.started", timestamp="2026-02-09T10:00:00Z",
                 rework_cycle=None, agent="agent:backend"),
            _evt("session.completed", timestamp="2026-02-09T10:30:00Z",
                 rework_cycle=None),
        ]
        cycles = _build_journey_cycles(events, "2026-02-09")
        # Legacy events form one cycle, signal events form another
        assert len(cycles) >= 2
        # The lifecycles should differ: legacy lifecycle < signal lifecycle
        legacy_lifecycle = cycles[0]["lifecycle"]
        signal_lifecycle = cycles[-1]["lifecycle"]
        assert signal_lifecycle > legacy_lifecycle

    def test_review_cycle_no_rework_prefix(self):
        """Review-dominated cycles should not get 'Rework →' outcome prefix."""
        events = [
            # Cycle 1: initial coding
            _evt("session.started", timestamp="2026-02-09T10:00:00Z",
                 rework_cycle=0, agent="agent:backend"),
            _evt("session.completed", timestamp="2026-02-09T10:30:00Z",
                 rework_cycle=0),
            # Cycle 2: review (with task=review signal)
            _evt("review.started", timestamp="2026-02-09T11:00:00Z",
                 rework_cycle=1, task="review"),
            _evt("session.completed", timestamp="2026-02-09T11:30:00Z",
                 rework_cycle=1, task="review"),
        ]
        cycles = _build_journey_cycles(events, "2026-02-09")
        assert len(cycles) == 2
        # Cycle 2 is review — should NOT have "Rework →" prefix
        assert "Rework" not in cycles[1]["outcome"]
