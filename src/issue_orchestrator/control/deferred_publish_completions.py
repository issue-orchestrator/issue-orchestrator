"""Owner of deferred publish-completion state for the async observer path.

Issue #6009 / review F1. When the async publish worker processes a completion
that requests a review exchange, it may *start* that exchange in the background
and return ``review_exchange_deferred=True`` (see
``ProcessingResult.for_review_exchange_deferred``). On the **first** completion
no exchange is running at observation time, so the observer returns a normal
observed completion and ``observe_active_sessions`` finalizes the session:
removes it from ``active_sessions``, kills the terminal, releases the claim, and
records the completion. The publish worker then starts the exchange and reports a
deferral — but by then there is no active session left to re-observe, no pending
publish job, and the hidden exchange can finish without the publish path ever
being re-entered. The completion is stranded.

This owner is the single boundary that closes that gap. It remembers the
originating :class:`Session` for each in-flight publish job and, when a publish
result reports a deferral, restores that session to ``active_sessions`` so the
next observation tick re-enters the completion pipeline. From there the
finalization owner sees the now-running exchange and keeps deferring
(``RUNNING``) until it finishes, at which point a fresh publish job resumes and
publishes the work — exactly the parity the synchronous ``decide_outcome`` path
already has.

Terminal publish results (success/failure) drop the remembered session: it was
already finalized at the observation that created the job, so the normal
terminal handling in ``_poll_job_results`` owns it.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from .active_sessions import append_unique_active_sessions

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState, PublishJobResult, Session

logger = logging.getLogger(__name__)


class DeferredPublishCompletions:
    """Keeps an async review-exchange deferral re-observable (issue #6009)."""

    def __init__(self) -> None:
        # session_key -> originating Session for jobs that may still defer.
        self._pending: dict[str, "Session"] = {}

    def track(self, session: "Session") -> None:
        """Remember a session whose publish job is about to be submitted.

        Called from observation when a needs-publish completion is recorded, so
        a later ``review_exchange_deferred`` result can restore this exact
        session for re-observation.
        """
        self._pending[session.key.stable_id()] = session

    def discard(self, session_key: str) -> None:
        """Forget a tracked session.

        Used when a session is finalized without a publish job (blocked, failed,
        timed-out) and when a publish result is terminal, so a stale deferral
        registration cannot leak across attempts.
        """
        self._pending.pop(session_key, None)

    def resume_if_deferred(
        self,
        result: "PublishJobResult",
        state: "OrchestratorState",
    ) -> bool:
        """Restore the originating session when a publish result is deferred.

        Returns ``True`` when the result is a review-exchange deferral that this
        owner handled — the originating session is back in
        ``state.active_sessions`` for re-observation and the caller must skip the
        terminal result handling. Returns ``False`` for terminal results, after
        dropping any tracking for the session.
        """
        if not result.review_exchange_deferred:
            self.discard(result.session_key)
            return False

        session = self._pending.get(result.session_key)
        if session is None:
            # The job deferred but we never tracked its session. The completion
            # cannot be re-observed through this owner; log loudly so the gap is
            # visible, but do not crash the tick.
            logger.error(
                "[ASYNC] Deferred publish result for issue #%d has no tracked "
                "session (session_key=%s); completion cannot be re-observed",
                result.issue_number,
                result.session_key,
            )
            return True

        self._restore_active(session, state)
        return True

    def _restore_active(self, session: "Session", state: "OrchestratorState") -> None:
        # Route the re-add through the active-sessions owner so duplicate
        # terminals are suppressed in one place; ``added`` is empty when the
        # session is already being observed.
        added = append_unique_active_sessions(state.active_sessions, [session])
        if not added:
            return
        # The claim was released when this session was finalized at observation
        # time, so the restored session must not be subject to lease renewal —
        # that would verify the now-absent claim and tear the session down again.
        # It only needs to be re-observed: the finalization owner reads the
        # completion record from disk and keeps deferring while the background
        # exchange runs, and the resumed publish job (not this session) owns the
        # remaining work.
        session.lease_id = None
        session.lease_expires_at = None
        logger.info(
            "[ASYNC] Restored session for deferred review exchange: "
            "issue=%d session=%s",
            session.issue.number,
            session.terminal_id,
        )
