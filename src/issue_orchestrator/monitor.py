"""Session monitoring and completion handling."""

import logging
from typing import Optional

from .config import Config
from .github import add_label, get_open_prs_for_branch, remove_label
from .models import Session, SessionStatus
from .tmux import kill_session as tmux_kill_session, session_exists as tmux_session_exists

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

    def _session_exists(self, session_name: str) -> bool:
        """Check if a session exists using the appropriate backend."""
        if self._using_iterm2:
            issue_number = int(session_name.replace("issue-", ""))
            return self._get_iterm_manager().session_exists(issue_number)
        else:
            return tmux_session_exists(session_name)

    def _kill_session(self, session_name: str) -> None:
        """Kill a session using the appropriate backend."""
        if self._using_iterm2:
            issue_number = int(session_name.replace("issue-", ""))
            self._get_iterm_manager().kill_session(issue_number)
        else:
            tmux_kill_session(session_name)

    def check_session(self, session: Session) -> SessionStatus:
        """Check the status of a session.

        Logic:
        1. If tmux session still running -> RUNNING
        2. If tmux session exited:
           a. Check if PR exists for branch -> COMPLETED
           b. Check if issue has 'blocked' label -> BLOCKED
           c. Check if issue has 'needs-human' label -> NEEDS_HUMAN
           d. Otherwise -> FAILED
        3. If runtime > timeout -> TIMED_OUT

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

        # Check if issue has 'blocked' label
        if self.config.get_label_blocked() in session.issue.labels:
            logger.info(
                f"Issue #{session.issue.number} has '{self.config.get_label_blocked()}' label, "
                f"marking session as BLOCKED"
            )
            return SessionStatus.BLOCKED

        # Check if issue has 'needs-human' label
        if self.config.get_label_needs_human() in session.issue.labels:
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
        - COMPLETED: remove in-progress label
        - BLOCKED: remove in-progress label (blocked label already set by agent)
        - NEEDS_HUMAN: remove in-progress label
        - FAILED: remove in-progress label, optionally add 'failed' label
        - TIMED_OUT: kill tmux session, add 'timed-out' label, remove in-progress label

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

                # Add timed-out label
                try:
                    add_label(
                        repo=repo,
                        issue_number=issue_number,
                        label="timed-out",
                    )
                    logger.info(f"Added 'timed-out' label to issue #{issue_number}")
                except Exception as e:
                    logger.error(
                        f"Failed to add 'timed-out' label to issue #{issue_number}: {e}"
                    )

            elif status == SessionStatus.FAILED:
                # Add failed label (optional, helps track failures)
                try:
                    add_label(
                        repo=repo,
                        issue_number=issue_number,
                        label="failed",
                    )
                    logger.info(f"Added 'failed' label to issue #{issue_number}")
                except Exception as e:
                    logger.warning(
                        f"Failed to add 'failed' label to issue #{issue_number}: {e}"
                    )

            # Remove in-progress label for all completion statuses
            if status in (
                SessionStatus.COMPLETED,
                SessionStatus.BLOCKED,
                SessionStatus.NEEDS_HUMAN,
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

            logger.info(
                f"Completed handling for issue #{issue_number} with status {status.value}"
            )

        except Exception as e:
            logger.error(
                f"Unexpected error handling completion for issue #{issue_number}: {e}"
            )
