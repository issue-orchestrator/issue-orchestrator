"""Behavior-only port for the issue-scoped persistent-pair registry.

This port is the **control-facing** contract for the registry: control
code (``ActionApplier``, ``Orchestrator``'s shutdown path, web reset
endpoints) needs to *release* pairs at lifecycle boundaries and
shut them all down on orchestrator stop. None of those callers care
about subprocess shape, recording paths, or reviewer worktrees —
they only need the lifecycle verbs.

Acquisition (``acquire(spawn=...)``) and the concrete pair value
type (``PersistentExchangePair``) live in execution/ and are
deliberately *not* exposed through this port. That keeps the
import-linter "ports must not depend on execution" rule honest:
the port has no knowledge of PTY sessions, ``subprocess.Popen``, or
recording writers, so a future adapter (non-PTY, remote process,
mocked) can implement this contract without anyone having to
re-shape control-layer code.

Execution-layer callers that need ``acquire`` import the concrete
:class:`~issue_orchestrator.execution.persistent_exchange_pair_registry_inmemory.InMemoryPersistentExchangePairRegistry`
directly; they are inside the boundary that's allowed to know the
adapter shape.
"""

from __future__ import annotations

from collections.abc import Hashable
from typing import Protocol


class PersistentExchangePairRegistry(Protocol):
    """Lifecycle contract for the issue-scoped persistent-pair registry.

    Implementations are owned by the composition root (one per
    orchestrator process) and shared across all review-exchange
    invocations.

    The two methods on this port are the *only* paths that may
    terminate a pair. Direct ``close_persistent_session`` calls or
    similar bypasses must not appear outside the adapter — going
    through ``release`` / ``shutdown_all`` is what makes the
    lifecycle invariants in ADR 0026 enforceable as a single owner.

    Implementations are not required to be thread-safe across
    different ``issue_key`` values; concurrent reviews of the same
    issue are not a supported use case (the orchestrator serializes
    them via the issue's session lock).
    """

    def release(self, issue_key: Hashable, *, reason: str) -> None:
        """Tear down the cached pair for ``issue_key``, if any.

        Closes both subprocesses and reclaims the reviewer worktree
        (via the adapter's release hook). Idempotent: releasing an
        already-absent key is a no-op so callers can put release in
        a finally block without tracking whether an acquire even
        succeeded.

        ``reason`` is recorded on the structured event emitted by
        diagnostics-aware implementations (B3). Use a short stable
        token like ``"exchange-complete"``, ``"issue-completed"``,
        ``"escalated-to-human"``, ``"reset-retry"``,
        ``"orchestrator-shutdown"``, ``"dead-on-acquire"``.
        """
        ...

    def shutdown_all(self, *, reason: str) -> None:
        """Tear down every cached pair. Wired to orchestrator shutdown.

        ``reason`` is propagated to per-pair release events.
        """
        ...

    def has_active_pair(self, issue_key: Hashable) -> bool:
        """Report whether a persistent pair is cached for ``issue_key``.

        Non-mutating counterpart to :meth:`release`: a lifecycle boundary that
        must decide whether it would tear down live review-exchange work reads
        this instead of releasing. Returning ``True`` for any cached pair (the
        exact membership :meth:`release` pops) keeps the activity check and the
        teardown reading the same state, so a freshness predicate can never miss
        a pair the reset would release.
        """
        ...
