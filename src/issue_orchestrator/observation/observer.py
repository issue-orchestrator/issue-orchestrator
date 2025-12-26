"""Session observation and completion handling.

Naming convention (from architecture review):
- "Observer" implies non-authoritative fact-gathering
- Observers observe, they don't decide
- Decisions belong in Controllers (LifecycleController)

Components that observe are named Observers;
Components that decide are named Controllers;
Components that act are named Adapters.
"""

import logging
from typing import Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from ..domain.state_machines.session_machine import SessionStateMachine
    from ..ports import RepositoryHost, SessionRunner

from ..config import Config
from ..models import Session, SessionStatus
from ..ports import EventSink, TraceEvent, NullEventSink
from .. import labels
from .observation import SessionObservation, SessionObservationResult

logger = logging.getLogger(__name__)


class SessionObserver:
    """Observe running sessions and gather facts about their state.

    This class observes sessions and returns facts (SessionStatus).
    It does NOT make policy decisions - that's the controller's job.

    Note: handle_completion() currently contains some policy (which labels
    to add/remove). This should eventually move to LifecycleController,
    with this class only doing observation.
    """

    def __init__(
        self,
        config: Config,
        session_machines: dict[str, "SessionStateMachine"] | None = None,
        events: EventSink | None = None,
        session_runner: Optional["SessionRunner"] = None,
        repository_host: Optional["RepositoryHost"] = None,
    ) -> None:
        """Initialize the observer with configuration.

        Args:
            config: Orchestrator configuration
            session_machines: Optional dict mapping session names to state machines
            events: Optional EventSink for emitting trace events
            session_runner: SessionRunner port for terminal operations
            repository_host: RepositoryHost port for GitHub operations
        """
        self.config = config
        self.session_machines = session_machines or {}
        self.events = events or NullEventSink()
        self._session_runner = session_runner
        self._repository_host = repository_host

    def _extract_session_number(self, session_name: str) -> int:
        """Extract the numeric ID from a session name (handles both issue- and review- prefixes)."""
        if session_name.startswith("issue-"):
            return int(session_name.replace("issue-", ""))
        elif session_name.startswith("review-"):
            return int(session_name.replace("review-", ""))
        else:
            raise ValueError(f"Unknown session name format: {session_name}")

    def _session_exists(self, session_id: int) -> bool:
        """Check if a session exists using the session runner."""
        if self._session_runner is None:
            return False
        return self._session_runner.session_exists(session_id)

    def _kill_session(self, session_id: int) -> None:
        """Kill a session using the session runner."""
        if self._session_runner is None:
            return
        self._session_runner.kill_session(session_id)

    def _send_exit_to_session(self, session_id: int) -> bool:
        """Send /exit command to a session."""
        if self._session_runner is None:
            return False
        return self._session_runner.send_to_session(session_id, "/exit")

    def _get_open_prs_for_branch(self, branch: str) -> list:
        """Get open PRs for a branch using the repository host."""
        if self._repository_host is None:
            return []
        return self._repository_host.get_prs_for_branch(branch, state="open")

    def _get_issue_labels(self, issue_number: int) -> list[str]:
        """Get labels for an issue using the repository host."""
        if self._repository_host is None:
            return []
        return self._repository_host.get_issue_labels(issue_number)

    def _add_label(self, issue_number: int, label: str) -> None:
        """Add a label to an issue using the repository host."""
        if self._repository_host is None:
            return
        self._repository_host.add_label(issue_number, label)

    def _remove_label(self, issue_number: int, label: str) -> None:
        """Remove a label from an issue using the repository host."""
        if self._repository_host is None:
            return
        self._repository_host.remove_label(issue_number, label)

    def observe_session(self, session: Session) -> SessionObservationResult:
        """Observe a session and return facts about its state.

        This method only gathers facts. It does NOT decide outcomes.
        The controller uses these observations + completion.json to decide.

        Returns:
            SessionObservationResult with observed facts:
            - RUNNING: Session exists and not timed out
            - TERMINATED: Session no longer exists
            - TIMED_OUT: Session exceeded timeout (may or may not exist)
        """
        # Get runtime info
        machine = self.session_machines.get(session.tmux_session_name)
        if machine:
            runtime = machine._get_runtime_minutes()
            timeout = machine.timeout_minutes
        else:
            runtime = session.runtime_minutes
            timeout = session.agent_config.timeout_minutes

        # Check timeout first
        timeout_exceeded = False
        if machine and machine.timeout_minutes:
            if runtime and runtime > machine.timeout_minutes:
                timeout_exceeded = True
        elif session.is_timed_out:
            timeout_exceeded = True

        # Check if session exists
        exists = self._session_exists(session.issue.number)

        # Check for completion.json - this is the source of truth for agent completion
        # If it exists AND is valid JSON, the agent called agent-done and work is done
        # Use session.completion_path which is agent-specific (e.g., completion-agent_e2e-test.json)
        completion_path = session.worktree_path / session.completion_path
        if completion_path.exists():
            # Validate the JSON is complete (not partially written)
            try:
                import json
                with open(completion_path) as f:
                    data = json.load(f)
                # Check for required terminator fields
                if all(k in data for k in ["session_id", "timestamp", "outcome", "summary"]):
                    logger.info(
                        f"Session #{session.issue.number} has valid completion.json - agent work is done"
                    )
                    # Emit event for observability
                    self.events.publish(TraceEvent(
                        name="observation.completion_detected",
                        data={
                            "issue_number": session.issue.number,
                            "session_name": session.tmux_session_name,
                            "outcome": data.get("outcome"),
                            "session_exists": exists,
                        },
                    ))
                    # Don't kill session yet - let controller handle cleanup after processing
                    # This allows inspection of what happened in the terminal
                    # Return TERMINATED so controller processes completion.json
                    return SessionObservationResult.terminated(runtime_minutes=runtime)
                else:
                    logger.debug(f"completion.json missing required fields, treating as incomplete")
            except (json.JSONDecodeError, OSError) as e:
                logger.debug(f"completion.json not yet valid (partial write?): {e}")

        # If session is running and has PR, try to send /exit (side effect)
        # This helps sessions that completed but forgot to exit
        if exists and not session.exit_sent:
            try:
                prs = self._get_open_prs_for_branch(session.branch_name)
                if prs:
                    logger.info(
                        f"Session #{session.issue.number} has PR but still running - sending /exit"
                    )
                    if self._send_exit_to_session(session.issue.number):
                        session.exit_sent = True
            except Exception as e:
                logger.debug(f"Could not check for PRs: {e}")

        # Build observation result
        if timeout_exceeded:
            # Timeout takes priority as it requires action (kill session)
            result = SessionObservationResult.timed_out(
                runtime_minutes=runtime,
                timeout_minutes=timeout,
                session_exists=exists,
            )
        elif exists:
            result = SessionObservationResult.running(runtime_minutes=runtime)
        else:
            result = SessionObservationResult.terminated(runtime_minutes=runtime)

        # Emit observation result for debugging (only for non-running sessions)
        if result.observation != SessionObservation.RUNNING:
            self.events.publish(TraceEvent(
                name="observation.result",
                data={
                    "issue_number": session.issue.number,
                    "session_name": session.tmux_session_name,
                    "observation": result.observation.value,
                    "session_exists": result.session_exists,
                    "runtime_minutes": result.runtime_minutes,
                    "timeout_minutes": result.timeout_minutes,
                    "worktree_path": str(session.worktree_path),
                    "completion_json_exists": completion_path.exists(),
                },
            ))

        return result

    def check_session(self, session: Session) -> SessionStatus:
        """Check the status of a session.

        Logic:
        1. If runtime > timeout -> TIMED_OUT (uses state machine if available)
        2. If session still running:
           a. Check if PR exists -> send /exit, return RUNNING (will complete next check)
           b. Otherwise -> RUNNING
        3. If session exited:
           a. Check if PR exists for branch -> COMPLETED
           b. Check if issue has 'blocked' label -> BLOCKED
           c. Check if issue has 'needs-human' label -> NEEDS_HUMAN
           d. Otherwise -> FAILED

        Args:
            session: The session to check

        Returns:
            SessionStatus indicating the current state of the session
        """
        # Check timeout using state machine if available (primary method)
        machine = self.session_machines.get(session.tmux_session_name)
        if machine and machine.check_timeout():
            logger.info(
                f"[STATE_MACHINE] Session {session.tmux_session_name} timed out "
                f"(runtime: {machine._get_runtime_minutes():.1f}m, "
                f"timeout: {machine.timeout_minutes}m)"
            )
            return SessionStatus.TIMED_OUT

        # Fallback to session-level timeout check (for sessions without state machines)
        if session.is_timed_out:
            logger.info(
                f"Session for issue #{session.issue.number} has timed out "
                f"(runtime: {session.runtime_minutes}m, "
                f"timeout: {session.agent_config.timeout_minutes}m)"
            )
            return SessionStatus.TIMED_OUT

        # Check if session is still running
        if self._session_exists(session.issue.number):
            # Session still running - but check if it has a PR (meaning it's done but didn't exit)
            # Only send /exit once to avoid spamming
            if not session.exit_sent:
                try:
                    prs = self._get_open_prs_for_branch(session.branch_name)
                    if prs:
                        logger.info(
                            f"Session #{session.issue.number} has PR but still running - sending /exit"
                        )
                        if self._send_exit_to_session(session.issue.number):
                            session.exit_sent = True
                except Exception as e:
                    logger.debug(f"Could not check for PRs: {e}")

            logger.debug(
                f"Session for issue #{session.issue.number} still running "
                f"(session: {session.tmux_session_name})"
            )
            return SessionStatus.RUNNING

        # Session has exited, determine the outcome
        logger.debug(
            f"Session for issue #{session.issue.number} has exited, "
            f"checking completion status"
        )

        # Check if PR exists for the branch
        try:
            prs = self._get_open_prs_for_branch(session.branch_name)
            if prs:
                logger.info(
                    f"Found {len(prs)} open PR(s) for branch {session.branch_name}, "
                    f"marking session as COMPLETED"
                )
                return SessionStatus.COMPLETED
        except Exception as e:
            logger.warning(
                f"Failed to check for open PRs on branch {session.branch_name}: {e}"
            )

        # Fetch fresh labels from GitHub (session.issue.labels is stale from launch time)
        try:
            current_labels = self._get_issue_labels(session.issue.number)
            logger.debug(f"Fresh labels for #{session.issue.number}: {current_labels}")
        except Exception as e:
            logger.warning(f"Failed to fetch labels for #{session.issue.number}: {e}")
            current_labels = session.issue.labels  # Fall back to stale labels

        # Check if issue has 'blocked' label
        if self.config.get_label_blocked() in current_labels:
            logger.info(
                f"Issue #{session.issue.number} has '{self.config.get_label_blocked()}' label, "
                f"marking session as BLOCKED"
            )
            return SessionStatus.BLOCKED

        # Check if issue has 'needs-human' label
        if self.config.get_label_needs_human() in current_labels:
            logger.info(
                f"Issue #{session.issue.number} has '{self.config.get_label_needs_human()}' label, "
                f"marking session as NEEDS_HUMAN"
            )
            return SessionStatus.NEEDS_HUMAN

        # No success indicators found
        logger.info(
            f"Session for issue #{session.issue.number} ended without completion markers, "
            f"marking as FAILED"
        )
        return SessionStatus.FAILED

    def check_all_sessions(self, sessions: list[Session]) -> dict[int, SessionStatus]:
        """Check all sessions and return their statuses.

        Args:
            sessions: List of sessions to check

        Returns:
            Dictionary mapping issue_number to SessionStatus
        """
        statuses: dict[int, SessionStatus] = {}

        for session in sessions:
            try:
                status = self.check_session(session)
                statuses[session.issue.number] = status
            except Exception as e:
                logger.error(
                    f"Error checking session for issue #{session.issue.number}: {e}"
                )
                statuses[session.issue.number] = SessionStatus.FAILED

        return statuses

    def handle_completion(self, session: Session, status: SessionStatus) -> None:
        """Handle session completion based on status.

        Actions:
        - COMPLETED: remove in-progress label (work done, PR created)
        - BLOCKED: keep in-progress label (maintains claim, blocked label coexists)
        - NEEDS_HUMAN: keep in-progress label (maintains claim, needs-human label coexists)
        - FAILED: remove in-progress label (releases claim)
        - TIMED_OUT: kill tmux session, add 'timed-out' label, remove in-progress label

        Note: BLOCKED and NEEDS_HUMAN keep in-progress to maintain ownership claim.
        When the blocker is resolved, work can resume immediately.

        Args:
            session: The completed session
            status: The final status of the session
        """
        issue_number = session.issue.number

        try:
            if status == SessionStatus.TIMED_OUT:
                # Kill the session
                try:
                    self._kill_session(issue_number)
                    logger.info(
                        f"Killed session {session.tmux_session_name} "
                        f"for issue #{issue_number}"
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to kill session {session.tmux_session_name}: {e}"
                    )

                # Add blocking label to prevent re-queuing
                try:
                    self._add_label(issue_number, labels.BLOCKED_FAILED)
                    logger.info(f"Added '{labels.BLOCKED_FAILED}' label to issue #{issue_number} (timed out)")
                except Exception as e:
                    logger.error(
                        f"Failed to add '{labels.BLOCKED_FAILED}' label to issue #{issue_number}: {e}"
                    )

            elif status == SessionStatus.FAILED:
                # Add blocking label to prevent re-queuing
                try:
                    self._add_label(issue_number, labels.BLOCKED_FAILED)
                    logger.info(f"Added '{labels.BLOCKED_FAILED}' label to issue #{issue_number}")
                except Exception as e:
                    logger.warning(
                        f"Failed to add '{labels.BLOCKED_FAILED}' label to issue #{issue_number}: {e}"
                    )

            # Remove in-progress label only for statuses that release ownership
            # BLOCKED and NEEDS_HUMAN keep in-progress to maintain claim
            if status in (
                SessionStatus.COMPLETED,
                SessionStatus.FAILED,
                SessionStatus.TIMED_OUT,
            ):
                try:
                    self._remove_label(issue_number, self.config.get_label_in_progress())
                    logger.info(
                        f"Removed '{self.config.get_label_in_progress()}' label from issue #{issue_number}"
                    )
                except Exception as e:
                    logger.error(
                        f"Failed to remove '{self.config.get_label_in_progress()}' label "
                        f"from issue #{issue_number}: {e}"
                    )

            # Auto-close tab based on config
            should_close = (
                (status == SessionStatus.COMPLETED and self.config.close_completed_tabs) or
                (status in (SessionStatus.FAILED, SessionStatus.BLOCKED, SessionStatus.NEEDS_HUMAN, SessionStatus.TIMED_OUT)
                 and self.config.close_failed_tabs)
            )
            if should_close:
                try:
                    self._kill_session(issue_number)
                    logger.info(f"Closed tab for {status.value} session #{issue_number}")
                except Exception as e:
                    logger.debug(f"Could not close tab for #{issue_number}: {e}")

            logger.info(
                f"Completed handling for issue #{issue_number} with status {status.value}"
            )

        except Exception as e:
            logger.error(
                f"Unexpected error handling completion for issue #{issue_number}: {e}"
            )


# Backwards compatibility alias (deprecated)
# TODO: Remove after all imports are updated
SessionMonitor = SessionObserver
