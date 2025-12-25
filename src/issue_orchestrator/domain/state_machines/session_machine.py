"""State machine for development session lifecycle management.

This module implements the state machine for tracking a development session through
its lifecycle, from launch through completion or failure.

The state machine is designed to be pure - it returns TransitionResult instead of
publishing events directly. The caller (control layer) is responsible for emitting
TraceEvents via EventSink.
"""

import logging
from datetime import datetime
from enum import Enum
from typing import Optional

from transitions import Machine

from .transition_result import TransitionResult

logger = logging.getLogger(__name__)


class SessionState(Enum):
    """States a development session can be in during its lifecycle."""

    PENDING = "pending"
    STARTING = "starting"
    RUNNING = "running"
    SLOW = "slow"
    COMPLETING = "completing"
    COMPLETED = "completed"
    FAILED = "failed"
    TIMED_OUT = "timed_out"
    BLOCKED = "blocked"
    NEEDS_HUMAN = "needs_human"


class SessionStateMachine:
    """State machine for managing development session lifecycle.

    This state machine tracks a development session through the following states:
    - PENDING: Session is scheduled but not yet launched
    - STARTING: Session is being initialized
    - RUNNING: Session is actively running
    - SLOW: Session has been running longer than expected
    - COMPLETING: Session is in the process of wrapping up
    - COMPLETED: Session finished successfully
    - FAILED: Session encountered a fatal error
    - TIMED_OUT: Session exceeded maximum runtime
    - BLOCKED: Session is blocked waiting for resolution
    - NEEDS_HUMAN: Session needs human intervention

    The state machine is pure - transitions store their result in last_transition
    which callers use to emit appropriate TraceEvents via EventSink.

    Attributes:
        session_id: Unique identifier for this session
        issue_number: The GitHub issue number this session is working on
        state: Current state of the session
        started_at: Timestamp when session started running (None if not started)
        timeout_minutes: Maximum runtime before timing out (None for no timeout)
        last_transition: Result of the most recent transition (for event emission)
    """

    def __init__(
        self,
        session_id: str,
        issue_number: int,
        initial_state: SessionState = SessionState.PENDING,
        timeout_minutes: Optional[int] = None
    ):
        """Initialize the session state machine.

        Args:
            session_id: Unique identifier for this session
            issue_number: The GitHub issue number
            initial_state: Starting state (defaults to PENDING)
            timeout_minutes: Maximum runtime in minutes (None for no timeout)
        """
        self.session_id = session_id
        self.issue_number = issue_number
        self.state = initial_state
        self.started_at: Optional[datetime] = None
        self.timeout_minutes = timeout_minutes
        self.last_transition: Optional[TransitionResult] = None

        # Define all possible states
        states = [state.value for state in SessionState]

        # Define state transitions
        transitions = [
            # Launch a pending session
            {
                'trigger': 'launch',
                'source': SessionState.PENDING.value,
                'dest': SessionState.STARTING.value,
                'after': '_on_launched'
            },
            # Session successfully started
            {
                'trigger': 'started',
                'source': SessionState.STARTING.value,
                'dest': SessionState.RUNNING.value,
                'after': '_on_started'
            },
            # Mark a running session as slow
            {
                'trigger': 'mark_slow',
                'source': SessionState.RUNNING.value,
                'dest': SessionState.SLOW.value,
                'after': '_on_slow'
            },
            # Complete a running or slow session
            {
                'trigger': 'complete',
                'source': [SessionState.RUNNING.value, SessionState.SLOW.value],
                'dest': SessionState.COMPLETED.value,
                'after': '_on_completed'
            },
            # Session failed during startup, running, or slow states
            {
                'trigger': 'fail',
                'source': [SessionState.STARTING.value, SessionState.RUNNING.value, SessionState.SLOW.value],
                'dest': SessionState.FAILED.value,
                'after': '_on_failed'
            },
            # Session timed out
            {
                'trigger': 'timeout',
                'source': [SessionState.RUNNING.value, SessionState.SLOW.value],
                'dest': SessionState.TIMED_OUT.value,
                'after': '_on_timed_out'
            },
            # Session blocked
            {
                'trigger': 'block',
                'source': SessionState.RUNNING.value,
                'dest': SessionState.BLOCKED.value,
                'after': '_on_blocked'
            },
            # Session needs human intervention
            {
                'trigger': 'needs_human',
                'source': SessionState.RUNNING.value,
                'dest': SessionState.NEEDS_HUMAN.value,
                'after': '_on_needs_human'
            },
            # Resume from blocked or needs_human states
            {
                'trigger': 'resume',
                'source': [SessionState.BLOCKED.value, SessionState.NEEDS_HUMAN.value],
                'dest': SessionState.RUNNING.value,
                'after': '_on_resumed'
            }
        ]

        # Create the state machine
        self.machine = Machine(
            model=self,
            states=states,
            transitions=transitions,
            initial=initial_state.value,
            send_event=True,
            auto_transitions=False
        )

        logger.info(f"SessionStateMachine initialized for session {session_id} in state {initial_state.value}")

    def _on_launched(self, event):
        """Callback for launch transition.

        Args:
            event: Transition event data from transitions library
        """
        data = event.kwargs.get('data', {})
        transition_data = {**data, 'session_id': self.session_id}

        self.last_transition = TransitionResult(
            success=True,
            from_state=SessionState.PENDING.value,
            to_state=SessionState.STARTING.value,
            event_name="session.launched",
            entity_id=self.issue_number,
            data=transition_data,
        )
        logger.info(f"Session {self.session_id} launched for issue {self.issue_number}")

    def _on_started(self, event):
        """Callback for started transition.

        Args:
            event: Transition event data from transitions library
        """
        self.started_at = datetime.now()
        data = event.kwargs.get('data', {})
        transition_data = {**data, 'session_id': self.session_id, 'started_at': self.started_at.isoformat()}

        self.last_transition = TransitionResult(
            success=True,
            from_state=SessionState.STARTING.value,
            to_state=SessionState.RUNNING.value,
            event_name="session.started",
            entity_id=self.issue_number,
            data=transition_data,
        )
        logger.info(f"Session {self.session_id} started for issue {self.issue_number}")

    def _on_slow(self, event):
        """Callback for mark_slow transition.

        Args:
            event: Transition event data from transitions library
        """
        data = event.kwargs.get('data', {})
        runtime = self._get_runtime_minutes()
        transition_data = {**data, 'session_id': self.session_id, 'runtime_minutes': runtime}

        self.last_transition = TransitionResult(
            success=True,
            from_state=SessionState.RUNNING.value,
            to_state=SessionState.SLOW.value,
            event_name="session.slow",
            entity_id=self.issue_number,
            data=transition_data,
        )
        logger.warning(f"Session {self.session_id} marked as slow (runtime: {runtime} minutes)")

    def _on_completed(self, event):
        """Callback for complete transition.

        Args:
            event: Transition event data from transitions library
        """
        data = event.kwargs.get('data', {})
        runtime = self._get_runtime_minutes()
        from_state = event.transition.source if hasattr(event, 'transition') else SessionState.RUNNING.value
        transition_data = {**data, 'session_id': self.session_id, 'runtime_minutes': runtime}

        self.last_transition = TransitionResult(
            success=True,
            from_state=from_state,
            to_state=SessionState.COMPLETED.value,
            event_name="session.completed",
            entity_id=self.issue_number,
            data=transition_data,
        )
        logger.info(f"Session {self.session_id} completed (runtime: {runtime} minutes)")

    def _on_failed(self, event):
        """Callback for fail transition.

        Args:
            event: Transition event data from transitions library
        """
        data = event.kwargs.get('data', {})
        runtime = self._get_runtime_minutes()
        from_state = event.transition.source if hasattr(event, 'transition') else SessionState.RUNNING.value
        transition_data = {**data, 'session_id': self.session_id, 'runtime_minutes': runtime}

        self.last_transition = TransitionResult(
            success=True,
            from_state=from_state,
            to_state=SessionState.FAILED.value,
            event_name="session.failed",
            entity_id=self.issue_number,
            data=transition_data,
        )
        logger.error(f"Session {self.session_id} failed (runtime: {runtime} minutes)")

    def _on_timed_out(self, event):
        """Callback for timeout transition.

        Args:
            event: Transition event data from transitions library
        """
        data = event.kwargs.get('data', {})
        runtime = self._get_runtime_minutes()
        from_state = event.transition.source if hasattr(event, 'transition') else SessionState.RUNNING.value
        transition_data = {**data, 'session_id': self.session_id, 'runtime_minutes': runtime}

        self.last_transition = TransitionResult(
            success=True,
            from_state=from_state,
            to_state=SessionState.TIMED_OUT.value,
            event_name="session.timeout",
            entity_id=self.issue_number,
            data=transition_data,
        )
        logger.error(f"Session {self.session_id} timed out (runtime: {runtime} minutes)")

    def _on_blocked(self, event):
        """Callback for block transition.

        Args:
            event: Transition event data from transitions library
        """
        data = event.kwargs.get('data', {})
        transition_data = {**data, 'session_id': self.session_id}

        self.last_transition = TransitionResult(
            success=True,
            from_state=SessionState.RUNNING.value,
            to_state=SessionState.BLOCKED.value,
            event_name="session.blocked",
            entity_id=self.issue_number,
            data=transition_data,
        )
        logger.warning(f"Session {self.session_id} blocked")

    def _on_needs_human(self, event):
        """Callback for needs_human transition.

        Args:
            event: Transition event data from transitions library
        """
        data = event.kwargs.get('data', {})
        transition_data = {**data, 'session_id': self.session_id}

        self.last_transition = TransitionResult(
            success=True,
            from_state=SessionState.RUNNING.value,
            to_state=SessionState.NEEDS_HUMAN.value,
            event_name="session.needs_human",
            entity_id=self.issue_number,
            data=transition_data,
        )
        logger.warning(f"Session {self.session_id} needs human intervention")

    def _on_resumed(self, event):
        """Callback for resume transition.

        Args:
            event: Transition event data from transitions library
        """
        from_state = event.transition.source if hasattr(event, 'transition') else SessionState.BLOCKED.value

        self.last_transition = TransitionResult(
            success=True,
            from_state=from_state,
            to_state=SessionState.RUNNING.value,
            event_name="session.resumed",
            entity_id=self.issue_number,
            data={'session_id': self.session_id},
        )

        logger.info(f"Session {self.session_id} resumed")

    def _get_runtime_minutes(self) -> Optional[float]:
        """Calculate runtime in minutes since session started.

        Returns:
            Runtime in minutes, or None if session hasn't started
        """
        if self.started_at is None:
            return None
        delta = datetime.now() - self.started_at
        return delta.total_seconds() / 60.0

    def check_timeout(self) -> bool:
        """Check if the session has exceeded its timeout.

        If the session has timed out and is in a running or slow state,
        automatically triggers the timeout transition.

        Returns:
            True if session has timed out, False otherwise
        """
        if self.timeout_minutes is None:
            return False

        runtime = self._get_runtime_minutes()
        if runtime is None:
            return False

        if runtime > self.timeout_minutes:
            current_state = self.get_state()
            if current_state in [SessionState.RUNNING, SessionState.SLOW]:
                logger.warning(
                    f"Session {self.session_id} exceeded timeout "
                    f"({runtime:.1f} > {self.timeout_minutes} minutes)"
                )
                self.timeout(data={'runtime_minutes': runtime, 'timeout_minutes': self.timeout_minutes})  # type: ignore[attr-defined]
                return True

        return False

    def get_state(self) -> SessionState:
        """Get the current state as an enum.

        Returns:
            Current SessionState enum value
        """
        return SessionState(self.state)

    def can_transition(self, trigger: str) -> bool:
        """Check if a transition is valid from the current state.

        Args:
            trigger: Name of the transition to check

        Returns:
            True if the transition is valid, False otherwise
        """
        trigger_func = getattr(self, f'may_{trigger}', None)
        if trigger_func and callable(trigger_func):
            return bool(trigger_func())
        return False

    def get_runtime_info(self) -> dict:
        """Get runtime information for this session.

        Returns:
            Dictionary with runtime information including:
            - started_at: ISO timestamp when session started (or None)
            - runtime_minutes: Current runtime in minutes (or None)
            - timeout_minutes: Configured timeout (or None)
            - is_timed_out: Whether session has exceeded timeout
        """
        runtime = self._get_runtime_minutes()
        is_timed_out = False
        if self.timeout_minutes is not None and runtime is not None:
            is_timed_out = runtime > self.timeout_minutes

        return {
            'started_at': self.started_at.isoformat() if self.started_at else None,
            'runtime_minutes': runtime,
            'timeout_minutes': self.timeout_minutes,
            'is_timed_out': is_timed_out
        }
