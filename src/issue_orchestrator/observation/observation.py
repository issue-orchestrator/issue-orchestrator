"""Observation models - facts about session state.

Observations are pure facts gathered by observers.
They describe WHAT IS, not what to do about it.

The separation:
- Observation: "session is not running" (fact)
- Decision: "mark as FAILED because no completion.json" (policy)

Observers gather observations.
Controllers make decisions based on observations + completion records.
"""

from dataclasses import dataclass, field
from enum import Enum
from typing import Optional


class SessionObservation(Enum):
    """What we observed about a session.

    These are facts, not decisions. The observer reports what it sees.
    The controller decides what to do based on observations + completion.json.
    """

    # Session is actively running
    RUNNING = "running"

    # Session process/tab no longer exists (exited, crashed, or was killed)
    TERMINATED = "terminated"

    # Session exceeded its timeout limit (may still be running)
    TIMED_OUT = "timed_out"


@dataclass(frozen=True)
class SessionObservationResult:
    """Complete observation result for a session.

    Contains all facts gathered about the session state.
    Controller uses this + completion.json to make decisions.
    """

    # Primary observation
    observation: SessionObservation

    # Session still exists (tab/process running)
    session_exists: bool

    # Runtime information
    runtime_minutes: Optional[float] = None
    timeout_minutes: Optional[int] = None

    # Whether timeout was exceeded (independent of session_exists)
    timeout_exceeded: bool = False

    # Additional context
    context: dict = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        """Check if this observation represents a terminal state.

        Terminal means the session is no longer running and won't resume.
        This is true for TERMINATED and TIMED_OUT.
        """
        return self.observation in (
            SessionObservation.TERMINATED,
            SessionObservation.TIMED_OUT,
        )

    @classmethod
    def running(cls, runtime_minutes: Optional[float] = None) -> "SessionObservationResult":
        """Create observation for a running session."""
        return cls(
            observation=SessionObservation.RUNNING,
            session_exists=True,
            runtime_minutes=runtime_minutes,
        )

    @classmethod
    def terminated(cls, runtime_minutes: Optional[float] = None) -> "SessionObservationResult":
        """Create observation for a terminated session."""
        return cls(
            observation=SessionObservation.TERMINATED,
            session_exists=False,
            runtime_minutes=runtime_minutes,
        )

    @classmethod
    def timed_out(
        cls,
        runtime_minutes: Optional[float] = None,
        timeout_minutes: Optional[int] = None,
        session_exists: bool = True,
    ) -> "SessionObservationResult":
        """Create observation for a timed-out session."""
        return cls(
            observation=SessionObservation.TIMED_OUT,
            session_exists=session_exists,
            runtime_minutes=runtime_minutes,
            timeout_minutes=timeout_minutes,
            timeout_exceeded=True,
        )
