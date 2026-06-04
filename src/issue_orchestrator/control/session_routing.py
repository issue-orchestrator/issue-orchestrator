"""Orchestrator-facing session routing helpers.

These helpers bridge orchestrator state and session infrastructure. Core launch
policy stays in SessionLauncher; this module handles wrapper concerns such as
active-session registration, orphan restoration, triage dispatch, and
SessionManager adapter calls.
"""

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

from ..domain.models import (
    Issue,
    PendingReview,
    PendingRetrospectiveReview,
    PendingRework,
    PendingTriageReview,
    PendingValidationRetry,
    Session,
)
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


@dataclass(frozen=True, slots=True)
class _PendingSessionQueues:
    """Owner for launch-routing pending queue removals."""

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


def orchestrator_launch_review_session(
    review: PendingReview,
    state: "OrchestratorState",
    session_launcher: SessionLauncher,
    session_restorer: "SessionRestorer",
) -> Optional[Session]:
    """Launch a review session and update orchestrator queues."""
    pending_queues = _PendingSessionQueues(state)
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
    pending_queues = _PendingSessionQueues(state)
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
    pending_queues = _PendingSessionQueues(state)
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
    pending_queues = _PendingSessionQueues(state)
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
) -> Optional[Session]:
    """Launch an issue session and update active-session tracking."""
    result = session_launcher.launch_issue_session(issue, state.active_sessions)
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
    runner = getattr(session_launcher.session_manager, "runner", None)
    discover = getattr(runner, "discover_running_sessions", None)
    if not callable(discover):
        return None
    try:
        running = discover()
    except Exception:
        logger.exception(
            "[ORPHAN] Failed to discover running terminal sessions for %s",
            request.session_name,
        )
        return None
    if not isinstance(running, list):
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
