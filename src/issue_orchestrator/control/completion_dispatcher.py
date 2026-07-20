"""Owner for *where* a session's completion decision runs.

A terminated session is finalized by ``SessionController.decide_outcome`` —
which, for a completed coding session, runs the publish gate (a real test
suite, ~100s), ``git push`` (~20s) and PR creation. Historically this ran
inline on the orchestrator's tick thread, so a single publish froze the whole
loop: the heartbeat (``last_tick_completed_at``) stopped advancing, every
dashboard card went "stalled", and no other session was serviced for the
duration.

``decide_outcome`` is effectively functional — it reads its inputs, drives the
per-worktree git/GitHub I/O, and returns a ``SessionDecision`` describing any
shared state effects (session completion, provider-circuit updates) without
applying them. The tick thread applies those effects when the dispatcher drains
the decision. That makes it safe to run off the tick thread.
This module owns that choice behind one seam so the tick loop stays identical
in shape:

    for session in dispatchable:        dispatcher.dispatch(session, decide_fn)
    for done in dispatcher.drain():     apply(done.session, done.decision)

``SynchronousCompletionDispatcher`` runs the decision inline (unchanged
behavior — used by tests and any caller that injects nothing). The background
dispatcher offloads the decision to a :class:`BackgroundJobRunner` and hands the
result back on a later tick, keeping the heartbeat alive while a publish runs.
``decide_outcome`` returning ``RUNNING`` (a still-deferred review exchange) is a
normal result the caller re-evaluates next tick — it is not a completion.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from threading import Lock
from typing import TYPE_CHECKING, Protocol

from ..ports.background_job import BackgroundJobRunner

if TYPE_CHECKING:
    from ..domain.models import Session
    from .session_controller import SessionDecision


@dataclass(frozen=True)
class CompletedDecision:
    """A finished completion decision ready for the tick thread to apply.

    Exactly one of ``decision`` / ``error`` is set. ``error`` carries an
    exception raised while deciding the outcome so the caller can surface it on
    the tick thread (preserving the fail-loud behavior of the old inline path).
    """

    session: "Session"
    decision: "SessionDecision | None"
    error: BaseException | None


class CompletionDispatcher(Protocol):
    """Strategy for running a session's completion decision."""

    def in_flight(self, terminal_id: str) -> bool:
        """True iff a decision for this session is already running (don't re-dispatch)."""
        ...

    def dispatch(self, session: "Session", decide: "Callable[[], SessionDecision]") -> None:
        """Run (or schedule) ``decide`` for ``session``."""
        ...

    def drain(self) -> list[CompletedDecision]:
        """Return and forget every decision that has finished since the last call."""
        ...


class SynchronousCompletionDispatcher:
    """Run the decision inline; ``drain`` returns it within the same tick.

    This preserves the original one-tick completion behavior and is the default
    when no background runner is injected (tests, and callers that opt out).
    """

    def __init__(self) -> None:
        self._done: list[CompletedDecision] = []

    def in_flight(self, terminal_id: str) -> bool:
        del terminal_id
        return False

    def dispatch(self, session: "Session", decide: "Callable[[], SessionDecision]") -> None:
        # No try/except: a decide error propagates here exactly as it did on the
        # old inline path. (The background dispatcher instead captures errors via
        # the runner and surfaces them as CompletedDecision.error on drain.)
        self._done.append(CompletedDecision(session=session, decision=decide(), error=None))

    def drain(self) -> list[CompletedDecision]:
        done = self._done
        self._done = []
        return done


class BackgroundCompletionDispatcher:
    """Run the decision on a :class:`BackgroundJobRunner`, keyed by terminal id.

    The dispatcher OWNS the submitted-through-drained lifecycle: ``dispatch``
    records the session under its terminal id and ``drain`` removes it only when
    the finished decision is handed back to the tick thread. ``in_flight`` reads
    that ownership, NOT the runner's execution status.

    This matters because ``BackgroundJobRunner.is_running`` reflects only whether
    the worker is *currently executing* (the thread adapter uses
    ``Thread.is_alive()``). A worker can finish after a tick takes its drain
    snapshot but before that same tick's ``in_flight`` check — at which point
    ``is_running`` is already ``False`` while the completed decision is still
    queued for the next drain. If ``in_flight`` followed ``is_running`` the tick
    would re-dispatch the still-undrained session, running a **duplicate**
    decision (a second publish gate + push + PR) alongside the pending result.
    Anchoring ``in_flight`` to owned state through drain closes that window.

    The decision value is stashed by the worker before it returns, so it is
    always present by the time the runner reports the job complete in ``drain``.
    """

    def __init__(self, runner: BackgroundJobRunner) -> None:
        self._runner = runner
        self._lock = Lock()
        self._sessions: dict[str, Session] = {}
        self._results: dict[str, SessionDecision] = {}

    def in_flight(self, terminal_id: str) -> bool:
        # Owned, not inferred: in flight from dispatch until drain removes it, so
        # a worker finishing between a tick's drain snapshot and its in_flight
        # check cannot be re-dispatched into a duplicate decision.
        with self._lock:
            return terminal_id in self._sessions

    def dispatch(self, session: "Session", decide: "Callable[[], SessionDecision]") -> None:
        terminal_id = session.terminal_id

        def run() -> None:
            decision = decide()
            with self._lock:
                self._results[terminal_id] = decision

        if self._runner.submit(terminal_id, run):
            with self._lock:
                self._sessions[terminal_id] = session

    def drain(self) -> list[CompletedDecision]:
        out: list[CompletedDecision] = []
        for job in self._runner.drain_completed():
            with self._lock:
                session = self._sessions.pop(job.job_id, None)
                decision = self._results.pop(job.job_id, None)
            if session is None:
                # A job_id we didn't dispatch (or already drained) — ignore.
                continue
            out.append(CompletedDecision(session=session, decision=decision, error=job.error))
        return out
