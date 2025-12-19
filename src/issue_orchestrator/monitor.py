"""Session monitoring and completion handling."""

import logging
from typing import Optional

from .config import Config
from .github import add_label, get_issue_labels, get_open_prs_for_branch, remove_label
from .models import Session, SessionStatus
from .tmux import kill_session, session_exists
from . import labels

logger = logging.getLogger(__name__)


class SessionMonitor:
    """Monitor running sessions and handle their completion."""

    def __init__(self, config: Config) -> None:
        """Initialize the monitor with configuration.

        Args:
            config: Orchestrator configuration
        """
        self.config = config
        self._iterm_manager = None  # Lazy init

    @property
    def _using_iterm2(self) -> bool:
        """Check if we're using iTerm2 mode (or web mode, which also uses iTerm2 tabs)."""
        return self.config.ui_mode in ("iterm2", "web")

    def _get_iterm_manager(self):
        """Get the iTerm2 session manager (lazy init)."""
        if self._iterm_manager is None:
            from .iterm2 import get_iterm_manager
            self._iterm_manager = get_iterm_manager()
        return self._iterm_manager

    def _extract_session_number(self, session_name: str) -> int:
        """Extract the numeric ID from a session name (handles both issue- and review- prefixes)."""
        if session_name.startswith("issue-"):
            return int(session_name.replace("issue-", ""))
        elif session_name.startswith("review-"):
            return int(session_name.replace("review-", ""))
        else:
            raise ValueError(f"Unknown session name format: {session_name}")

    def _session_exists(self, session_name: str) -> bool:
        """Check if a session exists using the appropriate backend."""
        if self._using_iterm2:
            session_number = self._extract_session_number(session_name)
            return self._get_iterm_manager().session_exists(session_number)
        else:
            return session_exists(session_name)

    def _kill_session(self, session_name: str) -> None:
        """Kill a session using the appropriate backend."""
        if self._using_iterm2:
            session_number = self._extract_session_number(session_name)
            self._get_iterm_manager().kill_session(session_number)
        else:
            kill_session(session_name)

    def _send_exit_to_session(self, issue_number: int) -> bool:
        """Send /exit command to a session."""
        if self._using_iterm2:
            return self._get_iterm_manager().send_to_session(issue_number, "/exit")
        return False

    def check_session(self, session: Session) -> SessionStatus:
        """Check the status of a session.

        Logic:
        1. If runtime > timeout -> TIMED_OUT
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
        # Check if session has timed out first
        if session.is_timed_out:
            logger.info(
                f"Session for issue #{session.issue.number} has timed out "
                f"(runtime: {session.runtime_minutes}m, "
                f"timeout: {session.agent_config.timeout_minutes}m)"
            )
            return SessionStatus.TIMED_OUT

        # Check if session is still running
        if self._session_exists(session.tmux_session_name):
            # Session still running - but check if it has a PR (meaning it's done but didn't exit)
            # Only send /exit once to avoid spamming
            if not session.exit_sent:
                try:
                    prs = get_open_prs_for_branch(
                        repo=self.config.repo,
                        branch=session.branch_name,
                    )
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
            prs = get_open_prs_for_branch(
                repo=self.config.repo,
                branch=session.branch_name,
            )
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
            current_labels = get_issue_labels(self.config.repo, session.issue.number)
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
        repo = self.config.repo

        try:
            if status == SessionStatus.TIMED_OUT:
                # Kill the session
                try:
                    self._kill_session(session.tmux_session_name)
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
                    add_label(
                        repo=repo,
                        issue_number=issue_number,
                        label=labels.BLOCKED_FAILED,
                    )
                    logger.info(f"Added '{labels.BLOCKED_FAILED}' label to issue #{issue_number} (timed out)")
                except Exception as e:
                    logger.error(
                        f"Failed to add '{labels.BLOCKED_FAILED}' label to issue #{issue_number}: {e}"
                    )

            elif status == SessionStatus.FAILED:
                # Add blocking label to prevent re-queuing
                try:
                    add_label(
                        repo=repo,
                        issue_number=issue_number,
                        label=labels.BLOCKED_FAILED,
                    )
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
                    remove_label(
                        repo=repo,
                        issue_number=issue_number,
                        label=self.config.get_label_in_progress(),
                    )
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
                    self._kill_session(session.tmux_session_name)
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
