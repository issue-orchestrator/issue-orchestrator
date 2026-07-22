"""Issue detail view model builder — synthesises an 'issue story' for the drawer."""

from __future__ import annotations

from collections.abc import Mapping
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
from ..domain.logical_run_projection import LogicalRunProjector
from ..events import EventName
from .blocked_explanations import invalid_or_validation_blocked_explanation
# Canonical event-set ownership lives in ``view_models.lifecycle_event_sets``
# (issue #6310 AC-4).  Issue_detail aliases ``OUTCOME_EVENTS`` and
# ``BLOCKED_EVENT_NAMES`` for blocked-detail derivation and the AC-4 guard
# test.  Journey projection (typed cycles, runs, validation badge) is built
# by ``view_models.journey_projection`` — see the typed pipeline call in
# ``build_issue_detail_view_model``.
from .journey_projection import build_journey_cycles_from_events, build_journey_runs
from .lifecycle_event_sets import (
    BLOCKED_EVENT_NAMES as _CANONICAL_BLOCKED_EVENT_NAMES,
    OUTCOME_EVENTS as _CANONICAL_OUTCOME_EVENTS,
)
from .lifecycle_semantics import IssueProjectionContext
from .rework_status import format_queued_rework_summary


# ---------------------------------------------------------------------------
# Context dataclass — assembled by the web endpoint from orchestrator state
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class IssueStoryContext:
    """Snapshot of orchestrator state relevant to one issue's story."""

    flow_stage: str  # queued / in_progress / blocked / awaiting_merge / done
    active_runtime_minutes: int | None = None  # if a session is currently running
    active_task_kind: str | None = None  # code / review / rework / tech_lead
    labels: tuple[str, ...] = ()
    dependency_summary: str | None = None
    current_rework_cycle: int = 0
    max_rework_cycles: int = 5
    pr_url: str | None = None
    pr_number: int | None = None
    # Short reason an issue is queued for rework (e.g. "Merge conflict against
    # base branch"); set only when ``flow_stage == "queued_for_rework"``.
    rework_reason: str | None = None


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
    raw_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build issue detail payload used by the dashboard drawer."""
    today = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d")
    event_source = raw_events if view == "raw" and raw_events is not None else events
    filtered = _filter_events_by_view(event_source, view)
    # Raw view still needs semantic runs so the frontend can scope raw events
    # to Latest run versus All runs without rendering the lifecycle body.
    projection_view = "debug" if view == "raw" else view
    projection_events = _filter_events_by_view(events, projection_view)
    story_events = _story_projection_events(projection_events, projection_view)
    timeline_steps = _build_journey_steps(story_events, today)
    previous_runs = _build_previous_cycles(cycles, today)

    # Typed projection pipeline (issue #6310): journey cycles + runs are
    # now built by ``lifecycle_projection`` as typed models, including the
    # typed ``CycleValidationBadge`` (AC-2).  The drawer payload comes
    # from ``.model_dump(mode="json")`` — same wire field names, but the
    # parallel dict projection is gone.
    projection_context = _projection_context_from_story_context(context)
    typed_cycles = build_journey_cycles_from_events(
        story_events,
        today,
        projection_context,
        issue_number=issue_number,
    )
    typed_runs = build_journey_runs(typed_cycles)
    runs = [run.model_dump(mode="json") for run in typed_runs]

    return {
        "issue_number": issue_number,
        "title": title,
        "issue_url": issue_url,
        "phase_toc": phase_toc,
        "cycles": cycles,
        "events": filtered,
        "summary": _summary(filtered),
        "actions": [],
        # Story fields
        "view": view,
        "status_explanation": _build_status_explanation(context, filtered),
        "timeline_steps": timeline_steps,
        "runs": runs,
        "run_count": len(runs),
        "previous_runs": previous_runs,
        "previous_runs_count": len(previous_runs),
        "raw_events_count": len(raw_events if raw_events is not None else events),
        "blocked_detail": _build_blocked_detail(context, filtered),
    }


def _projection_context_from_story_context(
    context: IssueStoryContext | None,
) -> IssueProjectionContext:
    """Build the neutral projection-layer context from the entry-point one.

    Keeps the layering inversion away from ``lifecycle_projection`` (see
    issue #6310 AC-3): the projection module never imports
    ``IssueStoryContext``.
    """
    if context is None:
        return IssueProjectionContext()
    return IssueProjectionContext(
        flow_stage=context.flow_stage,
        labels=context.labels,
        current_rework_cycle=context.current_rework_cycle,
        max_rework_cycles=context.max_rework_cycles,
    )

# ---------------------------------------------------------------------------
# View filtering
# ---------------------------------------------------------------------------

def _filter_events_by_view(events: list[dict[str, Any]], view: str) -> list[dict[str, Any]]:
    """Filter events to those visible in the requested view.

    Events with a ``views`` tag are included only if ``view`` is in the list.
    Events without a ``views`` tag (pre-registry data) are included in all views.
    """
    if view == "raw":
        return list(events)
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
    """Represent each Story review segment with either its start or terminal row.

    While a review segment is still *open* (the round has not closed with a
    terminal event), the in-round coder<->reviewer mechanics are still dropped
    from the row list, but the latest meaningful substate is surfaced as a
    single transient "in progress" row so the Story timeline keeps advancing
    instead of freezing on "Code review started" for the duration of an
    in-round rework (issue #6428). Once the round closes, the completed view
    stays clean — no mechanic rows survive.
    """
    collapsed: list[dict[str, Any]] = []
    pending_start_index: int | None = None
    latest_open_mechanic: dict[str, Any] | None = None

    for event in events:
        event_name = _canonical_event_name(event)
        if event_name in _REVIEW_STORY_MECHANIC_EVENTS:
            if pending_start_index is not None:
                latest_open_mechanic = event
            continue

        if event_name in _REVIEW_START_CLUSTER_EVENTS:
            collapsed.append(event)
            pending_start_index = len(collapsed) - 1
            latest_open_mechanic = None
            continue

        if event_name in _REVIEW_STORY_SEGMENT_TERMINAL_EVENTS:
            if pending_start_index is not None:
                start_event = collapsed[pending_start_index]
                del collapsed[pending_start_index]
                pending_start_index = None
                latest_open_mechanic = None
                collapsed.append(_project_review_terminal_story_event(event, start_event))
            else:
                collapsed.append(_project_review_terminal_story_event(event))
            continue

        if _event_starts_new_story_work_segment(event):
            pending_start_index = None
            latest_open_mechanic = None
        collapsed.append(event)

    if pending_start_index is not None and latest_open_mechanic is not None:
        progress = _OpenReviewRoundProgress.from_open_round_mechanic(latest_open_mechanic)
        if progress is not None:
            row = dict(latest_open_mechanic)
            _fill_missing_review_session_context(row, collapsed[pending_start_index])
            row["narrative"] = progress.narrative
            row["in_round_progress"] = True
            collapsed.append(row)

    return collapsed


@dataclass(frozen=True)
class _OpenReviewRoundProgress:
    """Bounded typed owner of the open-review-round in-progress decision (#6428).

    While a review round is still open the in-round mechanics are dropped from
    the Story rows, which used to freeze the timeline on "Code review started"
    for the whole of an in-round rework. This value object owns that transient
    row's policy: an instance existing means "surface a live progress row" and
    ``narrative`` is its copy. The decision reuses
    ``_review_exchange_event_status`` so the row and the always-on status line
    describe the same substate — no raw-dict seam, no cross-path drift.
    """

    narrative: str

    @classmethod
    def from_open_round_mechanic(
        cls,
        mechanic_event: Mapping[str, Any],
    ) -> _OpenReviewRoundProgress | None:
        """Decide the transient progress row, or ``None`` when there is none.

        ``None`` when the latest substate is already represented by the round's
        "Code review started" row, or carries no renderable substate.
        """
        event_name = _canonical_event_name(mechanic_event)
        if not cls._is_progress_signal(event_name, mechanic_event):
            return None
        status = _review_exchange_event_status(event_name, mechanic_event)
        if not status:
            return None
        return cls(narrative=_capitalize_first(status))

    @staticmethod
    def _is_progress_signal(event_name: str, event: Mapping[str, Any]) -> bool:
        # The reviewer's opening pass is already the "Code review started" row,
        # so only feedback and the coder rework prompt surface a progress row.
        if event_name == EventName.REVIEW_EXCHANGE_ROLE_FEEDBACK.value:
            return True
        if event_name == EventName.REVIEW_EXCHANGE_ROLE_PROMPTED.value:
            return event.get("role") == "coder"
        return False


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


def _canonical_event_name(event: Mapping[str, Any]) -> str:
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
        review_exchange_status = _active_review_exchange_status(events)
        if review_exchange_status is not None:
            return (
                f"Review exchange: {review_exchange_status} "
                f"({ctx.active_runtime_minutes} min)"
            )
        kind_label = {
            "code": "Code session",
            "review": "Code review",
            "retrospective-review": "Retrospective review",
            "rework": "Rework session",
            "tech_lead": "Tech Lead review",
        }.get(ctx.active_task_kind or "", "Session")
        return f"{kind_label} in progress ({ctx.active_runtime_minutes} min)"

    # Queued for rework \u2014 a PR needs another coding pass (reviewer changes or
    # a post-publish merge/validation problem) and the rework session has not
    # launched yet. Surfaced before awaiting-merge so a stale pr-pending label
    # does not mask it.
    if ctx.flow_stage == "queued_for_rework":
        return format_queued_rework_summary(
            ctx.pr_number,
            ctx.current_rework_cycle,
            ctx.rework_reason or "Rework requested",
        )

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


def _active_review_exchange_status(events: list[dict[str, Any]]) -> str | None:
    """Return the latest in-flight review-exchange substate, if current."""
    for index in range(len(events) - 1, -1, -1):
        event = events[index]
        event_name = _canonical_event_name(event)
        if event_name.startswith("review_exchange."):
            status = _review_exchange_event_status(event_name, event)
            if status:
                return status
            continue
        if event_name in {"review.rework_started", "review.rework_completed"}:
            narrative = _event_narrative(event)
            return _lowercase_first(narrative) if narrative else None
        if (
            event_name in {"session.validation_retry_needed", "session.validation_failed"}
            and _has_review_exchange_before(events, index)
        ):
            narrative = _event_narrative(event)
            return (
                _lowercase_first(narrative)
                if narrative
                else "validation failed; coder continuing"
            )
        if event_name in {"session.started", "agent.coding_started", "rework.started"}:
            return None
    return None


def _has_review_exchange_before(
    events: list[dict[str, Any]],
    before_index: int,
) -> bool:
    return any(
        _canonical_event_name(event).startswith("review_exchange.")
        for event in events[:before_index]
    )


def _review_exchange_event_status(
    event_name: str,
    event: Mapping[str, Any],
) -> str | None:
    round_index = event.get("round_index")
    round_suffix = (
        f" (round {round_index})" if isinstance(round_index, int) else ""
    )
    if event_name == EventName.REVIEW_EXCHANGE_ROUND_STARTED.value:
        return f"reviewer running{round_suffix}"
    if event_name == EventName.REVIEW_EXCHANGE_ROLE_PROMPTED.value:
        narrative = _event_narrative(event)
        if narrative:
            return _lowercase_first(narrative)
        role = event.get("role")
        if role == "reviewer":
            return f"reviewer running{round_suffix}"
        if role == "coder":
            return f"coder running{round_suffix}"
    if event_name == EventName.REVIEW_EXCHANGE_ROLE_FEEDBACK.value:
        role = event.get("role")
        response_type = event.get("reviewer_response_type") or event.get("response_type")
        if role == "reviewer" and response_type == "changes_requested":
            return f"coder needs requested changes{round_suffix}"
        narrative = _event_narrative(event)
        return _lowercase_first(narrative) if narrative else None
    if event_name == EventName.REVIEW_EXCHANGE_ROLE_TIMEOUT.value:
        narrative = _event_narrative(event)
        return _lowercase_first(narrative) if narrative else "role did not complete"
    if event_name in {
        EventName.REVIEW_EXCHANGE_ROUND_COMPLETED.value,
        EventName.REVIEW_EXCHANGE_COMPLETED.value,
    }:
        narrative = _event_narrative(event)
        return _lowercase_first(narrative) if narrative else None
    return None


def _event_narrative(event: Mapping[str, Any]) -> str:
    narrative = event.get("narrative")
    if isinstance(narrative, str) and narrative:
        return narrative
    summary = event.get("summary")
    if isinstance(summary, str) and summary:
        return summary
    return ""


def _lowercase_first(text: str) -> str:
    if not text:
        return ""
    return text[0].lower() + text[1:]


def _capitalize_first(text: str) -> str:
    if not text:
        return ""
    return text[0].upper() + text[1:]


# Canonical blocked-event set lives in ``lifecycle_projection``.  The local
# alias is preserved so call sites stay stable while the canonical set
# remains the single source of truth (issue #6310 AC-4).
_BLOCKED_EVENT_NAMES = _CANONICAL_BLOCKED_EVENT_NAMES


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

    specific = invalid_or_validation_blocked_explanation(
        event_name=event_name,
        event_summary=event_summary,
        labels=labels,
        event=blocking_event,
    )
    if specific is not None:
        return specific

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
    "session.invalid_completion_record": "Completion record rejected{_summary}",
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
    "tech_lead.launching": "Tech Lead review launching",
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
        if event.get("in_round_progress") is True:
            step_dict["in_round_progress"] = True
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

# Canonical outcome-event set lives in ``lifecycle_projection``.  The local
# alias keeps existing call sites stable (issue #6310 AC-4).
_OUTCOME_EVENTS = _CANONICAL_OUTCOME_EVENTS


def filter_last_run_cycles(cycles: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Filter cycles to only those from the latest lifecycle.

    Mirrors the frontend ``Latest run`` intent (logical run), so backend and
    UI share the same semantics.

    Returns all cycles from max lifecycle when available; falls back to run_id
    grouping only for legacy payloads without lifecycle annotations.
    """
    return _logical_run_projector.filter_last_run_cycles(cycles)


# Validation passed/failed event classification: imported at module top
# from `lifecycle_projection.VALIDATION_PASSED_EVENTS` /
# `VALIDATION_FAILED_EVENTS` so this projection cannot drift from the
# lifecycle projection's own definition of "what counts as a passed /
# failed validation event". Adding a new validation event name in
# lifecycle_projection automatically propagates here.
#
# Coding-side terminal events: imported at module top from
# `lifecycle_projection.CODING_TERMINAL_EVENTS` so the per-cycle validation
# badge here uses the same definition of "coding is over" as the lifecycle
# projection. A cycle that hasn't yet emitted one of these is still running,
# so the absence of a validation event is expected — not an anti-pattern.
# We only surface "Not validated" once coding has actually finished without
# recording any test evidence.



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
