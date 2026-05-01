"""Orchestrator-facing session routing helpers.

These helpers bridge orchestrator state and session infrastructure. Core launch
policy stays in SessionLauncher; this module handles wrapper concerns such as
active-session registration, orphan restoration, triage dispatch, and
SessionManager adapter calls.
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from ..domain.models import (
    Issue,
    PendingReview,
    PendingRework,
    PendingTriageReview,
    PendingValidationRetry,
    Session,
)
from ..events import EventName
from ..infra.config import Config
from ..ports import EventSink, Issue as IssueProtocol, make_trace_event
from .active_sessions import append_unique_active_sessions
from .session_launcher import SessionLauncher
from .session_manager import SessionManager

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from ..domain.state_machines.session_machine import SessionStateMachine
    from ..ports.session_runner import DiscoveredSession
    from .session_manager import SessionType
    from .session_restorer import SessionRestorer
    from .state_machine_manager import StateMachineManager

logger = logging.getLogger(__name__)


def orchestrator_launch_review_session(
    review: PendingReview,
    state: "OrchestratorState",
    session_launcher: SessionLauncher,
    session_restorer: "SessionRestorer",
) -> Optional[Session]:
    """Launch a review session and update orchestrator queues."""
    result = session_launcher.launch_review_session(review, state.active_sessions)
    state.pending_reviews = [r for r in state.pending_reviews if r.pr_number != review.pr_number]
    if result.success and result.session:
        append_unique_active_sessions(state.active_sessions, [result.session])
    elif result.keep_queued:
        session_name = f"review-{review.pr_number}"
        restored = session_restorer.restore_known_terminal(
            issue_number=review.issue_number,
            session_name=session_name,
            is_review=True,
            already_tracked=state.active_sessions,
        )
        if restored:
            append_unique_active_sessions(state.active_sessions, restored)
            logger.info("[ORPHAN] Restored tracking for existing terminal: %s", session_name)
        else:
            logger.warning("[ORPHAN] Couldn't restore session %s - terminal may be stale", session_name)
    return result.session if result.success else None


def orchestrator_launch_rework_session(
    rework: PendingRework,
    state: "OrchestratorState",
    session_launcher: SessionLauncher,
    session_restorer: "SessionRestorer",
) -> Optional[Session]:
    """Launch a rework session and update orchestrator queues."""
    result = session_launcher.launch_rework_session(rework, state.active_sessions)
    state.pending_reworks = [r for r in state.pending_reworks if r.issue_key != rework.issue_key]
    if result.success and result.session:
        append_unique_active_sessions(state.active_sessions, [result.session])
    elif result.keep_queued:
        issue_number = rework.resolve_issue_number()
        if issue_number is None:
            logger.warning("[ORPHAN] Rework missing issue number: %s", rework.issue_key)
            return None
        session_name = f"rework-{issue_number}"
        restored = session_restorer.restore_known_terminal(
            issue_number=issue_number,
            session_name=session_name,
            is_review=False,
            already_tracked=state.active_sessions,
        )
        if restored:
            append_unique_active_sessions(state.active_sessions, restored)
            logger.info("[ORPHAN] Restored tracking for existing terminal: %s", session_name)
        else:
            logger.warning("[ORPHAN] Couldn't restore session %s - terminal may be stale", session_name)
    return result.session if result.success else None


def orchestrator_launch_validation_retry_session(
    retry: PendingValidationRetry,
    state: "OrchestratorState",
    session_launcher: SessionLauncher,
    session_restorer: "SessionRestorer",
) -> Optional[Session]:
    """Launch a validation retry session and update retry queue tracking."""
    result = session_launcher.launch_validation_retry_session(retry, state.active_sessions)
    if result.success and result.session:
        state.pending_validation_retries = [
            queued for queued in state.pending_validation_retries
            if queued.issue_number != retry.issue_number
        ]
        append_unique_active_sessions(state.active_sessions, [result.session])
    elif result.keep_queued:
        session_name = f"issue-{retry.issue_number}"
        restored = session_restorer.restore_known_terminal(
            issue_number=retry.issue_number,
            session_name=session_name,
            is_review=False,
            already_tracked=state.active_sessions,
        )
        if restored:
            append_unique_active_sessions(state.active_sessions, restored)
            logger.info("[ORPHAN] Restored tracking for existing terminal: %s", session_name)
        else:
            logger.warning("[ORPHAN] Couldn't restore session %s - terminal may be stale", session_name)
    return result.session if result.success else None


def launch_triage_session(
    triage: PendingTriageReview,
    config: Config,
    launch_session_fn: Callable[[Issue], Optional[Session]],
) -> None:
    """Launch a triage review session."""
    agent = config.triage_review_agent
    if not agent or agent not in config.agents:
        raise ValueError(f"Invalid triage agent: {agent}")
    launch_session_fn(Issue(triage.issue_number, triage.title, [agent]))


def session_launcher_callback(
    session_type: "SessionType",
    number: int,
    launch_issue_fn: Callable[[int], Optional[Session]],
    launch_review_fn: Callable[[int], Optional[Session]],
    launch_rework_fn: Callable[[int], Optional[Session]],
    launch_triage_fn: Callable[[int], Optional[Session]],
) -> Optional[Session]:
    """Route SessionManager launch callbacks by session type."""
    from .session_manager import SessionType

    handlers = {
        SessionType.ISSUE: launch_issue_fn,
        SessionType.REVIEW: launch_review_fn,
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
    append_unique_active_sessions(active_sessions, restored)
    if restored:
        logger.info(
            "[ORPHAN] Restored %d running terminal session(s): %s",
            len(restored),
            ", ".join(str(getattr(s, "terminal_id", s)) for s in restored),
        )
    elif running:
        logger.warning(
            "[ORPHAN] Found %d running terminal session(s), but none could be restored",
            len(running),
        )
    return restored


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
        events.publish(make_trace_event(EventName.SESSION_NAME_PARSE_ERROR, {"session_name": session_name, "error": str(e)}))
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
    return session_manager.start(SessionContext(ref=ref, command=cmd, working_dir=wd, title=title))


def session_exists(name: str, session_manager: SessionManager, events: EventSink) -> bool:
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
) -> Optional[Session]:
    """Launch an issue session and update active-session tracking."""
    result = session_launcher.launch_issue_session(issue, state.active_sessions)
    if result.success and result.session:
        append_unique_active_sessions(state.active_sessions, [result.session])
    elif result.keep_queued and session_restorer is not None:
        session_name = f"issue-{issue.number}"
        restored = session_restorer.restore_known_terminal(
            issue_number=issue.number,
            session_name=session_name,
            is_review=False,
            already_tracked=state.active_sessions,
        )
        if restored:
            append_unique_active_sessions(state.active_sessions, restored)
            logger.info("[ORPHAN] Restored tracking for existing terminal: %s", session_name)
            return restored[0]
        logger.warning("[ORPHAN] Couldn't restore session %s - terminal may be stale", session_name)
    return result.session if result.success else None
