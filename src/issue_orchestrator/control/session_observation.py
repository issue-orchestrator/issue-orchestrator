"""Active session observation helpers.

This module owns the fast observation phase of session completion handling:
detect terminated sessions, collect completion facts, and release per-session
runtime resources. It deliberately does not publish PRs, mutate labels, or
perform worktree cleanup; those policies live in planning/execution phases.
"""

import logging
import time
from typing import TYPE_CHECKING, Callable, Optional

from ..domain.models import DiscoveredFailure, Session, SessionStatus
from ..events import EventName
from ..observation.observation import SessionObservation
from ..ports import EventSink
from ..ports.event_sink import make_trace_event
from ..ports.provider_resilience import ProviderErrorType
from .active_sessions import has_active_terminal

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from ..observation.observer import SessionObserver
    from ..ports.claim_manager import ClaimManager
    from .completion_observer import CompletionObserver, ObservationDecision
    from .deferred_publish_completions import DeferredPublishCompletions
    from .provider_resilience import ProviderResilienceManager

logger = logging.getLogger(__name__)


def _log_observation(session: Session, decision: "ObservationDecision") -> None:
    logger.info(
        "[OBSERVE] Session completed: session=%s issue=%d status=%s has_completion=%s",
        session.terminal_id,
        session.issue.number,
        decision.status.value,
        decision.observed is not None,
    )


def _defer_active_session(session: Session, decision: "ObservationDecision") -> None:
    """Keep a session active after a deferred (RUNNING) completion decision.

    Parity with the synchronous ``process_active_sessions`` guard: once the
    terminal is observed, a RUNNING decision means the completion is deferred
    (e.g. the review exchange is still running in the background), not final.
    The session stays in ``state.active_sessions`` so the next tick re-observes
    it; nothing is removed, killed, released, or recorded as completed (#6009).
    """
    logger.info(
        "[OBSERVE] Session deferred after completion decision: "
        "session=%s issue=%d reason=%s",
        session.terminal_id,
        session.issue.number,
        decision.reason,
    )


def _preserve_active_after_observation(decision: "ObservationDecision") -> bool:
    return decision.status == SessionStatus.RUNNING


def _publish_observation_event(
    session: Session,
    decision: "ObservationDecision",
    events: Optional[EventSink],
) -> None:
    if not events:
        return
    events.publish(make_trace_event(EventName.OBSERVATION_RESULT, {
        "issue_number": session.issue.number,
        "session_name": session.terminal_id,
        "status": decision.status.value,
        "has_completion": decision.observed is not None,
        "recovered_from_timeout": decision.recovered_from_timeout,
    }))


def _remove_active_session(state: "OrchestratorState", session: Session) -> None:
    state.active_sessions = [s for s in state.active_sessions if s.terminal_id != session.terminal_id]


def _kill_session(kill_session_fn: Callable[[str], None], session: Session) -> None:
    try:
        kill_session_fn(session.terminal_id)
        logger.debug("[OBSERVE] Killed terminal: %s", session.terminal_id)
    except Exception as exc:
        logger.warning("[OBSERVE] Failed to kill terminal %s: %s", session.terminal_id, exc)


def _release_claim_if_needed(
    session: Session,
    decision: "ObservationDecision",
    claim_manager: Optional["ClaimManager"],
    events: Optional[EventSink],
) -> None:
    if not claim_manager or not session.lease_id:
        return
    try:
        claim_manager.release_claim(session.issue.number, session.lease_id)
        logger.info(
            "[OBSERVE] Released claim for issue #%d: lease_id=%s",
            session.issue.number,
            session.lease_id,
        )
        if events:
            events.publish(make_trace_event(
                EventName.CLAIM_RELEASED,
                {
                    "issue_number": session.issue.number,
                    "lease_id": session.lease_id,
                    "status": decision.status.value,
                },
            ))
    except Exception as exc:
        logger.warning(
            "[OBSERVE] Failed to release claim for issue #%d: %s",
            session.issue.number,
            exc,
        )


def _update_provider_resilience(
    decision: "ObservationDecision",
    provider_resilience: Optional["ProviderResilienceManager"],
) -> None:
    if not provider_resilience or not decision.provider_status:
        return
    provider = decision.provider_status.provider
    if decision.provider_status.succeeded:
        provider_resilience.record_success(provider)
        return
    if decision.provider_status.error_type == ProviderErrorType.TRANSIENT:
        provider_resilience.record_transient_failure(
            provider,
            error_summary=decision.provider_status.last_error_summary,
            attempts=decision.provider_status.attempts,
        )


def _record_observed_completion(
    state: "OrchestratorState",
    session: Session,
    decision: "ObservationDecision",
) -> None:
    if decision.observed:
        state.observed_completions.append(decision.observed)
        logger.info(
            "[OBSERVE] Collected completion: issue=%d outcome=%s needs_publish=%s",
            session.issue.number,
            decision.observed.outcome,
            decision.observed.needs_publish,
        )
        return
    state.discovered_failures.append(DiscoveredFailure(
        session.issue.number,
        session.issue.title,
        _observed_failure_reason(decision),
    ))
    state.failed_this_cycle.add(session.issue.number)
    logger.warning(
        "[OBSERVE] No completion record for issue #%d, status=%s",
        session.issue.number,
        decision.status.value,
    )


def _observed_failure_reason(decision: "ObservationDecision") -> str:
    load_result = decision.completion_load_result
    if load_result is not None and load_result.invalid:
        return "invalid_completion_record"
    return decision.status.value


def _track_deferred_publish_candidate(
    session: Session,
    decision: "ObservationDecision",
    deferred_publish: Optional["DeferredPublishCompletions"],
) -> None:
    """Remember (or forget) a finalized session for async deferral recovery.

    A finalized completion that ``needs_publish`` will get a background publish
    job, and that job may report ``review_exchange_deferred`` once it starts the
    review exchange. Hand the session to the deferred-publish owner so the
    deferral can restore it for re-observation (issue #6009). Sessions finalized
    without a publish job (blocked/failed/timed-out) instead drop any stale
    registration so it cannot leak across attempts.
    """
    if deferred_publish is None:
        return
    if decision.observed is not None and decision.observed.needs_publish:
        deferred_publish.track(session)
    else:
        deferred_publish.discard(session.key.stable_id())


def _warn_if_slow(obs_elapsed: float, session: Session) -> None:
    if obs_elapsed <= 1.0:
        return
    logger.warning(
        "[OBSERVE] Session observation took %.1fs (session=%s issue=%s) - should be <1s",
        obs_elapsed,
        session.terminal_id,
        session.issue.number,
    )


def _observe_active_session(
    state: "OrchestratorState",
    session: Session,
    observer: "SessionObserver",
    completion_observer: "CompletionObserver",
    kill_session_fn: Callable[[str], None],
    claim_manager: Optional["ClaimManager"],
    events: Optional[EventSink],
    provider_resilience: Optional["ProviderResilienceManager"],
    deferred_publish: Optional["DeferredPublishCompletions"],
) -> None:
    obs_start = time.monotonic()
    obs = observer.observe_session(session)
    if obs.observation == SessionObservation.RUNNING:
        return

    decision = completion_observer.observe_completion(session, obs)

    if _preserve_active_after_observation(decision):
        # Deferred completion (e.g. review exchange still running). Preserve the
        # session for re-observation; do not finalize it.
        _defer_active_session(session, decision)
    else:
        _log_observation(session, decision)
        _publish_observation_event(session, decision, events)
        _remove_active_session(state, session)
        _kill_session(kill_session_fn, session)
        _release_claim_if_needed(session, decision, claim_manager, events)
        _update_provider_resilience(decision, provider_resilience)
        _record_observed_completion(state, session, decision)
        _track_deferred_publish_candidate(session, decision, deferred_publish)

    obs_elapsed = time.monotonic() - obs_start
    _warn_if_slow(obs_elapsed, session)


def observe_active_sessions(
    state: "OrchestratorState",
    observer: "SessionObserver",
    completion_observer: "CompletionObserver",
    kill_session_fn: Callable[[str], None],
    claim_manager: Optional["ClaimManager"] = None,
    events: Optional[EventSink] = None,
    provider_resilience: Optional["ProviderResilienceManager"] = None,
    deferred_publish: Optional["DeferredPublishCompletions"] = None,
) -> None:
    """Observe active sessions and collect completion facts (fast, no I/O-heavy operations).

    This is Phase 1 of the async completion flow:
    1. Observe each session to detect termination
    2. For terminated sessions, use CompletionObserver to read completion.json
    3. Collect ObservedCompletion facts into state.observed_completions
    4. Remove sessions from active tracking and kill terminals

    The Planner will see observed_completions and:
    - Plan immediate label updates (remove in-progress, add pr-pending/blocked)
    - Create PublishJobs for background execution

    Args:
        state: Orchestrator state (active_sessions, observed_completions)
        observer: Session observer for checking session status
        completion_observer: For reading completion.json (no execution)
        kill_session_fn: Function to kill terminal session
        claim_manager: Optional ClaimManager for releasing claims
        events: Optional EventSink for emitting events
        provider_resilience: Optional provider resilience manager for failure tracking
        deferred_publish: Optional owner that remembers finalized needs-publish
            sessions so an async ``review_exchange_deferred`` result can restore
            them for re-observation (issue #6009)
    """
    for session in list(state.active_sessions):
        # Snapshot iteration is mutation-safe; the live check filters any
        # duplicate terminal already removed by an earlier snapshot entry.
        if not has_active_terminal(state.active_sessions, session.terminal_id):
            logger.debug(
                "[OBSERVE] Skipping stale active-session snapshot entry: %s",
                session.terminal_id,
            )
            continue
        _observe_active_session(
            state=state,
            session=session,
            observer=observer,
            completion_observer=completion_observer,
            kill_session_fn=kill_session_fn,
            claim_manager=claim_manager,
            events=events,
            provider_resilience=provider_resilience,
            deferred_publish=deferred_publish,
        )
