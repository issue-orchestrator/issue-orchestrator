"""Issue detail view model builder — synthesises an 'issue story' for the drawer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from ..domain.event_taxonomy import (
    REVIEW_ROUND_CLOSE_EVENT_NAMES,
    REVIEW_START_CLUSTER_EVENT_NAMES,
    REVIEW_STORY_MECHANIC_EVENT_NAMES,
    REVIEW_TERMINAL_CLUSTER_EVENT_NAMES,
    EventIntent,
    infer_event_intent,
)
from ..domain.logical_run_projection import (
    LogicalRunProjector,
    group_events_by_logical_cycle,
)
from ..events import EventName


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
    max_rework_cycles: int = 5
    pr_url: str | None = None
    pr_number: int | None = None


_logical_run_projector = LogicalRunProjector()


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
    view: str = "user",
) -> dict[str, Any]:
    """Build issue detail payload used by the dashboard drawer."""
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    filtered = _filter_events_by_view(events, view)
    story_events = _story_projection_events(filtered, view)
    timeline_steps = _build_journey_steps(story_events, today)
    previous_runs = _build_previous_cycles(cycles, today)
    run_cycles = _build_journey_cycles(story_events, today, context)
    runs = _build_runs(run_cycles)

    return {
        "issue_number": issue_number,
        "title": title,
        "issue_url": issue_url,
        "phase_toc": phase_toc,
        "cycles": cycles,
        "events": filtered,
        "summary": _summary(filtered),
        "actions": [
            {"id": "focus", "label": "Focus"},
            {"id": "github", "label": "GitHub ↗", "url": issue_url},
        ],
        # Story fields
        "view": view,
        "status_explanation": _build_status_explanation(context, filtered),
        "timeline_steps": timeline_steps,
        "runs": runs,
        "run_count": len(runs),
        "previous_runs": previous_runs,
        "previous_runs_count": len(previous_runs),
        "raw_events_count": len(events),
        "blocked_detail": _build_blocked_detail(context, filtered),
    }


# ---------------------------------------------------------------------------
# View filtering
# ---------------------------------------------------------------------------

def _filter_events_by_view(events: list[dict[str, Any]], view: str) -> list[dict[str, Any]]:
    """Filter events to those visible in the requested view.

    Events with a ``views`` tag are included only if ``view`` is in the list.
    Events without a ``views`` tag (pre-registry data) are included in all views.
    """
    result: list[dict[str, Any]] = []
    for evt in events:
        event_name = str(evt.get("source_event") or evt.get("event") or "")
        views = evt.get("views")
        if event_name == "publish.failed" and view in {"user", "ops", "debug"}:
            # Compatibility for records written before publish.failed was
            # promoted from debug-only to user-visible.
            result.append(evt)
        elif views is None:
            # Legacy event without view tags — include everywhere
            result.append(evt)
        elif view in views:
            result.append(evt)
    return result


# Cluster definitions live in domain/event_taxonomy.py as the single source
# of truth so tests and view-model collapsers stay in lockstep.
_REVIEW_START_CLUSTER_EVENTS = REVIEW_START_CLUSTER_EVENT_NAMES
_REVIEW_TERMINAL_CLUSTER_EVENTS = REVIEW_TERMINAL_CLUSTER_EVENT_NAMES
_REVIEW_STORY_MECHANIC_EVENTS = REVIEW_STORY_MECHANIC_EVENT_NAMES
_REVIEW_STORY_SEGMENT_TERMINAL_EVENTS = REVIEW_TERMINAL_CLUSTER_EVENT_NAMES | frozenset({
    EventName.REVIEW_EXCHANGE_FAILED.value,
    EventName.REVIEW_EXCHANGE_ROLE_TIMEOUT.value,
})


def _story_projection_events(events: list[dict[str, Any]], view: str) -> list[dict[str, Any]]:
    """Apply user-story-specific event collapsing without affecting ops/debug views."""
    if view != "user":
        return events
    return _collapse_completed_review_segments(
        _collapse_review_terminal_clusters(
            _drop_outer_coding_completion_during_review_rounds(
                _collapse_review_start_clusters(events)
            )
        )
    )


def _collapse_review_start_clusters(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse redundant initial review-start events into one story row.

    Keeps the most useful event in the cluster, preferring ``review_exchange.round_started``
    because it is emitted only after the reviewer prompt exists and its actions/transcript are
    meaningful immediately.
    """
    collapsed: list[dict[str, Any]] = []
    idx = 0
    while idx < len(events):
        event = events[idx]
        event_name = _canonical_event_name(event)
        if event_name not in _REVIEW_START_CLUSTER_EVENTS:
            collapsed.append(event)
            idx += 1
            continue

        cluster = [event]
        next_idx = idx + 1
        while next_idx < len(events):
            candidate = events[next_idx]
            candidate_name = _canonical_event_name(candidate)
            if candidate_name not in _REVIEW_START_CLUSTER_EVENTS:
                break
            cluster.append(candidate)
            next_idx += 1

        if (
            len(cluster) == 1
            and _canonical_event_name(cluster[0]) == EventName.REVIEW_EXCHANGE_ROUND_STARTED.value
        ):
            collapsed.append(cluster[0])
            idx = next_idx
            continue

        chosen = _preferred_review_start_story_event(cluster)
        projected = dict(chosen)
        projected["narrative"] = "Code review started"
        collapsed.append(projected)
        idx = next_idx

    return collapsed


def _preferred_review_start_story_event(cluster: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick the best single event to represent a review-start cluster in Story."""
    for preferred_name in (
        EventName.REVIEW_EXCHANGE_ROUND_STARTED.value,
        EventName.REVIEW_EXCHANGE_STARTED.value,
        EventName.REVIEW_STARTED.value,
    ):
        for event in reversed(cluster):
            if _canonical_event_name(event) == preferred_name:
                return event
    return cluster[-1]


def _collapse_review_terminal_clusters(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse redundant review-end mechanics into one terminal review row in Story."""
    collapsed: list[dict[str, Any]] = []
    idx = 0
    while idx < len(events):
        event = events[idx]
        event_name = _canonical_event_name(event)
        if event_name not in _REVIEW_TERMINAL_CLUSTER_EVENTS:
            collapsed.append(event)
            idx += 1
            continue

        cluster = [event]
        next_idx = idx + 1
        while next_idx < len(events):
            candidate = events[next_idx]
            candidate_name = _canonical_event_name(candidate)
            if candidate_name not in _REVIEW_TERMINAL_CLUSTER_EVENTS:
                break
            cluster.append(candidate)
            next_idx += 1

        terminal = _preferred_review_terminal_story_event(cluster)
        if terminal is None:
            collapsed.extend(cluster)
        else:
            collapsed.append(terminal if len(cluster) == 1 else dict(terminal))
        idx = next_idx

    return collapsed


def _preferred_review_terminal_story_event(cluster: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick the single user-facing event for a terminal review cluster when possible."""
    for preferred_name in (
        EventName.REVIEW_APPROVED.value,
        EventName.REVIEW_CHANGES_REQUESTED.value,
    ):
        for event in reversed(cluster):
            if _canonical_event_name(event) == preferred_name:
                return event
    return None


def _collapse_completed_review_segments(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Represent each Story review segment with either its start or terminal row."""
    collapsed: list[dict[str, Any]] = []
    pending_start_index: int | None = None

    for event in events:
        event_name = _canonical_event_name(event)
        if event_name in _REVIEW_STORY_MECHANIC_EVENTS:
            continue

        if event_name in _REVIEW_START_CLUSTER_EVENTS:
            collapsed.append(event)
            pending_start_index = len(collapsed) - 1
            continue

        if event_name in _REVIEW_STORY_SEGMENT_TERMINAL_EVENTS:
            if pending_start_index is not None:
                start_event = collapsed[pending_start_index]
                del collapsed[pending_start_index]
                pending_start_index = None
                collapsed.append(_project_review_terminal_story_event(event, start_event))
            else:
                collapsed.append(_project_review_terminal_story_event(event))
            continue

        if _event_starts_new_story_work_segment(event):
            pending_start_index = None
        collapsed.append(event)

    return collapsed


def _project_review_terminal_story_event(
    event: dict[str, Any],
    start_event: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Use a plain Story label for exchange-only terminal events."""
    projected = dict(event)
    if start_event is not None:
        _fill_missing_review_session_context(projected, start_event)

    event_name = _canonical_event_name(event)
    if event_name not in {
        EventName.REVIEW_EXCHANGE_ROUND_COMPLETED.value,
        EventName.REVIEW_EXCHANGE_COMPLETED.value,
    }:
        return projected

    summary = str(projected.get("summary") or "").strip().lower()
    if "changes_requested" in summary:
        projected["narrative"] = "Reviewer requested changes"
    elif "ok" in summary or event_name == EventName.REVIEW_EXCHANGE_COMPLETED.value:
        projected["narrative"] = "Reviewed"
    return projected


def _fill_missing_review_session_context(
    terminal_event: dict[str, Any],
    start_event: dict[str, Any],
) -> None:
    """Carry reviewer session identity when Story drops the review-start row."""
    for key in (
        "run_id",
        "run_dir",
        "task",
        "agent",
        "rework_cycle",
        "round_index",
        "logical_run",
        "logical_cycle",
        "logical_phase",
    ):
        if terminal_event.get(key) is None and start_event.get(key) is not None:
            terminal_event[key] = start_event[key]


def _event_starts_new_story_work_segment(event: dict[str, Any]) -> bool:
    logical_phase = str(event.get("logical_phase") or "").strip().lower()
    if logical_phase in {"coding", "rework"}:
        return True

    intent_raw = event.get("event_intent")
    intent = (
        intent_raw
        if isinstance(intent_raw, str) and intent_raw
        else infer_event_intent(
            event_name=_canonical_event_name(event),
            task=str(event.get("task") or ""),
        ).value
    )
    return intent in {EventIntent.CODING.value, EventIntent.REWORK.value}


def _drop_outer_coding_completion_during_review_rounds(
    events: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Hide redundant outer coding completion rows while a review round is active."""
    projected: list[dict[str, Any]] = []
    review_round_open = False
    for event in events:
        event_name = str(event.get("event") or "")
        source_event = _canonical_event_name(event)
        if source_event == EventName.REVIEW_EXCHANGE_ROUND_STARTED.value:
            review_round_open = True
            projected.append(event)
            continue
        if review_round_open and _is_outer_coding_completion_event(event_name, source_event):
            continue
        projected.append(event)
        if source_event in REVIEW_ROUND_CLOSE_EVENT_NAMES:
            review_round_open = False
    return projected


def _is_outer_coding_completion_event(event_name: str, source_event: str) -> bool:
    return (
        event_name == EventName.AGENT_CODING_COMPLETED.value
        or source_event == EventName.OBSERVATION_COMPLETION_DETECTED.value
    )


def _canonical_event_name(event: dict[str, Any]) -> str:
    return str(event.get("source_event") or event.get("event") or "")


# ---------------------------------------------------------------------------
# Status explanation
# ---------------------------------------------------------------------------

def _build_status_explanation(  # noqa: C901 — maps flow stages to status explanations
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
    "publish.failed",
    "review.changes_requested",
    "review.escalated",
})


def _blocked_explanation(ctx: IssueStoryContext, events: list[dict[str, Any]]) -> str:  # noqa: C901 — maps blocking conditions to explanations
    """Produce a specific blocked explanation from labels + events."""
    labels = ctx.labels

    # Find the most recent blocking event for detail
    blocking_event = _find_last_event(events, _BLOCKED_EVENT_NAMES)
    event_name = str(blocking_event.get("source_event") or blocking_event.get("event") or "") if blocking_event else ""
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
        return f"Timed out \u2014 agent didn't invoke completion command"

    # Validation failed
    if event_name == "session.validation_failed" or "validation-failed" in labels:
        reason = event_summary or "project tests did not pass"
        return f"Validation failed \u2014 {reason}"

    # Needs human
    if any(l in labels for l in ("blocked-needs-human", "needs-human")):
        if event_name == "issue.needs_human" and event_summary:
            return f"Needs human input: {event_summary}"
        # Agent didn't invoke completion command (session exited without completion)
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

    # Publish failure or generic blocked-failed
    if "publish-failed" in labels:
        return f"Publishing failed: {event_summary}" if event_summary else "Publishing failed — could not push or create PR"
    if "blocked-failed" in labels:
        return f"Failed: {event_summary}" if event_summary else "Session failed"

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
    """Find the most recent event whose name is in *names*.

    Prefers ``source_event`` (internal canonical name) so fan-out renames
    don't break outcome/blocked detection.
    """
    for event in reversed(events):
        canonical = str(event.get("source_event") or event.get("event") or "")
        if canonical in names:
            return event
    return None


# ---------------------------------------------------------------------------
# Journey steps
# ---------------------------------------------------------------------------

# Events that are noise in the journey — too low-level for user narrative
_JOURNEY_SKIP_PREFIXES = ("observation.", "cleanup.", "tick.", "orchestrator.", "apply.", "worktree.", "completion.", "stale.", "pr.", "claim.")
_JOURNEY_SKIP_EVENTS = frozenset({
    "issue.labels_changed",
    "issue.claimed",
    "session.processing_completed",
    "session.no_completion_record",
    "session.no_output",
})

def _should_skip_event(event_name: str) -> bool:
    """Check if an event should be skipped (legacy path for untagged events)."""
    if event_name in _JOURNEY_SKIP_EVENTS:
        return True
    return any(event_name.startswith(p) for p in _JOURNEY_SKIP_PREFIXES)

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
    "review.rework_started": "Coder addressing review feedback{_summary}",
    "review.rework_completed": "Coder finished review rework{_summary}",
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
    "session.validation_retry_needed": "Validation failed — retrying{_summary}",
    "publish.failed": "Publish failed{_summary}",
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

        # Legacy events without view tags: apply old skip rules
        if event.get("views") is None and _should_skip_event(event_name):
            continue

        narrative = _event_to_narrative(event)
        ts = event.get("timestamp") or ""
        time_label = _format_time_label(ts, today)
        day = str(ts)[:10] if ts else ""

        detail = event.get("detail")
        step_dict: dict[str, Any] = {
            "timestamp": ts,
            "time_label": time_label,
            "day": day,
            "narrative": narrative,
            "status": str(event.get("status") or ""),
            "event": event_name,
        }
        if detail:
            step_dict["detail"] = str(detail)
        steps.append(step_dict)

    return steps


def _event_to_narrative(event: dict[str, Any]) -> str:
    """Convert a single timeline event to a human-readable narrative sentence."""
    event_name = str(event.get("event") or "")
    summary = str(event.get("summary") or "")
    agent = _format_agent(event)

    # Prefer narrative from view registry (set at write time)
    stored_narrative = event.get("narrative")
    if isinstance(stored_narrative, str) and stored_narrative:
        # Suppress trivial summaries that the narrative already implies
        useful_summary = summary if summary and not _is_trivial_summary(summary) else ""
        suffix = f": {useful_summary}" if useful_summary else ""
        text = f"{stored_narrative}{suffix}"
        if agent:
            text = f"{text} ({agent})"
        return text

    # Fall back to static narrative map
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


_TRIVIAL_SUMMARIES = frozenset({
    "completed", "started", "ok", "passed", "failed",
})


def _is_trivial_summary(summary: str) -> bool:
    """Return True for single-word status values that add no information."""
    return summary.strip().lower() in _TRIVIAL_SUMMARIES


def _format_agent(event: dict[str, Any]) -> str:
    """Extract a short agent label from event data, e.g. 'backend' from 'agent:backend'."""
    raw = event.get("agent")
    if not raw or not isinstance(raw, str):
        return ""
    # Strip common prefixes like "agent:" for brevity
    if raw.startswith("agent:"):
        return raw[6:]
    return raw



# ---------------------------------------------------------------------------
# Journey cycles — collapsible lifecycle groups
# ---------------------------------------------------------------------------

# Outcome derivation: last significant event → outcome label
_OUTCOME_EVENTS = frozenset({
    "session.failed",
    "session.timeout",
    "session.blocked",
    "session.completed",
    "review_exchange.round_completed",
    "review.changes_requested",
    "review.approved",
    "review.escalated",
    "review.merged",
    "issue.blocked",
    "issue.needs_human",
    "publish.failed",
    "issue.completed",
})


def filter_last_run_cycles(cycles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter cycles to only those from the latest lifecycle.

    Mirrors the frontend ``Latest run`` intent (logical run), so backend and
    UI share the same semantics.

    Returns all cycles from max lifecycle when available; falls back to run_id
    grouping only for legacy payloads without lifecycle annotations.
    """
    return _logical_run_projector.filter_last_run_cycles(cycles)


def _build_runs(cycles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group cycle rows into logical runs for UI rendering.

    Logical runs are lifecycle-based (coding + review + rework chain),
    not individual physical session launches.
    """
    runs = _logical_run_projector.build_runs(cycles)
    for run in runs:
        run_number = run.get("run_number") or "?"
        reset_from_scratch = any(
            bool(cycle.get("reset_from_scratch"))
            for cycle in run.get("cycles", [])
            if isinstance(cycle, dict)
        )
        run["reset_from_scratch"] = reset_from_scratch
        run["run_label"] = (
            f"Run {run_number} (scratch retry)"
            if reset_from_scratch
            else f"Run {run_number}"
        )
    if not runs:
        return runs

    latest = runs[-1]
    if not _run_contains_review_events(latest):
        latest["outcome"] = _coerce_non_review_latest_outcome(str(latest.get("outcome") or ""))
        for cycle in latest.get("cycles", []):
            cycle["outcome"] = _coerce_non_review_latest_outcome(str(cycle.get("outcome") or ""))
    return runs


def _run_contains_review_events(run: dict[str, Any]) -> bool:
    for cycle in run.get("cycles", []):
        steps = cycle.get("steps")
        if not isinstance(steps, list):
            continue
        if any(
            str(step.get("event") or "").startswith(("review.", "review_exchange."))
            for step in steps
            if isinstance(step, dict)
        ):
            return True
    return False


def _coerce_non_review_latest_outcome(outcome: str) -> str:
    lower = outcome.strip().lower()
    if "approved" in lower or "completed" in lower or "awaiting merge" in lower:
        if lower.startswith("rework"):
            return "Rework \u2192 In progress"
        return "In progress"
    return outcome


def _build_journey_cycles(
    events: list[dict[str, Any]],
    today: str,
    context: IssueStoryContext | None = None,
) -> list[dict[str, Any]]:
    """Build collapsible cycle groups from backend-owned logical semantics."""
    if not _has_logical_semantics(events):
        return []
    return _build_semantic_journey_cycles(events, today, context)


def _has_logical_semantics(events: list[dict[str, Any]]) -> bool:
    if not events:
        return False
    return all(
        isinstance(e.get("logical_run"), int)
        and isinstance(e.get("logical_cycle"), int)
        and isinstance(e.get("logical_phase"), str)
        for e in events
    )


def _build_semantic_journey_cycles(
    events: list[dict[str, Any]],
    today: str,
    context: IssueStoryContext | None = None,
) -> list[dict[str, Any]]:
    """Group events using backend-owned logical semantics."""
    cycles: list[dict[str, Any]] = []
    grouped_events = [
        event
        for event in events
        if event.get("views") is not None
        or not _should_skip_event(str(event.get("event") or ""))
    ]
    for idx, group in enumerate(group_events_by_logical_cycle(grouped_events), start=1):
        raw_events = list(group.events)
        cycle = _finalize_cycle_from_events(
            idx,
            group.logical_run,
            group.logical_cycle,
            raw_events,
            today,
            context,
        )
        cycles.append(cycle)

    if cycles:
        cycles[-1]["expanded"] = True

    return _annotate_cycle_in_run(cycles)


def _finalize_cycle_from_events(
    cycle_number: int,
    lifecycle: int,
    iteration: int,
    raw_events: list[dict[str, Any]],
    today: str,
    context: IssueStoryContext | None,
) -> dict[str, Any]:
    """Build one cycle dict from plain events (signal-based path)."""
    # Agent label: from the first event with an agent
    agent = ""
    for evt in raw_events:
        a = _format_agent(evt)
        if a:
            agent = a
            break

    # Reviewer agent: from review outcome events
    reviewer_agent = ""
    for evt in raw_events:
        ra = evt.get("reviewer_agent")
        if ra and isinstance(ra, str):
            reviewer_agent = ra.removeprefix("agent:")
            break

    # Retry count: number of session.started events minus 1 (first start is not a retry)
    # Use source_event so fan-out renames don't break the count.
    session_starts = sum(
        1 for evt in raw_events
        if str(evt.get("source_event") or evt.get("event") or "") in (
            "session.started", "rework.started", "rework.launching",
        )
    )
    retry_count = max(0, session_starts - 1)

    # Time label from first event
    first_ts = raw_events[0].get("timestamp") or "" if raw_events else ""
    time_label = _format_date_time_label(first_ts)
    run_id = next(
        (str(evt.get("run_id")) for evt in raw_events if evt.get("run_id")),
        None,
    )
    session_run_ids = [
        run for run in dict.fromkeys(
            str(evt.get("run_id"))
            for evt in raw_events
            if evt.get("run_id")
        )
    ]

    # Outcome from last significant event
    outcome = _derive_cycle_outcome(raw_events, iteration, context)
    reset_from_scratch = any(
        bool(evt.get("from_scratch") or evt.get("reset_from_scratch"))
        for evt in raw_events
    )

    # Artifacts
    artifacts = _collect_cycle_artifacts(raw_events)

    # Nested journey steps + phase groups
    steps = [_build_cycle_step(evt, today) for evt in raw_events]
    phase_groups = _build_phase_groups(raw_events, steps, iteration)

    return {
        "cycle": cycle_number,
        "lifecycle": lifecycle,
        "iteration": iteration,
        "run_id": run_id,
        "timestamp": first_ts,
        "session_run_ids": session_run_ids,
        "agent": agent,
        "reviewer_agent": reviewer_agent,
        "retry_count": retry_count,
        "reset_from_scratch": reset_from_scratch,
        "cycle_label": f"Cycle {iteration} (scratch)" if reset_from_scratch else f"Cycle {iteration}",
        "outcome": outcome,
        "time_label": time_label,
        "expanded": False,
        "artifacts": artifacts,
        "steps": steps,
        "phase_groups": phase_groups,
        "validation": _cycle_validation_summary(raw_events),
    }


_CYCLE_VALIDATION_PASSED_EVENTS = frozenset(
    {"validation.passed", "session.validation_passed"}
)
_CYCLE_VALIDATION_FAILED_EVENTS = frozenset(
    {
        "validation.failed",
        "session.validation_failed",
        "session.validation_retry_needed",
    }
)


def _cycle_validation_summary(raw_events: list[dict[str, Any]]) -> dict[str, Any]:
    """Distill validation evidence for the cycle header badge.

    The journey renders a per-cycle badge (Validated / Failed / Not validated)
    that opens the existing validation-failure dialog for that cycle's
    run_dir. We only need the outcome kind and a run_dir pointer here —
    full JUnit cases load lazily inside the dialog endpoint.
    """
    for evt in reversed(raw_events):
        name = str(evt.get("event") or evt.get("source_event") or "").lower()
        if name in _CYCLE_VALIDATION_FAILED_EVENTS:
            return {
                "kind": "failed",
                "run_dir": _optional_str(evt.get("run_dir")),
            }
        if name in _CYCLE_VALIDATION_PASSED_EVENTS:
            return {
                "kind": "passed",
                "run_dir": _optional_str(evt.get("run_dir")),
            }
    return {"kind": "not_validated", "run_dir": None}


def _optional_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _annotate_cycle_in_run(cycles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Annotate cycles with a run-local sequence number.

    Uses ``run_id`` when available. Falls back to lifecycle grouping when
    ``run_id`` is absent (legacy timelines).
    """
    return _logical_run_projector.annotate_cycle_in_run(cycles)


def _build_cycle_step(evt: dict[str, Any], today: str) -> dict[str, Any]:
    """Build one journey step entry from a timeline event."""
    ts = evt.get("timestamp") or ""
    actions = evt.get("actions")
    step_dict: dict[str, Any] = {
        "timestamp": ts,
        "time_label": _format_time_label(ts, today),
        "day": str(ts)[:10] if ts else "",
        "narrative": _event_to_narrative(evt),
        "status": str(evt.get("status") or ""),
        "event": str(evt.get("event") or ""),
    }
    detail = _step_detail_text(evt)
    if detail:
        step_dict["detail"] = detail
    if actions:
        step_dict["actions"] = actions
    return step_dict


def _step_detail_text(evt: dict[str, Any]) -> str | None:
    # Only surface the event's own detail text.  Artifact-resolution errors
    # (actions_error / show_actions_error) are already accessible via the
    # "What is missing?" action button and should not pollute the narrative.
    detail = evt.get("detail")
    if not detail:
        return None
    return str(detail)


def _build_phase_groups(
    raw_events: list[dict[str, Any]],
    steps: list[dict[str, Any]],
    iteration: int,
) -> list[dict[str, Any]]:
    """Group cycle steps into user-facing phase buckets."""
    groups: list[dict[str, Any]] = []
    current_key: str | None = None

    for evt, step in zip(raw_events, steps, strict=False):
        phase_key = _phase_key_for_event(evt, iteration)
        if current_key != phase_key:
            groups.append({
                "key": phase_key,
                "label": _phase_label_for_key(phase_key),
                "steps": [],
            })
            current_key = phase_key
        groups[-1]["steps"].append(step)

    return groups


def _phase_key_for_event(evt: dict[str, Any], iteration: int) -> str:
    """Map an event to a phase key for cycle rendering."""
    logical_phase = str(evt.get("logical_phase") or "").strip().lower()
    if logical_phase in {"coding", "review", "rework", "orchestrator"}:
        return logical_phase

    intent_raw = evt.get("event_intent")
    intent = (
        intent_raw
        if isinstance(intent_raw, str) and intent_raw
        else infer_event_intent(
            event_name=str(evt.get("source_event") or evt.get("event") or ""),
            task=str(evt.get("task") or ""),
        ).value
    )

    if intent == EventIntent.ORCHESTRATOR.value:
        return "orchestrator"
    if intent == EventIntent.REVIEW.value:
        return "review"
    if intent == EventIntent.REWORK.value:
        return "rework"
    if intent == EventIntent.CODING.value:
        return "rework" if iteration > 1 else "coding"

    return "rework" if iteration > 1 else "coding"


def _phase_label_for_key(key: str) -> str:
    if key == "review":
        return "Review"
    if key == "orchestrator":
        return "Orchestrator"
    if key == "rework":
        return "Rework"
    return "Coding"


def _derive_cycle_outcome(
    events: list[dict[str, Any]],
    iteration: int,
    context: IssueStoryContext | None,
) -> str:
    """Derive outcome label from the last significant event in the cycle."""
    # Find the last outcome-relevant event (use source_event for canonical matching)
    last_outcome_event: dict[str, Any] | None = None
    for evt in reversed(events):
        canonical = str(evt.get("source_event") or evt.get("event") or "")
        if canonical in _OUTCOME_EVENTS:
            last_outcome_event = evt
            break

    if last_outcome_event is None:
        return "In progress"

    event_name = str(last_outcome_event.get("source_event") or last_outcome_event.get("event") or "")
    summary = str(last_outcome_event.get("summary") or "")

    label = _outcome_label(event_name, summary, context)

    # Prefix with "Rework → " when iteration > 1, but not for review-dominated cycles
    if iteration > 1:
        is_review_cycle = any(
            str(e.get("event_intent") or "") == EventIntent.REVIEW.value
            for e in events
        )
        if not is_review_cycle:
            label = f"Rework \u2192 {label}"

    return label


def _outcome_label(  # noqa: C901 — event-type dispatch for outcome labeling
    event_name: str,
    summary: str,
    context: IssueStoryContext | None,
) -> str:
    """Map a single event name to its outcome label text."""
    round_completed_label = _round_completed_outcome_label(event_name, summary)
    if round_completed_label is not None:
        return round_completed_label

    session_label = _session_outcome_label(event_name, summary)
    if session_label is not None:
        return session_label

    direct_label = _DIRECT_OUTCOME_LABELS.get(event_name)
    if direct_label is not None:
        return direct_label

    blocked_label = _issue_blocked_outcome_label(event_name, summary, context)
    if blocked_label is not None:
        return blocked_label

    return summary or event_name


def _round_completed_outcome_label(event_name: str, summary: str) -> str | None:
    """Map round-completion summaries into cycle outcome labels when applicable."""
    if event_name != "review_exchange.round_completed":
        return None
    if not summary:
        return None
    summary_lower = summary.strip().lower()
    if "changes_requested" in summary_lower:
        return "Changes Requested"
    if "ok" in summary_lower:
        return "Approved"
    return None


def _session_outcome_label(event_name: str, summary: str) -> str | None:
    if event_name == "session.failed":
        return f"Failed{_duration_suffix(summary)}"
    if event_name == "session.timeout":
        return f"Timed out{_duration_suffix(summary)}"
    if event_name == "session.blocked":
        reason = summary or "unknown"
        return f"Agent blocked: {reason}"
    return None


def _issue_blocked_outcome_label(
    event_name: str,
    summary: str,
    context: IssueStoryContext | None,
) -> str | None:
    if event_name == "issue.blocked":
        if context and context.flow_stage == "blocked":
            return _blocked_explanation(context, [{"event": event_name, "summary": summary}])
        reason = summary or "blocked"
        return f"Blocked: {reason}"
    if event_name == "issue.needs_human":
        reason = summary or "unknown"
        return f"Needs human: {reason}"
    return None


_DIRECT_OUTCOME_LABELS = {
    "session.completed": "Completed",
    "review.changes_requested": "Changes Requested",
    "review.approved": "Approved",
    "review.escalated": "Escalated",
    "review.merged": "Merged",
    "issue.completed": "Completed",
}


def _duration_suffix(summary: str) -> str:
    """Extract a duration hint from summary, e.g. ' (2 min)'."""
    # Summaries sometimes contain duration info; pass through if present
    if summary:
        return f": {summary}"
    return ""


def _collect_cycle_artifacts(events: list[dict[str, Any]]) -> dict[str, Any]:  # noqa: C901 — collects artifacts from heterogeneous event types
    """Collect artifact references from cycle events."""
    log_url: str | None = None
    pr_url: str | None = None
    pr_number: int | None = None
    has_review_feedback = False

    for evt in events:
        event_name = str(evt.get("source_event") or evt.get("event") or "")

        # PR from issue.pr_created
        if event_name == "issue.pr_created":
            for artifact in evt.get("artifacts") or []:
                if not isinstance(artifact, dict):
                    continue
                value = str(artifact.get("value") or "")
                if "/pull/" in value:
                    pr_url = value
                    # Extract PR number from URL
                    try:
                        pr_number = int(value.rstrip("/").rsplit("/", 1)[-1])
                    except (ValueError, IndexError):
                        pass

        # Review feedback presence
        if event_name in ("review.changes_requested", "review.approved"):
            has_review_feedback = True

        # Log URL from session artifacts
        for artifact in evt.get("artifacts") or []:
            if not isinstance(artifact, dict):
                continue
            atype = str(artifact.get("type") or "")
            if atype == "log" or atype == "transcript":
                log_url = str(artifact.get("value") or artifact.get("url") or "")

    return {
        "log_url": log_url,
        "pr_url": pr_url,
        "pr_number": pr_number,
        "has_review_feedback": has_review_feedback,
    }


def _format_date_time_label(timestamp: Any) -> str:
    """Format a timestamp to 'Feb 9, 2:10:30 PM' style for cycle headers."""
    if not timestamp:
        return ""
    ts = str(timestamp)
    try:
        dt = datetime.fromisoformat(ts)
        time_part = dt.strftime("%-I:%M:%S %p").lstrip("0")
        date_part = dt.strftime("%b %-d")
        return f"{date_part}, {time_part}"
    except (ValueError, TypeError):
        if "T" in ts and len(ts) >= 19:
            return ts[:19].replace("T", " ")
        return ts


def _format_time_label(timestamp: Any, today: str = "") -> str:
    """Format a timestamp to a label like '8:15:30 PM' (today) or 'Feb 8, 8:15:30 PM' (other days)."""
    if not timestamp:
        return ""
    ts = str(timestamp)
    try:
        dt = datetime.fromisoformat(ts)
        time_part = dt.strftime("%-I:%M:%S %p").lstrip("0")
        if today and ts[:10] == today:
            return time_part
        # Include short date for non-today events
        date_part = dt.strftime("%b %-d")
        return f"{date_part}, {time_part}"
    except (ValueError, TypeError):
        # Fallback: show the time portion
        if "T" in ts and len(ts) >= 19:
            return ts[11:19]
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
    if label in ("needs-human", "failed", "publish-failed"):
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
