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
from datetime import datetime
from enum import Enum
from typing import Optional

from ..ports.provider_readiness import ProviderReadiness
from ..domain.provider_lane import ProviderLane


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

    # The provider this session runs on is not authenticated, confirmed by the
    # provider's own credential probe. Deliberately NOT TIMED_OUT: an auth-dead
    # session sits at its login banner for the full timeout, and reporting that
    # as a timeout is what misdirected four failure investigations toward issue
    # substance on 2026-08-04 (#6999).
    PROVIDER_AUTH_FAILED = "provider_auth_failed"

    # The provider reported that this session's independently billed lane has
    # no usable capacity. Interactive providers do not use provider_runner, so
    # this live observation is their typed path into the circuit owner.
    PROVIDER_QUOTA_EXHAUSTED = "provider_quota_exhausted"


@dataclass(frozen=True)
class ProviderQuotaObservation:
    """Observed quota exhaustion, including its lane and chronology."""

    lane: ProviderLane
    error_summary: str
    observed_at: datetime


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

    # Why the provider could not do work, when that is the observation. Typed
    # so control never re-reads a banner: the provider adapter already decided.
    provider_readiness: Optional[ProviderReadiness] = None
    provider_quota: Optional[ProviderQuotaObservation] = None

    def __post_init__(self) -> None:
        """Enforce the cross-field invariant on the type, not on one factory.

        A ``PROVIDER_AUTH_FAILED`` observation is only meaningful with a named,
        auth-expired readiness: control hands that provider to the circuit owner
        and the provider-impact command, so a missing or ``READY`` value would
        terminate the session while recording the outage nowhere. Checking it
        here means no construction path can bypass it — a convenience
        classmethod is not a boundary (#6999 F9).
        """
        if self.observation is SessionObservation.PROVIDER_AUTH_FAILED:
            readiness = self.provider_readiness
            if readiness is None or not readiness.human_fixable or not readiness.provider:
                raise ValueError(
                    "a PROVIDER_AUTH_FAILED observation requires a named, "
                    f"auth-expired ProviderReadiness; got {readiness!r}"
                )
        if (
            self.observation is SessionObservation.PROVIDER_QUOTA_EXHAUSTED
            and self.provider_quota is None
        ):
            raise ValueError(
                "a PROVIDER_QUOTA_EXHAUSTED observation requires typed quota evidence"
            )

    @property
    def is_terminal(self) -> bool:
        """Check if this observation represents a terminal state.

        Terminal means the session is no longer running and won't resume.
        This is true for TERMINATED, TIMED_OUT, and PROVIDER_AUTH_FAILED.
        """
        return self.observation in (
            SessionObservation.TERMINATED,
            SessionObservation.TIMED_OUT,
            SessionObservation.PROVIDER_AUTH_FAILED,
            SessionObservation.PROVIDER_QUOTA_EXHAUSTED,
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

    @classmethod
    def provider_auth_failed(
        cls,
        readiness: ProviderReadiness,
        runtime_minutes: Optional[float] = None,
        session_exists: bool = True,
    ) -> "SessionObservationResult":
        """Create observation for a session whose provider is not authenticated.

        The auth-expired invariant is enforced by ``__post_init__``, so it holds
        for this constructor and for direct dataclass construction alike.
        """
        return cls(
            observation=SessionObservation.PROVIDER_AUTH_FAILED,
            session_exists=session_exists,
            runtime_minutes=runtime_minutes,
            provider_readiness=readiness,
        )

    @classmethod
    def provider_quota_exhausted(
        cls,
        quota: ProviderQuotaObservation,
        runtime_minutes: Optional[float] = None,
        session_exists: bool = True,
    ) -> "SessionObservationResult":
        """Create an observation for an exhausted provider quota lane."""
        return cls(
            observation=SessionObservation.PROVIDER_QUOTA_EXHAUSTED,
            session_exists=session_exists,
            runtime_minutes=runtime_minutes,
            provider_quota=quota,
        )
