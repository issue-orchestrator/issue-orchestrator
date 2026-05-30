"""Session-launch helpers for fresh lifecycle reruns."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Protocol

from ..domain.fresh_lifecycle_rerun import (
    FRESH_LIFECYCLE_RERUN_INTENT,
    prepend_fresh_lifecycle_rerun_context,
)
from ..ports.event_sink import SessionStartedEventPayload
from .actions import Action, RemoveLabelAction


class ActionApplier(Protocol):
    def __call__(self, actions: list[Action], *, context: str) -> bool: ...


def is_enabled(labels: Sequence[str], rerun_label: str) -> bool:
    """Return whether the issue carries the pending fresh-rerun intent."""
    return rerun_label in labels


def metadata(enabled: bool) -> dict[str, object]:
    """Return session/event metadata for a fresh-rerun launch."""
    return {"rerun_intent": FRESH_LIFECYCLE_RERUN_INTENT} if enabled else {}


def apply_manifest(
    update_manifest: Callable[[dict[str, object]], None],
    from_scratch: bool,
    started_at: str,
    fresh: bool,
) -> None:
    """Record scratch and fresh-rerun launch metadata in the session manifest."""
    updates: dict[str, object] = {}
    if from_scratch:
        updates.update({
            "reset_from_scratch": True,
            "review_cache_boundary": "scratch_reset",
            "review_cache_boundary_started_at": started_at,
        })
    updates.update(metadata(fresh))
    if updates:
        update_manifest(updates)


def clear_label(
    apply_actions: ActionApplier,
    issue_number: int,
    rerun_label: str,
    enabled: bool,
) -> None:
    """Best-effort cleanup of consumed fresh lifecycle rerun intent."""
    if enabled:
        apply_actions([
            RemoveLabelAction(
                issue_number=issue_number,
                label=rerun_label,
                reason="fresh lifecycle rerun session launched - clearing intent",
            ),
        ], context="launch_clear_fresh_lifecycle_rerun_coding")


def prompt(prompt_text: str, *, enabled: bool) -> str:
    """Prepend coding rerun context when fresh-rerun intent is active."""
    return prepend_fresh_lifecycle_rerun_context(prompt_text) if enabled else prompt_text


def apply_event_metadata(
    payload: SessionStartedEventPayload,
    enabled: bool,
) -> None:
    """Add fresh-rerun event metadata when active."""
    if enabled:
        payload["rerun_intent"] = FRESH_LIFECYCLE_RERUN_INTENT
