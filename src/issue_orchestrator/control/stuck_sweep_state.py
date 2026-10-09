"""The stuck sweep's durable state: its timer and recovery counters (#6824).

Hydrated at startup and persisted after every sweep. Moved out of
``stuck_sweep`` so that module holds the sweep's decisions alone.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from ..ports.queue_cache_store import QueueCacheStore

logger = logging.getLogger(__name__)


def hydrate_stuck_sweep_state(
    state: "OrchestratorState",
    store: "QueueCacheStore | None",
) -> None:
    """Restore the durable sweep timer and recovery counters at startup.

    The recovery counter is crash-safe truth: without it a restart would reset
    every issue's budget to 0 and re-inject an already-exhausted issue forever.
    Loaded unconditionally when a store is present (even when the sweep is
    disabled) so the counters survive an enable/disable toggle.
    """
    if store is None:
        return
    state.last_stuck_sweep_at = store.load_last_stuck_sweep_at()
    state.recovery_attempts = store.load_recovery_attempts()
    # Unacknowledged escalations survive a restart so an exhausted issue is
    # re-escalated until its needs-human label lands (#6824 R1).
    state.pending_stuck_sweep_escalations = store.load_pending_escalations()
    state.review_release_budgets = store.load_review_release_budgets()


def persist_stuck_sweep_state(
    state: "OrchestratorState",
    store: "QueueCacheStore | None",
) -> None:
    """Persist the sweep timer + recovery counters; degrade on failure.

    A persist failure is logged, not raised: the sweep already mutated
    in-memory state and a restart re-hydrates from the store, so a lost write
    at worst re-sweeps one issue early or under-counts one attempt — never a
    crash on the observation path (mirrors ``record_health_review_creation``).
    """
    if store is None:
        return
    try:
        _save(state, store)
    except Exception:
        logger.warning(
            "[STUCK_SWEEP] failed to persist recovery counters; a restart "
            "re-hydrates them from the queue-cache store",
            exc_info=True,
        )


def _save(state: "OrchestratorState", store: "QueueCacheStore") -> None:
    store.save_last_stuck_sweep_at(state.last_stuck_sweep_at)
    store.save_recovery_attempts(state.recovery_attempts)
    store.save_pending_escalations(state.pending_stuck_sweep_escalations)
    store.save_review_release_budgets(state.review_release_budgets)


def forget_stuck_sweep_issue(
    state: "OrchestratorState",
    store: "QueueCacheStore | None",
    issue_number: int,
) -> None:
    """A reset ended the issue's attempt; its sweep record ends with it (#8219).

    The recovery budget, the unlanded needs-human escalation and the one-shot
    escalation/release buffers were all decided on the attempt the reset
    discarded. Kept, the next plan would block the fresh retry behind
    ``needs-human`` from the old budget. Persisted at once and strictly - unlike
    a sweep's own save, a failure raises - so the reset is never reported
    settled while a restart could still hydrate the old record back.
    """
    state.recovery_attempts.pop(issue_number, None)
    state.pending_stuck_sweep_escalations.discard(issue_number)
    state.review_release_budgets.discard(issue_number)
    state.stuck_sweep_escalations = [n for n in state.stuck_sweep_escalations if n != issue_number]
    state.stuck_sweep_review_releases = [
        n for n in state.stuck_sweep_review_releases if n != issue_number
    ]
    state.stuck_sweep_held_for_review = state.stuck_sweep_held_for_review - {issue_number}
    if store is not None:
        _save(state, store)


__all__ = ["forget_stuck_sweep_issue", "hydrate_stuck_sweep_state", "persist_stuck_sweep_state"]
