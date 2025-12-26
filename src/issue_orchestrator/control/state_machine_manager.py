"""StateMachineManager - manages state machines for issues, sessions, and reviews.

This module extracts state machine management from the orchestrator,
providing a clean interface for getting or creating state machines.
"""

import logging
from typing import Optional

from ..domain.state_machines.issue_machine import IssueStateMachine
from ..domain.state_machines.session_machine import SessionStateMachine
from ..domain.state_machines.review_machine import ReviewStateMachine
from ..config import Config
from ..ports import EventSink

logger = logging.getLogger(__name__)


class StateMachineManager:
    """Manages state machines for issues, sessions, and reviews.

    This class provides get-or-create semantics for state machines,
    ensuring each entity has exactly one state machine instance.

    State machines are created lazily on first access and cached
    for the lifetime of the manager.
    """

    def __init__(
        self,
        config: Config,
        events: EventSink,
    ):
        """Initialize the state machine manager.

        Args:
            config: Configuration for state machine parameters
            events: EventSink for state machine events
        """
        self.config = config
        self.events = events

        # State machine caches
        self._issue_machines: dict[int, IssueStateMachine] = {}
        self._session_machines: dict[str, SessionStateMachine] = {}
        self._review_machines: dict[int, ReviewStateMachine] = {}

    def get_issue_machine(self, issue_number: int) -> IssueStateMachine:
        """Get or create issue state machine.

        Args:
            issue_number: The GitHub issue number

        Returns:
            IssueStateMachine for the given issue
        """
        if issue_number not in self._issue_machines:
            machine = IssueStateMachine(issue_number=issue_number)
            self._issue_machines[issue_number] = machine
            logger.debug(f"[STATE_MACHINE] Created IssueStateMachine for #{issue_number}")
        return self._issue_machines[issue_number]

    def get_session_machine(
        self,
        session_name: str,
        issue_number: int,
        timeout_minutes: int | None = None,
    ) -> SessionStateMachine:
        """Get or create session state machine.

        Args:
            session_name: The terminal session name (e.g., "issue-123")
            issue_number: The issue number associated with the session
            timeout_minutes: Timeout for the session (uses config default if None)

        Returns:
            SessionStateMachine for the given session
        """
        if session_name not in self._session_machines:
            timeout = timeout_minutes or self.config.session_timeout_minutes
            machine = SessionStateMachine(
                session_id=session_name,
                issue_number=issue_number,
                timeout_minutes=timeout,
            )
            self._session_machines[session_name] = machine
            logger.debug(f"[STATE_MACHINE] Created SessionStateMachine for {session_name}")
        return self._session_machines[session_name]

    def get_review_machine(
        self,
        pr_number: int,
        issue_number: int,
    ) -> ReviewStateMachine:
        """Get or create review state machine.

        Args:
            pr_number: The PR number
            issue_number: The linked issue number

        Returns:
            ReviewStateMachine for the given PR
        """
        if pr_number not in self._review_machines:
            machine = ReviewStateMachine(
                pr_number=pr_number,
                issue_number=issue_number,
                max_rework_cycles=self.config.max_rework_cycles,
            )
            self._review_machines[pr_number] = machine
            logger.debug(f"[STATE_MACHINE] Created ReviewStateMachine for PR #{pr_number}")
        return self._review_machines[pr_number]

    def has_issue_machine(self, issue_number: int) -> bool:
        """Check if an issue state machine exists."""
        return issue_number in self._issue_machines

    def has_session_machine(self, session_name: str) -> bool:
        """Check if a session state machine exists."""
        return session_name in self._session_machines

    def has_review_machine(self, pr_number: int) -> bool:
        """Check if a review state machine exists."""
        return pr_number in self._review_machines

    def remove_session_machine(self, session_name: str) -> None:
        """Remove a session state machine (e.g., after session completes)."""
        if session_name in self._session_machines:
            del self._session_machines[session_name]
            logger.debug(f"[STATE_MACHINE] Removed SessionStateMachine for {session_name}")

    @property
    def issue_machines(self) -> dict[int, IssueStateMachine]:
        """Get all issue state machines."""
        return self._issue_machines

    @property
    def session_machines(self) -> dict[str, SessionStateMachine]:
        """Get all session state machines."""
        return self._session_machines

    @property
    def review_machines(self) -> dict[int, ReviewStateMachine]:
        """Get all review state machines."""
        return self._review_machines
