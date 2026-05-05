"""Port for the issue-scoped coder/reviewer subprocess pair registry.

A *persistent exchange pair* is the two PTY-attached agent processes
(one coder, one reviewer) that drive an issue's review-exchange
dialogue. They share an interactive context across rounds so the
agent does not have to rebuild that context from scratch every
prompt.

This port owns the **lifetime** of those pairs, keyed by issue.
Callers spawn a pair once per issue (or first exchange), reuse it
across exchanges, and release it when the issue is done. ADR 0026
documents the design and the migration plan (B1 = registry in
place but still released per-exchange; B2 = drop the per-exchange
release and survive across exchanges; B3 = diagnostics).

The port is defined as a :class:`Protocol` so adapters can swap
freely — in-memory for production and tests, fakes for unit tests
that don't want to touch real subprocesses, and (in B3) a
diagnostics-aware implementation that emits structured events on
every transition.
"""

from __future__ import annotations

from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol


@dataclass
class PersistentExchangePair:
    """One issue's coder + reviewer subprocess pair plus its reviewer worktree.

    Identity is the ``issue_key`` that the registry uses for caching.
    The two :class:`PersistentSession` instances are the live PTY-attached
    agent processes; ``reviewer_worktree_path`` is the sibling worktree
    the reviewer runs in (created at pair construction, fast-forwarded
    between rounds, removed when the pair is released).

    ``created_at`` is wall-clock seconds since epoch, set once at
    construction. ``last_used_at`` is updated on every
    :meth:`PersistentExchangePairRegistry.acquire` cache hit so future
    diagnostics / idle-reaping can see when the pair was last touched.

    The dataclass is mutable so the registry can update
    ``last_used_at`` in place; callers must treat it as read-only and
    route lifecycle changes through the registry.
    """

    # Typed ``Any`` here because the concrete ``PersistentSession``
    # type lives in execution/ — ports are forbidden from depending
    # on the outer layer. The registry adapter (execution-layer)
    # constructs pairs with strictly-typed
    # :class:`~issue_orchestrator.execution.persistent_round_runner.PersistentSession`
    # instances; the port is intentionally opaque about the session
    # type.
    coder_session: Any
    reviewer_session: Any
    reviewer_worktree_path: Path
    issue_key: Hashable
    created_at: float
    last_used_at: float = field(default=0.0)

    def is_alive(self) -> bool:
        """Both subprocesses are still running.

        A dead pair on the cache is a bug — the registry must evict
        and respawn rather than hand back a wedged pair. This is the
        check ``acquire`` does before returning a cached entry.
        """
        return (
            self.coder_session.proc.poll() is None
            and self.reviewer_session.proc.poll() is None
        )


class PersistentExchangePairRegistry(Protocol):
    """Issue-scoped registry of persistent coder/reviewer subprocess pairs.

    Implementations are owned by the composition root (one per
    orchestrator process) and shared across all review-exchange
    invocations. The registry guarantees:

    - One pair per ``issue_key`` at a time. Concurrent acquires for
      the same key return the same pair.
    - A returned pair is always alive (``is_alive()`` true). A cached
      pair whose subprocess died is evicted and respawned.
    - ``release`` and ``shutdown_all`` are the *only* paths that
      terminate a pair. Callers must not close the underlying
      :class:`PersistentSession` instances directly.

    Implementations are not required to be thread-safe across
    different ``issue_key`` values; concurrent reviews of the same
    issue are not a supported use case (the orchestrator serializes
    them via the issue's session lock).
    """

    def acquire(
        self,
        *,
        issue_key: Hashable,
        spawn: Callable[[], PersistentExchangePair],
    ) -> PersistentExchangePair:
        """Return a live pair for ``issue_key``.

        On cache hit (and pair still alive): returns the cached pair
        with ``last_used_at`` refreshed.

        On cache miss or evicted-dead-pair: invokes ``spawn`` to build
        a new pair, caches it, and returns it.

        ``spawn`` is supplied by the caller because *how* to spawn
        (which command, env, recording paths) is a policy the registry
        deliberately does not own — the registry only owns *when* and
        *for how long*. ``spawn`` is invoked at most once per acquire.
        """
        ...

    def release(self, issue_key: Hashable, *, reason: str) -> None:
        """Tear down the cached pair for ``issue_key``, if any.

        Closes both subprocesses and removes the reviewer worktree.
        Idempotent: releasing an already-absent key is a no-op so the
        caller never has to track whether a release already happened
        on the failure path.

        ``reason`` is recorded on the structured event emitted by
        diagnostics-aware implementations (B3). Use a short stable
        token like ``"exchange-complete"``, ``"issue-closed"``,
        ``"reset-retry"``, ``"orchestrator-shutdown"``,
        ``"dead-on-acquire"``.
        """
        ...

    def shutdown_all(self, *, reason: str) -> None:
        """Tear down every cached pair. Wired to orchestrator shutdown.

        ``reason`` is propagated to per-pair release events.
        """
        ...
