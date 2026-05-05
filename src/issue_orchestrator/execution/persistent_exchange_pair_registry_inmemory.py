"""In-memory implementation of :class:`PersistentExchangePairRegistry`.

Owned by the composition root (one instance per orchestrator process)
and shared across all review-exchange invocations. Tear-down is wired
into orchestrator shutdown so no PTY-attached agent processes leak
when the orchestrator stops.

ADR 0026 documents the design and migration plan. In B1 the registry
is in place but every exchange still calls ``release`` at the end, so
the cache never has hits; B2 drops the per-exchange ``release`` and
the registry's caching starts to matter.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Hashable
from threading import RLock
from typing import Any

from ..ports.persistent_exchange_pair_registry import (
    PersistentExchangePair,
    PersistentExchangePairRegistry,
)
from .persistent_round_runner import close_persistent_session

logger = logging.getLogger(__name__)


class InMemoryPersistentExchangePairRegistry(PersistentExchangePairRegistry):
    """Thread-safe issue-keyed cache of live coder/reviewer pairs.

    Concurrent acquires for *different* keys are allowed; concurrent
    acquires for the *same* key are serialized through the registry's
    lock so a slow ``spawn`` callable can't be invoked twice.

    The implementation deliberately does not own the reviewer
    worktree's filesystem layout — ``release`` invokes the
    caller-supplied ``on_release`` hook (registered at construction)
    so worktree removal can call into the existing reviewer-worktree
    helpers without this module growing a dependency on them.
    """

    def __init__(
        self,
        *,
        on_release: Callable[[PersistentExchangePair, str], None] | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self._cache: dict[Hashable, PersistentExchangePair] = {}
        self._lock = RLock()
        self._on_release = on_release
        self._clock = clock

    def acquire(
        self,
        *,
        issue_key: Hashable,
        spawn: Callable[[], PersistentExchangePair],
    ) -> PersistentExchangePair:
        with self._lock:
            existing = self._cache.get(issue_key)
            if existing is not None:
                if existing.is_alive():
                    existing.last_used_at = self._clock()
                    logger.debug(
                        "[exchange-pair-registry] cache hit issue_key=%s "
                        "coder_pid=%d reviewer_pid=%d age=%.1fs",
                        issue_key,
                        existing.coder_session.proc.pid,
                        existing.reviewer_session.proc.pid,
                        self._clock() - existing.created_at,
                    )
                    return existing
                logger.warning(
                    "[exchange-pair-registry] cached pair for issue_key=%s is "
                    "dead (coder_alive=%s reviewer_alive=%s); evicting and respawning",
                    issue_key,
                    existing.coder_session.proc.poll() is None,
                    existing.reviewer_session.proc.poll() is None,
                )
                self._tear_down(existing, reason="dead-on-acquire")
                del self._cache[issue_key]

            pair = spawn()
            pair.last_used_at = self._clock()
            self._cache[issue_key] = pair
            logger.info(
                "[exchange-pair-registry] spawned issue_key=%s "
                "coder_pid=%d reviewer_pid=%d reviewer_worktree=%s",
                issue_key,
                pair.coder_session.proc.pid,
                pair.reviewer_session.proc.pid,
                pair.reviewer_worktree_path,
            )
            return pair

    def release(self, issue_key: Hashable, *, reason: str) -> None:
        with self._lock:
            pair = self._cache.pop(issue_key, None)
            if pair is None:
                return
            self._tear_down(pair, reason=reason)

    def shutdown_all(self, *, reason: str) -> None:
        with self._lock:
            keys = list(self._cache.keys())
            for issue_key in keys:
                pair = self._cache.pop(issue_key)
                self._tear_down(pair, reason=reason)

    def _tear_down(self, pair: PersistentExchangePair, *, reason: str) -> None:
        """Close subprocesses and notify the worktree-removal hook.

        Errors during close are logged but never re-raised — release
        runs in ``finally`` blocks where a raise would mask the
        original exception. Errors are still surfaced in the log so
        leaks don't go unnoticed.
        """
        coder_pid = pair.coder_session.proc.pid
        reviewer_pid = pair.reviewer_session.proc.pid
        logger.info(
            "[exchange-pair-registry] releasing issue_key=%s reason=%s "
            "coder_pid=%d reviewer_pid=%d age=%.1fs",
            pair.issue_key, reason, coder_pid, reviewer_pid,
            self._clock() - pair.created_at,
        )
        for session_label, session in (
            ("reviewer", pair.reviewer_session),
            ("coder", pair.coder_session),
        ):
            try:
                close_persistent_session(session)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "[exchange-pair-registry] %s session close raised "
                    "issue_key=%s pid=%d reason=%s",
                    session_label, pair.issue_key, session.proc.pid, reason,
                )

        if self._on_release is not None:
            try:
                self._on_release(pair, reason)
            except Exception:  # noqa: BLE001
                logger.exception(
                    "[exchange-pair-registry] on_release hook raised "
                    "issue_key=%s reason=%s reviewer_worktree=%s",
                    pair.issue_key, reason, pair.reviewer_worktree_path,
                )

    def snapshot(self) -> list[dict[str, Any]]:
        """Return a debug snapshot of cached pairs (for diagnostics).

        Used by future control-API endpoints (B3); returned dicts are
        plain JSON-serializable values so the caller can render them
        without importing this module.
        """
        with self._lock:
            now = self._clock()
            return [
                {
                    "issue_key": str(pair.issue_key),
                    "coder_pid": pair.coder_session.proc.pid,
                    "reviewer_pid": pair.reviewer_session.proc.pid,
                    "reviewer_worktree": str(pair.reviewer_worktree_path),
                    "alive": pair.is_alive(),
                    "age_seconds": now - pair.created_at,
                    "idle_seconds": now - pair.last_used_at,
                }
                for pair in self._cache.values()
            ]
