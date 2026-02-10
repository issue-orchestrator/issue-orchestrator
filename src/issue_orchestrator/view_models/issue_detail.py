"""Issue detail view model builder — synthesises an 'issue story' for the drawer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


# ---------------------------------------------------------------------------
# Context dataclass — assembled by the web endpoint from orchestrator state
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IssueStoryContext:
    """Snapshot of orchestrator state relevant to one issue's story."""

    flow_stage: str  # queued / in_progress / blocked / awaiting_merge / done
    active_runtime_minutes: int | None = None  # if a session is currently running
    active_task_kind: str | None = None  # code / review / rework / triage
    labels: tuple[str, ...] = ()
    dependency_summary: str | None = None
    current_rework_cycle: int = 0
    max_rework_cycles: int = 10
    pr_url: str | None = None
    pr_number: int | None = None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def build_issue_detail_view_model(
    issue_number: int,
    title: str,
    issue_url: str,
    events: list[dict[str, Any]],
    phase_toc: list[dict[str, Any]],
    cycles: list[dict[str, Any]],
    context: IssueStoryContext | None = None,
) -> dict[str, Any]:
    """Build issue detail payload used by the dashboard drawer."""
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    journey = _build_journey_steps(events, today)
    prev_cycles = _build_previous_cycles(cycles, today)

    return {
        "issue_number": issue_number,
        "title": title,
        "issue_url": issue_url,
        "phase_toc": phase_toc,
        "cycles": cycles,
        "events": events,
        "summary": _summary(events),
        "actions": [
            {"id": "focus", "label": "Focus"},
            {"id": "github", "label": "GitHub ↗", "url": issue_url},
        ],
        # Story fields
        "status_explanation": _build_status_explanation(context, events),
        "journey_steps": journey,
        "previous_cycles": prev_cycles,
        "previous_cycles_count": len(prev_cycles),
        "raw_events_count": len(events),
        "blocked_detail": _build_blocked_detail(context, events),
    }


# ---------------------------------------------------------------------------
# Status explanation
# ---------------------------------------------------------------------------

def _build_status_explanation(
    ctx: IssueStoryContext | None,
    events: list[dict[str, Any]],
) -> str:
    """One-sentence explanation of why the issue is in its current state."""
    if ctx is None:
        return _fallback_explanation(events)

    # Running
    if ctx.active_runtime_minutes is not None:
        kind_label = {
            "code": "Code session",
            "review": "Code review",
            "rework": "Rework session",
            "triage": "Triage review",
        }.get(ctx.active_task_kind or "", "Session")
        return f"{kind_label} in progress ({ctx.active_runtime_minutes} min)"

    # Awaiting merge
    if ctx.flow_stage == "awaiting_merge":
        if ctx.pr_number:
            return f"PR #{ctx.pr_number} approved \u2014 ready to merge"
        return "Awaiting merge"

    # Blocked — map to taxonomy
    if ctx.flow_stage == "blocked":
        return _blocked_explanation(ctx, events)

    # Done
    if ctx.flow_stage == "done":
        last_summary = _last_event_summary(events)
        if last_summary:
            return f"Completed: {last_summary}"
        return "Completed"

    # Queued
    if ctx.flow_stage in ("queued", "queue"):
        if ctx.dependency_summary:
            return f"Queued, waiting on {ctx.dependency_summary}"
        return "Waiting for an available slot"

    # In progress but no active session info (transient)
    if ctx.flow_stage == "in_progress":
        return "In progress"

    return _fallback_explanation(events)


_BLOCKED_EVENT_NAMES = frozenset({
    "session.timeout",
    "session.failed",
    "session.blocked",
    "session.validation_failed",
    "issue.blocked",
    "issue.needs_human",
    "review.changes_requested",
    "review.escalated",
})


def _blocked_explanation(ctx: IssueStoryContext, events: list[dict[str, Any]]) -> str:
    """Produce a specific blocked explanation from labels + events."""
    labels = ctx.labels

    # Find the most recent blocking event for detail
    blocking_event = _find_last_event(events, _BLOCKED_EVENT_NAMES)
    event_name = str(blocking_event.get("event", "")) if blocking_event else ""
    event_summary = str(blocking_event.get("summary", "")) if blocking_event else ""

    # Rework limit exceeded
    if event_name == "review.escalated" or (
        ctx.current_rework_cycle > 0
        and ctx.current_rework_cycle >= ctx.max_rework_cycles
    ):
        return (
            f"Rework limit reached (cycle {ctx.current_rework_cycle}/{ctx.max_rework_cycles})"
            f" \u2014 reviewer keeps requesting changes"
        )

    # Session timeout
    if event_name == "session.timeout":
        return f"Timed out \u2014 agent didn't invoke agent-done"

    # Validation failed
    if event_name == "session.validation_failed" or "validation-failed" in labels:
        reason = event_summary or "project tests did not pass"
        return f"Validation failed \u2014 {reason}"

    # Needs human
    if any(l in labels for l in ("blocked-needs-human", "needs-human")):
        if event_name == "issue.needs_human" and event_summary:
            return f"Needs human input: {event_summary}"
        # Agent didn't invoke agent-done (session exited without completion)
        if event_summary:
            return f"Needs investigation: {event_summary}"
        return "Needs human investigation"

    # Agent self-reported blocked
    if event_name in ("session.blocked", "issue.blocked"):
        reason = event_summary or "unknown reason"
        return f"Agent reported blocked: {reason}"

    # Session failed / crashed
    if event_name == "session.failed":
        reason = event_summary or "session crashed"
        return f"Session failed \u2014 {reason}"

    # Generic blocked label
    if "blocked-failed" in labels:
        if event_summary:
            return f"Failed: {event_summary}"
        return "Session failed"

    # Fallback
    if event_summary:
        return f"Blocked: {event_summary}"
    return "Blocked"


def _fallback_explanation(events: list[dict[str, Any]]) -> str:
    """When no context is available, derive from last event."""
    if not events:
        return "No events recorded"
    last = events[-1]
    status = str(last.get("status") or "")
    summary = str(last.get("summary") or "")
    event_name = str(last.get("event") or "")
    if summary:
        return summary
    if status:
        return f"{event_name}: {status}"
    return event_name or "Unknown"


def _last_event_summary(events: list[dict[str, Any]]) -> str:
    """Return summary from the last event that has one."""
    for event in reversed(events):
        s = event.get("summary")
        if s:
            return str(s)
    return ""


def _find_last_event(
    events: list[dict[str, Any]],
    names: frozenset[str],
) -> dict[str, Any] | None:
    """Find the most recent event whose name is in *names*."""
    for event in reversed(events):
        if str(event.get("event", "")) in names:
            return event
    return None


# ---------------------------------------------------------------------------
# Journey steps
# ---------------------------------------------------------------------------

# Events that are noise in the journey — too low-level for user narrative
_JOURNEY_SKIP_PREFIXES = ("observation.", "cleanup.", "tick.", "orchestrator.", "apply.", "worktree.", "completion.", "stale.", "pr.")
_JOURNEY_SKIP_EVENTS = frozenset({
    "issue.labels_changed",
    "issue.claimed",
    "session.processing_completed",
    "session.no_output",
})

# Event name → narrative template.  {summary} is replaced with event summary.
_NARRATIVE_MAP: dict[str, str] = {
    "session.started": "Code session started",
    "session.completed": "Agent completed{_summary}",
    "session.failed": "Session failed{_summary}",
    "session.timeout": "Session timed out",
    "session.blocked": "Agent blocked{_summary}",
    "session.validation_failed": "Validation failed{_summary}",
    "issue.pr_created": "PR created{_summary}",
    "issue.blocked": "Blocked{_summary}",
    "issue.needs_human": "Needs human input{_summary}",
    "issue.completed": "Issue completed",
    "issue.unblocked": "Unblocked",
    "review.started": "Code review started",
    "review.queued": "Review queued",
    "review.approved": "Reviewer approved{_summary}",
    "review.changes_requested": "Reviewer requested changes{_summary}",
    "review.merged": "PR merged",
    "review.escalated": "Escalated to human review",
    "rework.started": "Rework session started",
    "rework.launching": "Rework session launching",
    "triage.launching": "Triage review launching",
    "validation.started": "Validation started",
    "validation.completed": "Validation passed",
    "session.validation_retry_needed": "Validation failed — retrying",
    "review.comment_added": "Review comment posted{_summary}",
}


def _build_journey_steps(
    events: list[dict[str, Any]],
    today: str,
) -> list[dict[str, Any]]:
    """Build narrative journey steps from all events.

    Returns all steps with a ``day`` field so the UI can group by day
    and offer "last run" vs "all" filtering.
    """
    steps: list[dict[str, Any]] = []

    for event in events:
        event_name = str(event.get("event") or "")

        # Skip noise
        if event_name in _JOURNEY_SKIP_EVENTS:
            continue
        if any(event_name.startswith(p) for p in _JOURNEY_SKIP_PREFIXES):
            continue

        narrative = _event_to_narrative(event)
        ts = event.get("timestamp") or ""
        time_label = _format_time_label(ts, today)
        day = str(ts)[:10] if ts else ""

        steps.append({
            "timestamp": ts,
            "time_label": time_label,
            "day": day,
            "narrative": narrative,
            "status": str(event.get("status") or ""),
            "event": event_name,
        })

    return steps


def _event_to_narrative(event: dict[str, Any]) -> str:
    """Convert a single timeline event to a human-readable narrative sentence."""
    event_name = str(event.get("event") or "")
    summary = str(event.get("summary") or "")
    agent = _format_agent(event)

    template = _NARRATIVE_MAP.get(event_name)
    if template:
        suffix = f": {summary}" if summary else ""
        text = template.replace("{_summary}", suffix)
        if agent:
            text = f"{text} ({agent})"
        return text

    # Fallback: use the step label + summary
    step = str(event.get("step") or event_name or "event")
    label = step.replace("_", " ").replace(".", " ").strip().capitalize()
    parts = [label]
    if summary:
        parts = [f"{label}: {summary}"]
    if agent:
        parts.append(f"({agent})")
    return " ".join(parts)


def _format_agent(event: dict[str, Any]) -> str:
    """Extract a short agent label from event data, e.g. 'backend' from 'agent:backend'."""
    raw = event.get("agent")
    if not raw or not isinstance(raw, str):
        return ""
    # Strip common prefixes like "agent:" for brevity
    if raw.startswith("agent:"):
        return raw[6:]
    return raw




def _format_time_label(timestamp: Any, today: str = "") -> str:
    """Format a timestamp to a label like '8:15 PM' (today) or 'Feb 8, 8:15 PM' (other days)."""
    if not timestamp:
        return ""
    ts = str(timestamp)
    try:
        dt = datetime.fromisoformat(ts)
        time_part = dt.strftime("%-I:%M %p").lstrip("0")
        if today and ts[:10] == today:
            return time_part
        # Include short date for non-today events
        date_part = dt.strftime("%b %-d")
        return f"{date_part}, {time_part}"
    except (ValueError, TypeError):
        # Fallback: show the time portion
        if "T" in ts and len(ts) >= 19:
            return ts[11:16]
        return ts


# ---------------------------------------------------------------------------
# Previous cycles
# ---------------------------------------------------------------------------

def _build_previous_cycles(
    cycles: list[dict[str, Any]],
    today: str,
) -> list[dict[str, Any]]:
    """Build summarised cards for cycles that completed before today."""
    previous: list[dict[str, Any]] = []
    for cycle_data in cycles:
        start = str(cycle_data.get("start") or "")
        # Include cycles that started before today
        if start[:10] >= today:
            continue
        duration = _compute_duration_label(cycle_data.get("start"), cycle_data.get("end"))
        # Extract summary from last event in the cycle
        cycle_events = cycle_data.get("events") or []
        summary = ""
        for evt in reversed(cycle_events):
            s = evt.get("summary")
            if s:
                summary = str(s)
                break

        previous.append({
            "cycle": cycle_data.get("cycle", 0),
            "duration_label": duration,
            "outcome": str(cycle_data.get("status") or "unknown"),
            "pr_url": _extract_pr_url(cycle_events),
            "summary": summary,
        })
    return previous


def _compute_duration_label(start: Any, end: Any) -> str:
    if not start or not end:
        return ""
    try:
        s = datetime.fromisoformat(str(start))
        e = datetime.fromisoformat(str(end))
        delta = e - s
        minutes = int(delta.total_seconds() / 60)
        if minutes < 1:
            return "<1 min"
        if minutes < 60:
            return f"{minutes} min"
        hours = minutes // 60
        remaining = minutes % 60
        if remaining == 0:
            return f"{hours}h"
        return f"{hours}h {remaining}m"
    except (ValueError, TypeError):
        return ""


def _extract_pr_url(events: list[dict[str, Any]]) -> str | None:
    """Find a PR URL from event artifacts."""
    for event in reversed(events):
        for artifact in event.get("artifacts") or []:
            if not isinstance(artifact, dict):
                continue
            value = str(artifact.get("value") or "")
            if "/pull/" in value:
                return value
    return None


# ---------------------------------------------------------------------------
# Blocked detail
# ---------------------------------------------------------------------------

def _build_blocked_detail(
    ctx: IssueStoryContext | None,
    events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Structured blocked detail when the issue is blocked."""
    if ctx is None or ctx.flow_stage != "blocked":
        return None

    blocking_event = _find_last_event(events, _BLOCKED_EVENT_NAMES)
    event_summary = str(blocking_event.get("summary", "")) if blocking_event else ""

    rework_info: str | None = None
    if ctx.current_rework_cycle > 0:
        rework_info = f"Rework cycle {ctx.current_rework_cycle}/{ctx.max_rework_cycles}"
        if ctx.current_rework_cycle >= ctx.max_rework_cycles:
            rework_info += " — limit reached"

    blocking_labels = [l for l in ctx.labels if _is_blocking_label(l)]

    return {
        "reason": _build_status_explanation(ctx, events),
        "labels": blocking_labels,
        "rework_info": rework_info,
        "event_summary": event_summary,
    }


def _is_blocking_label(label: str) -> bool:
    """Check if a label is a blocking label."""
    if label == "blocked":
        return True
    if label.startswith("blocked-") or label.startswith("blocked:"):
        return True
    if label in ("needs-human", "failed"):
        return True
    return False


# ---------------------------------------------------------------------------
# Legacy summary (kept for backward compat in payload)
# ---------------------------------------------------------------------------

def _summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    if not events:
        return {"status": "unknown", "last_event": "", "event_count": 0}
    last = events[-1]
    return {
        "status": str(last.get("status") or "unknown"),
        "last_event": str(last.get("event") or ""),
        "event_count": len(events),
    }
