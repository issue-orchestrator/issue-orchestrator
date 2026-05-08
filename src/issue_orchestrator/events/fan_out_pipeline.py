"""Canonical fan-out pipeline: internal event → external timeline records.

`DefaultTimelineWriter.record()` is the production caller. Tests and
goldens that need to assert on the user-facing timeline shape go
through the same `produce_external_records()` function rather than
re-implementing writer policy in test code.

Why a single owner:

The fan-out has subtle policy that's easy to miss when re-implementing:

  - View-registry name mapping (one internal → N external).
  - View-tier membership (`user` / `ops` / `debug`) per external event.
  - Narrative enrichment (dynamic text injection like
    `"Review round {N} completed — {verdict}"`).
  - `logical_phase` override **conditional on enrichment context**:
    if the upstream `enrich_logical_semantics()` already promoted a
    coding event to `rework` (because we're in a rework cycle), the
    view-event's `phase="coding"` override must NOT downgrade it.

That last conditional is the one that bit a test re-implementation
during code review of the original external-golden PR — and the
fact that it lived only in writer-internal code is exactly why this
extraction is needed.

Inputs to `produce_external_records`:

  - `internal_event_name` — the event the orchestrator emitted
    (matches `EventName.value` for catalogued events).
  - `enriched_data` — the data dict *after* upstream enrichment
    (logical_run / logical_cycle / logical_phase / event_intent
    already injected). Callers are responsible for that step;
    `DefaultTimelineWriter` does it via `enrich_logical_semantics()`,
    tests do it explicitly when they need to exercise rework or
    other state-machine-driven branches.
  - `base_event_id` — used as-is for the first external record;
    later records get `f"{base}-{i}"` suffixes.
  - `timestamp_iso` — pre-formatted ISO timestamp string.

Returns the ordered list of `TimelineRecord`s the writer would
append to the store for this single internal event.
"""

from __future__ import annotations

from typing import Any, Callable

from ..ports.timeline_store import TimelineRecord
from .view_registry import ViewEvent, fan_out


def produce_external_records(
    *,
    internal_event_name: str,
    enriched_data: dict[str, Any],
    base_event_id: str,
    timestamp_iso: str,
) -> list[TimelineRecord]:
    """Fan out one internal event into its external `TimelineRecord`s.

    See module docstring for policy details and the rework-phase
    conditional. This is the canonical surface — the writer calls
    it, tests call it. There is no other correct way to derive the
    external record stream from an internal event.
    """
    out: list[TimelineRecord] = []
    external_events = fan_out(internal_event_name)
    for i, view_event in enumerate(external_events):
        record_data = _build_external_data(
            internal_event_name=internal_event_name,
            view_event=view_event,
            enriched_data=enriched_data,
        )
        record_id = base_event_id if i == 0 else f"{base_event_id}-{i}"
        out.append(
            TimelineRecord(
                event_id=record_id,
                timestamp=timestamp_iso,
                event=view_event.name,
                data=record_data,
                source_event=internal_event_name,
            )
        )
    return out


def _build_external_data(
    *,
    internal_event_name: str,
    view_event: ViewEvent,
    enriched_data: dict[str, Any],
) -> dict[str, Any]:
    """Apply per-ViewEvent enrichment to a copy of the data dict."""
    record_data = dict(enriched_data)
    record_data["views"] = sorted(view_event.views)
    if view_event.narrative:
        record_data["narrative"] = enrich_narrative(
            view_event.narrative, internal_event_name, enriched_data
        )
    if view_event.phase:
        enriched_phase = record_data.get("logical_phase", "system")
        # Don't override if upstream enrichment already promoted the
        # phase (e.g. coding→rework for session.started in a rework
        # cycle). Mirrors DefaultTimelineWriter exactly.
        if enriched_phase != "rework" or view_event.phase != "coding":
            record_data["logical_phase"] = view_event.phase
    return record_data


def enrich_narrative(
    narrative: str,
    internal_event: str,
    data: dict[str, Any],
) -> str:
    """Dynamic narrative enrichment.

    Static narratives in `VIEW_REGISTRY` carry the baseline text;
    enrichers inject runtime data (round numbers, PR numbers, review
    verdicts) so the stored timeline is self-describing.

    If no enricher is registered for the event, or the enricher
    returns None for the supplied data, the static narrative is
    returned unchanged.
    """
    enricher = _NARRATIVE_ENRICHERS.get(internal_event)
    if enricher is not None:
        return enricher(data) or narrative
    return narrative


def _enrich_round_started(data: dict[str, Any]) -> str | None:
    ri = data.get("round_index")
    return f"Review round {ri} started" if isinstance(ri, int) else None


def _enrich_round_completed(data: dict[str, Any]) -> str | None:
    ri = data.get("round_index")
    if not isinstance(ri, int):
        return None
    verdict = data.get("reviewer_response_type")
    suffix = f" — {verdict}" if isinstance(verdict, str) and verdict else ""
    return f"Review round {ri} completed{suffix}"


def _enrich_session_started(data: dict[str, Any]) -> str | None:
    if data.get("reset_from_scratch"):
        return "Scratch coding agent started"
    return None


def _enrich_issue_unblocked(data: dict[str, Any]) -> str | None:
    if data.get("from_scratch"):
        return "Scratch reset requested"
    return None


def _enrich_review_started(data: dict[str, Any]) -> str | None:
    if data.get("cached"):
        return "Cached review result reused for unchanged commit"
    return None


def _enrich_review_approved(data: dict[str, Any]) -> str | None:
    if data.get("cached"):
        return "Cached review approval reused for unchanged commit"
    rounds = data.get("rounds")
    return (
        f"Review approved after {rounds} rounds"
        if isinstance(rounds, int) and rounds > 1
        else None
    )


def _enrich_review_rework_started(data: dict[str, Any]) -> str | None:
    ri = data.get("round_index")
    return f"Coder started rework for review round {ri}" if isinstance(ri, int) else None


def _enrich_review_rework_completed(data: dict[str, Any]) -> str | None:
    ri = data.get("round_index")
    return f"Coder completed rework for review round {ri}" if isinstance(ri, int) else None


def _enrich_changes_requested(data: dict[str, Any]) -> str | None:
    if data.get("cached"):
        return "Cached changes-requested verdict reused for unchanged commit"
    rounds = data.get("rounds")
    return f"Reviewer requested changes (round {rounds})" if isinstance(rounds, int) else None


def _enrich_pr_created(data: dict[str, Any]) -> str | None:
    pr = data.get("pr_number")
    return f"PR #{pr} created" if isinstance(pr, int) else None


def _enrich_exchange_completed(data: dict[str, Any]) -> str | None:
    rounds = data.get("rounds")
    if not isinstance(rounds, int):
        return None
    plural = "round" if rounds == 1 else "rounds"
    status = data.get("status")
    # Persistent runner emits one of three terminal statuses
    # (`persistent_session_exchange.py:533, 565, 824, 863, 903, 952`).
    # Differentiate the user-facing narrative so the timeline
    # distinguishes a successful exchange from a stopped or errored one.
    if status == "stopped":
        return f"Review exchange ended without approval ({rounds} {plural})"
    if status == "error":
        return f"Review exchange failed ({rounds} {plural})"
    return f"Review exchange completed ({rounds} {plural})"


def _role_label(role: Any) -> str | None:
    if isinstance(role, str) and role in {"coder", "reviewer"}:
        return role.capitalize()
    return None


def _enrich_role_prompted(data: dict[str, Any]) -> str | None:
    role = _role_label(data.get("role"))
    ri = data.get("round_index")
    if role and isinstance(ri, int):
        if data.get("protocol_retry") is True:
            return f"{role} protocol retry sent (round {ri})"
        return f"{role} prompt sent (round {ri})"
    return None


def _enrich_role_feedback(data: dict[str, Any]) -> str | None:
    role = _role_label(data.get("role"))
    ri = data.get("round_index")
    verdict = data.get("response_type")
    if not (role and isinstance(ri, int)):
        return None
    suffix = f" — {verdict}" if isinstance(verdict, str) and verdict else ""
    return f"{role} feedback (round {ri}){suffix}"


def _enrich_role_timeout(data: dict[str, Any]) -> str | None:
    role = _role_label(data.get("role"))
    ri = data.get("round_index")
    if role and isinstance(ri, int):
        return f"{role} timed out (round {ri})"
    return None


_NARRATIVE_ENRICHERS: dict[str, Callable[[dict[str, Any]], str | None]] = {
    "session.started": _enrich_session_started,
    "issue.unblocked": _enrich_issue_unblocked,
    "review.started": _enrich_review_started,
    "review_exchange.round_started": _enrich_round_started,
    "review_exchange.round_completed": _enrich_round_completed,
    "review_exchange.role_prompted": _enrich_role_prompted,
    "review_exchange.role_feedback": _enrich_role_feedback,
    "review_exchange.role_timeout": _enrich_role_timeout,
    "review.rework_started": _enrich_review_rework_started,
    "review.rework_completed": _enrich_review_rework_completed,
    "review.approved": _enrich_review_approved,
    "review.changes_requested": _enrich_changes_requested,
    "issue.pr_created": _enrich_pr_created,
    "review_exchange.completed": _enrich_exchange_completed,
}
