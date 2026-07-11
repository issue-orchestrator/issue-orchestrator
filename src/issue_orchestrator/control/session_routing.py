"""Orchestrator-facing session routing helpers.

These helpers bridge orchestrator state and session infrastructure. Core launch
policy stays in SessionLauncher; this module handles wrapper concerns such as
active-session registration, orphan restoration, triage dispatch, and
SessionManager adapter calls.
"""

import logging
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from ..domain.models import (
    DiscoveredFailure,
    Issue,
    PendingReview,
    PendingRetrospectiveReview,
    PendingRework,
    PendingTriageReview,
    PendingValidationRetry,
    Session,
)
from ..domain.triage_session import TriageSessionFlavor
from ..events import EventName
from ..infra.config import Config
from ..ports import EventSink, Issue as IssueProtocol, make_trace_event
from ..ports.session_runner import DiscoveredSession
from .active_sessions import append_unique_active_sessions
from .session_launcher import SessionLauncher
from .session_manager import SessionManager, SessionRef

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from ..domain.state_machines.session_machine import SessionStateMachine
    from .session_manager import SessionType
    from .session_restorer import SessionRestorer
    from .state_machine_manager import StateMachineManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _ExistingTerminalRestorationRequest:
    """Typed request to restore one known terminal from runner discovery."""

    issue_number: int
    session_name: str
    is_review: bool
    tab_name: str = ""


class TriageQueueOutcome(Enum):
    """Explicit result of asking the queue owner to enqueue triage work."""

    QUEUED = "queued"
    DUPLICATE = "duplicate"


# Bound on retryable launch failures per queued triage item. Three attempts
# ride out a transient SQLite/log/filesystem blip without relaunch-looping a
# genuinely broken input forever; after the third failure the item is dropped
# and the drop is surfaced loudly (fail-fast-but-not-silent).
TRIAGE_LAUNCH_RETRY_LIMIT = 3


class TriageRetentionOutcome(Enum):
    """Explicit result of retaining a queued triage item after a retryable failure."""

    RETAINED = "retained"
    EXHAUSTED = "exhausted"


@dataclass(frozen=True, slots=True)
class PendingSessionQueues:
    """Owner for pending session queues: launch-routing removals + triage intake.

    Triage intake is behavior-level (#6768 round 3): producers say WHICH
    variant they are queueing (batch review, failure investigation, or health
    review) and this owner constructs the ``PendingTriageReview``, applies the
    single deduplication rule (by issue number against the pending queue), and
    returns an explicit :class:`TriageQueueOutcome`. Producers never touch the
    dataclass or the state list.
    """

    state: "OrchestratorState"

    def remove_review(self, pr_number: int) -> None:
        self.state.pending_reviews[:] = [
            r for r in self.state.pending_reviews if r.pr_number != pr_number
        ]

    def remove_retrospective_review(self, issue_number: int) -> None:
        self.state.pending_retrospective_reviews[:] = [
            r
            for r in self.state.pending_retrospective_reviews
            if r.issue_number != issue_number
        ]

    def remove_rework(self, rework: PendingRework) -> None:
        self.state.pending_reworks[:] = [
            r for r in self.state.pending_reworks if r.issue_key != rework.issue_key
        ]

    def remove_validation_retry(self, issue_number: int) -> None:
        self.state.pending_validation_retries[:] = [
            queued
            for queued in self.state.pending_validation_retries
            if queued.issue_number != issue_number
        ]

    def remove_triage(self, issue_number: int) -> None:
        self.state.pending_triage_reviews[:] = [
            t
            for t in self.state.pending_triage_reviews
            if t.issue_number != issue_number
        ]

    def queue_batch_review(self, issue_number: int, title: str) -> TriageQueueOutcome:
        """Queue a threshold-created batch tracking issue (audits the PR manifest)."""
        return self._queue_triage(
            PendingTriageReview(
                issue_number, title, flavor=TriageSessionFlavor.BATCH_REVIEW
            )
        )

    def queue_health_review(self, issue_number: int, title: str) -> TriageQueueOutcome:
        """Queue an interval-created health-review anchor (ADR-0031 §4); like a
        batch review it carries no failure context (``__post_init__`` rejects one)."""
        return self._queue_triage(
            PendingTriageReview(
                issue_number, title, flavor=TriageSessionFlavor.HEALTH_REVIEW
            )
        )

    def queue_failure_investigation(
        self, issue_number: int, title: str, *, failure: DiscoveredFailure
    ) -> TriageQueueOutcome:
        """Queue a focused investigation of one failed issue.

        ``failure`` is required (non-optional): the queue item is the only
        carrier of the typed triggering-failure context once the per-tick
        ``discovered_failures`` buffer is cleared after planning (the
        launch-time board snapshot reads it from here).
        ``PendingTriageReview.__post_init__`` stays as defense-in-depth
        against untyped callers passing ``None`` anyway.
        """
        return self._queue_triage(
            PendingTriageReview(
                issue_number,
                title,
                flavor=TriageSessionFlavor.FAILURE_INVESTIGATION,
                failure=failure,
            )
        )

    def retain_triage_for_retry(self, issue_number: int) -> TriageRetentionOutcome:
        """Bounded retention of a queued triage item after a retryable launch failure.

        Failure investigations have no labels-as-truth recovery: the queued
        item is the ONLY durable record of the investigation (the per-tick
        ``discovered_failures`` buffer is cleared after planning), so a
        transient required-input prep failure must retain it for retry, not
        delete it. Retention is bounded by ``TRIAGE_LAUNCH_RETRY_LIMIT``:
        once exhausted ``EXHAUSTED`` is returned, but the item is NOT removed
        here (#6771 round 4). Destructive removal of the only durable record
        must not precede confirmation that the needs-human label/comment
        transition landed, so the launch caller commits the drop via
        ``remove_triage`` only after ``escalate_issue_needs_human`` succeeds;
        on escalation failure the item is retained and re-attempted.

        Asking to retain an item that is not queued is an invariant violation
        upstream (the launch path holds the item it just failed to launch);
        fail fast rather than silently absorbing it.
        """
        item = next(
            (
                t
                for t in self.state.pending_triage_reviews
                if t.issue_number == issue_number
            ),
            None,
        )
        if item is None:
            raise ValueError(
                f"Cannot retain triage item for issue #{issue_number} after a "
                "retryable launch failure: no such item is queued"
            )
        item.retryable_launch_failures += 1
        if item.retryable_launch_failures >= TRIAGE_LAUNCH_RETRY_LIMIT:
            return TriageRetentionOutcome.EXHAUSTED
        logger.warning(
            "[TRIAGE] Retaining %s for issue #%d after retryable launch failure "
            "%d/%d",
            item.flavor.value,
            issue_number,
            item.retryable_launch_failures,
            TRIAGE_LAUNCH_RETRY_LIMIT,
        )
        return TriageRetentionOutcome.RETAINED

    def _queue_triage(self, item: PendingTriageReview) -> TriageQueueOutcome:
        """Apply the one dedup rule (issue number vs pending queue) and enqueue."""
        queue = self.state.pending_triage_reviews
        if any(t.issue_number == item.issue_number for t in queue):
            logger.info(
                "[TRIAGE] Issue #%d already queued for triage; skipping %s request",
                item.issue_number,
                item.flavor.value,
            )
            return TriageQueueOutcome.DUPLICATE
        queue.append(item)
        logger.info(
            "[TRIAGE] Queued %s for issue #%d: %s",
            item.flavor.value,
            item.issue_number,
            item.title,
        )
        return TriageQueueOutcome.QUEUED


def orchestrator_launch_review_session(
    review: PendingReview,
    state: "OrchestratorState",
    session_launcher: SessionLauncher,
    session_restorer: "SessionRestorer",
) -> Optional[Session]:
    """Launch a review session and update orchestrator queues."""
    pending_queues = PendingSessionQueues(state)
    result = session_launcher.launch_review_session(review, state.active_sessions)
    if result.success and result.session:
        pending_queues.remove_review(review.pr_number)
        append_unique_active_sessions(state.active_sessions, [result.session])
    elif result.keep_queued:
        restored = _restore_existing_terminal(
            request=_ExistingTerminalRestorationRequest(
                issue_number=review.issue_number,
                session_name=f"review-{review.pr_number}",
                is_review=True,
                tab_name=f"Review PR #{review.pr_number}",
            ),
            state=state,
            session_launcher=session_launcher,
            session_restorer=session_restorer,
        )
        if restored:
            pending_queues.remove_review(review.pr_number)
            return restored
    else:
        pending_queues.remove_review(review.pr_number)
    return result.session if result.success else None


def orchestrator_launch_retrospective_review_session(
    review: PendingRetrospectiveReview,
    state: "OrchestratorState",
    session_launcher: SessionLauncher,
    session_restorer: "SessionRestorer",
) -> Optional[Session]:
    """Launch a retrospective review session and update orchestrator queues."""
    pending_queues = PendingSessionQueues(state)
    result = session_launcher.launch_retrospective_review_session(
        review,
        state.active_sessions,
    )
    if result.success and result.session:
        pending_queues.remove_retrospective_review(review.issue_number)
        append_unique_active_sessions(state.active_sessions, [result.session])
    elif result.keep_queued:
        restored = _restore_existing_terminal(
            request=_ExistingTerminalRestorationRequest(
                issue_number=review.issue_number,
                session_name=SessionRef.for_retrospective_review(
                    review.issue_number
                ).name,
                is_review=True,
            ),
            state=state,
            session_launcher=session_launcher,
            session_restorer=session_restorer,
        )
        if restored:
            pending_queues.remove_retrospective_review(review.issue_number)
            return restored
    else:
        pending_queues.remove_retrospective_review(review.issue_number)
    return result.session if result.success else None


def orchestrator_launch_rework_session(
    rework: PendingRework,
    state: "OrchestratorState",
    session_launcher: SessionLauncher,
    session_restorer: "SessionRestorer",
) -> Optional[Session]:
    """Launch a rework session and update orchestrator queues."""
    pending_queues = PendingSessionQueues(state)
    result = session_launcher.launch_rework_session(rework, state.active_sessions)
    if result.success and result.session:
        pending_queues.remove_rework(rework)
        append_unique_active_sessions(state.active_sessions, [result.session])
    elif result.keep_queued:
        issue_number = rework.resolve_issue_number()
        if issue_number is None:
            logger.warning("[ORPHAN] Rework missing issue number: %s", rework.issue_key)
            return None
        restored = _restore_existing_terminal(
            request=_ExistingTerminalRestorationRequest(
                issue_number=issue_number,
                session_name=f"rework-{issue_number}",
                is_review=False,
            ),
            state=state,
            session_launcher=session_launcher,
            session_restorer=session_restorer,
        )
        if restored:
            pending_queues.remove_rework(rework)
            return restored
    else:
        pending_queues.remove_rework(rework)
    return result.session if result.success else None


def orchestrator_launch_validation_retry_session(
    retry: PendingValidationRetry,
    state: "OrchestratorState",
    session_launcher: SessionLauncher,
    session_restorer: "SessionRestorer",
) -> Optional[Session]:
    """Launch a validation retry session and update retry queue tracking."""
    pending_queues = PendingSessionQueues(state)
    result = session_launcher.launch_validation_retry_session(
        retry, state.active_sessions
    )
    if result.success and result.session:
        pending_queues.remove_validation_retry(retry.issue_number)
        append_unique_active_sessions(state.active_sessions, [result.session])
    elif result.keep_queued:
        restored = _restore_existing_terminal(
            request=_ExistingTerminalRestorationRequest(
                issue_number=retry.issue_number,
                session_name=f"issue-{retry.issue_number}",
                is_review=False,
            ),
            state=state,
            session_launcher=session_launcher,
            session_restorer=session_restorer,
        )
        if restored:
            pending_queues.remove_validation_retry(retry.issue_number)
            return restored
    return result.session if result.success else None


def orchestrator_launch_triage_session(
    triage: PendingTriageReview,
    state: "OrchestratorState",
    config: Config,
    session_launcher: SessionLauncher,
    session_restorer: "SessionRestorer",
) -> Optional[Session]:
    """Launch a queued triage session and update orchestrator queues.

    The pending-triage queue carries every triage variant — threshold-created
    batch tracking issues, interval-created health-review anchors (ADR-0031
    §4), and failure investigations — and the planner launches them
    through this path before ordinary issue pickup. The producer boundary that
    queued the item declared its flavor; forward it verbatim (#6768 B5:
    hard-coding one flavor here made batch reviews skip manifest prep).

    Queue lifecycle mirrors :func:`orchestrator_launch_review_session`
    (#6768 round 4 — a launched item previously stayed queued and was
    relaunched every tick): the item is removed through the owning
    :class:`PendingSessionQueues` on success, on restore of an existing
    terminal, and on permanent launch failure (labels-as-truth recovers a
    dropped batch at startup; a dropped investigation is a best-effort audit).
    It is retained in exactly two cases:

    - ``keep_queued`` — an existing terminal that could not be restored yet;
    - ``retry_queued`` — required-input prep failed transiently BEFORE the
      session started. For failure investigations the queued item is the only
      record of the investigation (no labels-as-truth recovery), so one
      transient SQLite/log/filesystem error must not delete it. Retention is
      bounded by the queue owner (``retain_triage_for_retry``); on exhaustion
      the item is dropped as a DURABLE needs-human transition — the
      needs-human label plus an explanatory comment applied through the
      launcher's owning action boundary, then the ``ISSUE_NEEDS_HUMAN``
      event (#6771 round 3: a log line and an event alone do not survive an
      orchestrator restart; labels are the source of truth).
    """
    agent = config.triage_review_agent
    if not agent or agent not in config.agents:
        raise ValueError(f"Invalid triage agent: {agent}")
    pending_queues = PendingSessionQueues(state)
    result = session_launcher.launch_issue_session(
        Issue(triage.issue_number, triage.title, [agent]),
        state.active_sessions,
        triage_flavor=triage.flavor,
    )
    if result.success and result.session:
        _clear_stale_needs_human_on_launch(triage, session_launcher)
        pending_queues.remove_triage(triage.issue_number)
        append_unique_active_sessions(state.active_sessions, [result.session])
    elif result.keep_queued:
        restored = _restore_existing_terminal(
            request=_ExistingTerminalRestorationRequest(
                issue_number=triage.issue_number,
                session_name=f"issue-{triage.issue_number}",
                is_review=False,
            ),
            state=state,
            session_launcher=session_launcher,
            session_restorer=session_restorer,
        )
        if restored:
            _clear_stale_needs_human_on_launch(triage, session_launcher)
            pending_queues.remove_triage(triage.issue_number)
            return restored
    elif result.retry_queued:
        outcome = pending_queues.retain_triage_for_retry(triage.issue_number)
        if outcome is TriageRetentionOutcome.EXHAUSTED:
            _commit_or_retain_dropped_triage(
                triage, result.reason, session_launcher, pending_queues
            )
    else:
        pending_queues.remove_triage(triage.issue_number)
    return result.session if result.success else None


def _commit_or_retain_dropped_triage(
    triage: PendingTriageReview,
    last_error: str,
    session_launcher: SessionLauncher,
    pending_queues: PendingSessionQueues,
) -> None:
    """Commit protocol for a triage item that exhausted its launch retries.

    The queued item is the ONLY durable record of a failure investigation, so
    it is dropped ONLY after ``escalate_issue_needs_human`` confirms the
    needs-human label + comment landed on the issue (#6771 round 4). The
    ordering is the whole point: destructive queue removal must not precede
    confirmation of the durable transition. If the escalation mutation fails,
    the item is RETAINED as a recoverable record and re-attempted on a later
    tick (either the launch prep recovers and the investigation runs, or a
    subsequent exhaustion re-attempts the escalation until it commits); the
    failure is surfaced loudly and no ISSUE_NEEDS_HUMAN event is emitted for
    the non-transition.
    """
    logger.error(
        "[TRIAGE] Escalating dropped %s for issue #%d after %d retryable "
        "launch failures: %s",
        triage.flavor.value,
        triage.issue_number,
        TRIAGE_LAUNCH_RETRY_LIMIT,
        last_error,
    )
    comment = (
        f"**Queued {triage.flavor.value} dropped after "
        f"{TRIAGE_LAUNCH_RETRY_LIMIT} launch failures**\n\n"
        "The orchestrator could not prepare the required inputs for this "
        f"triage session {TRIAGE_LAUNCH_RETRY_LIMIT} times in a row, so the "
        "queued item was dropped and will not retry on its own.\n\n"
        f"Last error: {last_error}\n\n"
        "A human needs to fix the launch failure and re-queue (or close) "
        "this investigation."
    )
    result = session_launcher.escalate_issue_needs_human(
        issue_number=triage.issue_number,
        reason="triage launch retries exhausted",
        comment=comment,
        context="triage_launch_retry_exhausted",
        event_data={
            "issue_number": triage.issue_number,
            "issue_title": triage.title,
            "reason": (
                f"triage launch failed {TRIAGE_LAUNCH_RETRY_LIMIT} "
                f"times on required-input preparation; dropping "
                f"queued {triage.flavor.value}: {last_error}"
            ),
        },
    )
    if result.committed:
        pending_queues.remove_triage(triage.issue_number)
        return
    # Not committed: retain the recoverable record. Remember any partially
    # applied needs-human label so a later successful launch can clear it
    # (#6771 round 5) — once applied it stays tracked until cleared or committed.
    triage.needs_human_escalation_incomplete = (
        triage.needs_human_escalation_incomplete or result.label_applied
    )
    logger.error(
        "[TRIAGE] Durable needs-human escalation did NOT commit for issue "
        "#%d; retaining the queued %s as the only recoverable record "
        "(will re-attempt on a later tick)",
        triage.issue_number,
        triage.flavor.value,
    )


def _clear_stale_needs_human_on_launch(
    triage: PendingTriageReview, session_launcher: SessionLauncher
) -> None:
    """Clear a needs-human label an incomplete escalation left behind (#6771 r5).

    A prior tick may have exhausted launch retries and applied the needs-human
    source-of-truth label but failed to commit the escalation (comment failed).
    If prep then recovers and the investigation launches, that label is stale
    and contradicts the running work, so the successful-launch path clears it
    through the launcher's owning action boundary."""
    if triage.needs_human_escalation_incomplete:
        session_launcher.clear_needs_human_label(triage.issue_number)
        triage.needs_human_escalation_incomplete = False


def session_launcher_callback(
    session_type: "SessionType",
    number: int,
    launch_issue_fn: Callable[[int], Optional[Session]],
    launch_review_fn: Callable[[int], Optional[Session]],
    launch_retrospective_review_fn: Callable[[int], Optional[Session]],
    launch_rework_fn: Callable[[int], Optional[Session]],
    launch_triage_fn: Callable[[int], Optional[Session]],
) -> Optional[Session]:
    """Route SessionManager launch callbacks by session type."""
    from .session_manager import SessionType

    handlers = {
        SessionType.ISSUE: launch_issue_fn,
        SessionType.REVIEW: launch_review_fn,
        SessionType.RETROSPECTIVE_REVIEW: launch_retrospective_review_fn,
        SessionType.REWORK: launch_rework_fn,
        SessionType.TRIAGE: launch_triage_fn,
    }
    return handlers[session_type](number)


def restore_running_sessions(
    running: list["DiscoveredSession"],
    active_sessions: list[Session],
    session_restorer: "SessionRestorer",
) -> list[Session]:
    """Restore running terminal sessions into active-session tracking."""
    restored = session_restorer.restore_sessions(running, active_sessions)
    added = append_unique_active_sessions(active_sessions, restored)
    if added:
        logger.info(
            "[ORPHAN] Restored %d running terminal session(s): %s",
            len(added),
            ", ".join(str(session.terminal_id) for session in added),
        )
    elif running:
        logger.warning(
            "[ORPHAN] Found %d running terminal session(s), but none could be restored",
            len(running),
        )
    return added


def parse_session_ref(
    session_name: str,
    operation: str,
    events: EventSink,
):
    """Parse a session ref and publish a trace event on invalid names."""
    from .session_manager import SessionRef

    try:
        return SessionRef.from_name(session_name)
    except ValueError as e:
        events.publish(
            make_trace_event(
                EventName.SESSION_NAME_PARSE_ERROR,
                {"session_name": session_name, "error": str(e)},
            )
        )
        raise


def create_session(
    name: str,
    cmd: str,
    wd: Path,
    title: str | None,
    session_manager: SessionManager,
    events: EventSink,
) -> bool:
    """Create a terminal session through SessionManager."""
    from .session_manager import SessionContext

    ref = parse_session_ref(name, "create", events)
    return session_manager.start(
        SessionContext(ref=ref, command=cmd, working_dir=wd, title=title)
    )


def session_exists(
    name: str, session_manager: SessionManager, events: EventSink
) -> bool:
    """Check whether a terminal session exists through SessionManager."""
    return session_manager.exists(parse_session_ref(name, "exists", events))


def kill_session(name: str, session_manager: SessionManager, events: EventSink) -> None:
    """Stop a terminal session through SessionManager."""
    session_manager.stop(parse_session_ref(name, "kill", events))


def get_session_machine(
    name: str,
    n: int,
    timeout: int,
    state_machines: "StateMachineManager",
) -> Optional["SessionStateMachine"]:
    """Get or create the state machine for a terminal session."""
    return state_machines.get_session_machine(name, n, timeout)


def orchestrator_launch_session(
    issue: IssueProtocol,
    state: "OrchestratorState",
    session_launcher: SessionLauncher,
    session_restorer: "SessionRestorer | None" = None,
    *,
    triage_flavor: TriageSessionFlavor | None = None,
) -> Optional[Session]:
    """Launch an issue session and update active-session tracking."""
    result = session_launcher.launch_issue_session(
        issue, state.active_sessions, triage_flavor=triage_flavor
    )
    if result.success and result.session:
        append_unique_active_sessions(state.active_sessions, [result.session])
    elif result.keep_queued and session_restorer is not None:
        restored = _restore_existing_terminal(
            request=_ExistingTerminalRestorationRequest(
                issue_number=issue.number,
                session_name=f"issue-{issue.number}",
                is_review=False,
            ),
            state=state,
            session_launcher=session_launcher,
            session_restorer=session_restorer,
        )
        if restored:
            return restored
    return result.session if result.success else None


def _restore_existing_terminal(
    *,
    request: _ExistingTerminalRestorationRequest,
    state: "OrchestratorState",
    session_launcher: SessionLauncher,
    session_restorer: "SessionRestorer",
) -> Optional[Session]:
    discovered = _discover_existing_terminal(
        request=request,
        session_launcher=session_launcher,
        session_restorer=session_restorer,
    )
    if discovered is None:
        _log_unrestorable_existing_terminal(request.session_name)
        return None

    run_dir = _recorded_run_dir_from_discovered(discovered, request.session_name)
    if run_dir is None:
        return None

    restored = session_restorer.restore_known_terminal(
        issue_number=request.issue_number,
        session_name=request.session_name,
        run_dir=run_dir,
        is_review=request.is_review,
        already_tracked=list(state.active_sessions),
        tab_name=request.tab_name,
    )
    added = append_unique_active_sessions(state.active_sessions, restored)
    if not added:
        _log_unrestorable_existing_terminal(request.session_name)
        return None
    logger.info(
        "[ORPHAN] Restored existing terminal %s from discovered run assets: %s",
        request.session_name,
        run_dir,
    )
    return added[0]


def _discover_existing_terminal(
    *,
    request: _ExistingTerminalRestorationRequest,
    session_launcher: SessionLauncher,
    session_restorer: "SessionRestorer",
) -> "DiscoveredSession | None":
    try:
        running = session_launcher.session_manager.runner.discover_running_sessions()
    except Exception:
        logger.exception(
            "[ORPHAN] Failed to discover running terminal sessions for %s",
            request.session_name,
        )
        return None

    for raw_session_info in running:
        session_info = _discovered_session_from_raw(raw_session_info)
        if session_info is None:
            continue
        if _matches_existing_terminal(
            session_info=session_info,
            request=request,
            session_restorer=session_restorer,
        ):
            return session_info
    return None


def _discovered_session_from_raw(raw: object) -> DiscoveredSession | None:
    if not isinstance(raw, dict):
        return None

    raw_issue_number = raw.get("issue_number")
    raw_tab_name = raw.get("tab_name")
    raw_is_review = raw.get("is_review")
    raw_run_dir = raw.get("run_dir")
    if isinstance(raw_issue_number, bool) or not isinstance(raw_issue_number, int):
        return None
    if not isinstance(raw_tab_name, str):
        return None
    if not isinstance(raw_is_review, bool):
        return None
    run_dir = raw_run_dir if isinstance(raw_run_dir, str) else ""
    raw_session_name = raw.get("session_name")
    if isinstance(raw_session_name, str):
        return DiscoveredSession(
            issue_number=raw_issue_number,
            tab_name=raw_tab_name,
            is_review=raw_is_review,
            run_dir=run_dir,
            session_name=raw_session_name,
        )
    return DiscoveredSession(
        issue_number=raw_issue_number,
        tab_name=raw_tab_name,
        is_review=raw_is_review,
        run_dir=run_dir,
    )


def _matches_existing_terminal(
    *,
    session_info: "DiscoveredSession",
    request: _ExistingTerminalRestorationRequest,
    session_restorer: "SessionRestorer",
) -> bool:
    discovered_names = {
        str(session_info.get("session_name") or ""),
        str(session_info.get("tab_name") or ""),
    }
    try:
        discovered_names.add(session_restorer.canonical_terminal_id(session_info))
    except Exception:
        logger.debug(
            "[ORPHAN] Could not derive canonical terminal id from discovered session",
            exc_info=True,
        )
    return request.session_name in discovered_names


def _recorded_run_dir_from_discovered(
    session_info: "DiscoveredSession",
    session_name: str,
) -> Path | None:
    raw: object = session_info.get("run_dir")
    if type(raw) is not str or not raw.strip():
        logger.warning(
            "[ORPHAN] Existing terminal %s has no recorded run_dir from runner discovery",
            session_name,
        )
        return None
    run_dir = Path(raw)
    if not run_dir.is_absolute():
        logger.warning(
            "[ORPHAN] Existing terminal %s reported non-absolute run_dir: %s",
            session_name,
            run_dir,
        )
        return None
    return run_dir


def _log_unrestorable_existing_terminal(session_name: str) -> None:
    logger.warning(
        "[ORPHAN] Existing terminal %s cannot be restored from launch routing; "
        "active restoration requires discovered run assets",
        session_name,
    )
