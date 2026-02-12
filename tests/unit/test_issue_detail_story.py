"""Tests for the issue story synthesis in issue_detail.py."""

from __future__ import annotations

import pytest

from issue_orchestrator.view_models.issue_detail import (
    IssueStoryContext,
    build_issue_detail_view_model,
    _build_blocked_detail,
    _build_journey_steps,
    _build_previous_cycles,
    _build_status_explanation,
    _event_to_narrative,
    _format_time_label,
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


# ── Time Label Formatting ────────────────────────────────────────────────

class TestFormatTimeLabel:

    def test_iso_timestamp(self):
        result = _format_time_label("2026-02-09T20:15:00Z")
        assert result  # Should produce some time string

    def test_empty_string(self):
        assert _format_time_label("") == ""

    def test_none(self):
        assert _format_time_label(None) == ""
