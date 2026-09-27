"""When the health review last fired, durably, and reconciled with anchor truth.

The cadence half of the health-review trigger (ADR-0031): stamp and persist a
marker-labeled anchor's creation, and at startup hydrate the stamp - taking
the newer of the store and the newest health anchor, since the anchors are
the crash-safe truth (ADR-0013). Moved out of ``health_review_trigger`` (#7347)
so that module holds the trigger decision and anchor routing alone.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime
from typing import TYPE_CHECKING, Optional

from .health_review_trigger import (
    has_health_review_marker,
    health_review_anchor_issues,
    health_review_interval_minutes,
)

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from ..infra.config import Config
    from ..ports import Issue, RepositoryHost
    from ..ports.queue_cache_store import QueueCacheStore
    from .actions import CreateTechLeadIssueAction

logger = logging.getLogger(__name__)


def record_health_review_creation(
    action: CreateTechLeadIssueAction,
    state: "OrchestratorState",
    store: "Optional[QueueCacheStore]",
    now: Optional[float] = None,
) -> None:
    """Record a successful anchor creation (marker-labeled actions only).

    Stamps the in-memory state (stops the next tick re-firing) and persists
    the marker durably (stops a restart re-firing). Persistence failure is
    logged, not raised: the created issue must not be reported as an apply
    failure, the in-memory stamp plus the open anchor's marker label guard
    the current process, and a restart reconciles the durable value from the
    anchor issue itself (:func:`hydrate_last_health_review_at`) — the
    external side effect and the durable timestamp cannot silently diverge.
    """
    if not has_health_review_marker(action.labels):
        return
    stamped_at = time.time() if now is None else now
    state.last_health_review_at = stamped_at
    # Record the board the trigger DECIDED on, carried verbatim on the action —
    # never a fresh recompute. By now the anchor has been created and queued
    # into state.pending_tech_lead_reviews, which is itself part of the board, so
    # recomputing here would stamp a transient state that only exists between
    # creation and launch and that the board never returns to. The gate would
    # then never match and would re-fire every interval forever — the exact
    # waste this trigger exists to prevent.
    #
    # "" (a storm anchor planned without facts) means "never reviewed", which
    # makes the next due review fire: fail toward reviewing, never toward
    # silent suppression.
    state.last_reviewed_board_fingerprint = action.health_review_fingerprint
    if store is None:
        return
    try:
        store.save_last_health_review_at(stamped_at)
        store.save_last_reviewed_board_fingerprint(
            state.last_reviewed_board_fingerprint
        )
    except Exception:
        logger.warning(
            "Failed to persist health-review markers; a restart reconciles the "
            "interval from the anchor issue and re-reviews on any board change",
            exc_info=True,
        )


def hydrate_last_health_review_at(
    config: "Config",
    state: "OrchestratorState",
    store: "Optional[QueueCacheStore]",
    repository_host: "RepositoryHost",
) -> None:
    """Hydrate the last-fired marker at startup, reconciling with anchor truth.

    The store is the fast path, but the anchor issues themselves are the
    crash-safe truth (ADR-0013): if persisting the stamp failed after an
    anchor was created (disk full, SQLite error), the store is BEHIND — once
    that anchor closes, plain store hydration would re-fire the review before
    the interval elapses. Reconcile by deriving the last-fired time from the
    newest marker-labeled anchor in scope (open or closed) and taking the
    newer of the two; the reconciled value is persisted back so the store
    self-heals. Costs one GitHub call, at startup, only when the trigger is
    armed.
    """
    stored = store.load_last_health_review_at() if store is not None else 0.0
    state.last_health_review_at = stored
    # Rehydrate the reviewed-board fingerprint so a restart does not re-walk an
    # unchanged board. There is no anchor-issue truth for it (unlike the
    # timestamp), so a lost value stays "" and the next due review fires — the
    # fail-toward-reviewing side.
    state.last_reviewed_board_fingerprint = (
        store.load_last_reviewed_board_fingerprint() if store is not None else ""
    )
    if health_review_interval_minutes(config) <= 0:
        return
    anchored = most_recent_health_anchor_created_at(repository_host, config)
    if anchored <= stored:
        return
    logger.info(
        "Reconciled last_health_review_at from anchor truth: store=%.2f anchor=%.2f",
        stored,
        anchored,
    )
    state.last_health_review_at = anchored
    if store is None:
        return
    try:
        store.save_last_health_review_at(anchored)
    except Exception:
        logger.warning(
            "Failed to self-heal persisted last_health_review_at; the next "
            "restart will reconcile from the anchor issue again",
            exc_info=True,
        )


def most_recent_health_anchor_created_at(
    repository_host: "RepositoryHost", config: "Config"
) -> float:
    """Created-at epoch of the newest in-scope health anchor (0.0 when none)."""
    scoped = health_review_anchor_issues(repository_host, config, state="all")
    return max((_created_at_epoch(issue) for issue in scoped), default=0.0)


def _created_at_epoch(issue: "Issue") -> float:
    """Parse an issue's ISO-8601 ``created_at`` into an epoch timestamp."""
    raw = issue.created_at
    if raw is None:
        return 0.0
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00")).timestamp()


__all__ = [
    "hydrate_last_health_review_at",
    "most_recent_health_anchor_created_at",
    "record_health_review_creation",
]
