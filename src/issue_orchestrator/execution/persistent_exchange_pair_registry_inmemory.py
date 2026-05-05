"""In-memory adapter for ``PersistentExchangePairRegistry``.

Owned by the composition root (one instance per orchestrator process)
and shared across all review-exchange invocations. Tear-down is wired
into orchestrator shutdown so no PTY-attached agent processes leak
when the orchestrator stops.

This module also defines :class:`PersistentExchangePair`, the value
type the adapter caches and returns from :meth:`acquire`. The
dataclass and ``acquire`` live in execution/ on purpose: they
reference :class:`PersistentSession` (a subprocess-backed handle),
which the port boundary forbids exposing. Control-layer callers
that only need the ``release`` / ``shutdown_all`` lifecycle verbs
depend on the narrow port; execution-layer callers that need
``acquire`` import this concrete adapter directly.

ADR 0026 documents the design and migration plan.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any

from ..ports.persistent_exchange_pair_registry import PersistentExchangePairRegistry
from .persistent_round_runner import PersistentSession, close_persistent_session

logger = logging.getLogger(__name__)


@dataclass
class PersistentExchangePair:
    """One issue's coder + reviewer subprocess pair plus its reviewer worktree.

    Lives in execution/ so the dataclass can name
    :class:`PersistentSession` directly without violating the port
    boundary. Returned by :meth:`InMemoryPersistentExchangePairRegistry.acquire`
    to execution-layer callers.

    ``created_at`` is wall-clock seconds since epoch, set once at
    construction. ``last_used_at`` is updated on every cache hit so
    future diagnostics / idle-reaping can see when the pair was
    last touched. The dataclass is mutable so the registry can
    update ``last_used_at`` in place; callers must treat it as
    read-only and route lifecycle changes through
    ``release`` / ``shutdown_all``.
    """

    coder_session: PersistentSession
    reviewer_session: PersistentSession
    reviewer_worktree_path: Path
    issue_key: Hashable
    created_at: float
    last_used_at: float = field(default=0.0)


def _pair_is_alive(pair: PersistentExchangePair) -> bool:
    """Both subprocesses are still running.

    Lives at module scope, not as a method on the dataclass, because
    aliveness is a subprocess-shape question — keeping it out of the
    value type means the type stays free of "reach through ``proc.poll()``"
    semantics that the reviewer flagged as a port-boundary smell on
    PR #6209.
    """
    return (
        pair.coder_session.proc.poll() is None
        and pair.reviewer_session.proc.poll() is None
    )


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

    ``acquire`` is execution-only: callers that need it import this
    concrete class instead of the narrow port. Control-layer callers
    only see ``release`` / ``shutdown_all`` via the port.
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
        """Return a live pair for ``issue_key``.

        On cache hit (and pair still alive): returns the cached pair
        with ``last_used_at`` refreshed.

        On cache miss or evicted-dead-pair: invokes ``spawn`` to
        build a new pair, caches it, and returns it.

        ``spawn`` is supplied by the caller because *how* to spawn
        (which command, env, recording paths) is a policy the
        registry deliberately does not own — the registry only owns
        *when* and *for how long*. ``spawn`` is invoked at most
        once per acquire.
        """
        with self._lock:
            existing = self._cache.get(issue_key)
            if existing is not None:
                if _pair_is_alive(existing):
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
                    "alive": _pair_is_alive(pair),
                    "age_seconds": now - pair.created_at,
                    "idle_seconds": now - pair.last_used_at,
                }
                for pair in self._cache.values()
            ]
