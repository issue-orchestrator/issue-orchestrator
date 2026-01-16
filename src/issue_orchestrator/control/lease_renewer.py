"""LeaseRenewer - handles lease renewal for long-running sessions.

This module manages lease renewal for active sessions to prevent
claim expiration during long-running work.
"""

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..ports.claim_manager import ClaimManager
    from ..ports.event_sink import EventSink
    from ..domain.lease_config import LeaseConfig
    from ..domain.models import Session

logger = logging.getLogger(__name__)


class LeaseRenewer:
    """Renews leases for active sessions during orchestrator tick.

    The LeaseRenewer is called periodically (typically each tick) to
    check if any active sessions need their leases renewed. It handles:
    - Checking which sessions are approaching lease expiry
    - Renewing leases before they expire
    - Detecting and reporting sessions that have lost their claims

    Usage in orchestrator tick:
        renewer = LeaseRenewer(claim_manager, events, config)
        lost_sessions = renewer.check_renewals(state.active_sessions)
        for session in lost_sessions:
            handle_claim_loss(session)
    """

    def __init__(
        self,
        claim_manager: "ClaimManager",
        events: "EventSink",
        config: "LeaseConfig",
    ):
        """Initialize the lease renewer.

        Args:
            claim_manager: ClaimManager for renewing claims.
            events: EventSink for emitting renewal events.
            config: LeaseConfig with renewal timing settings.
        """
        self._claim_manager = claim_manager
        self._events = events
        self._config = config

    def check_renewals(self, sessions: list["Session"]) -> list["Session"]:
        """Check and renew leases for active sessions.

        This should be called periodically (e.g., each orchestrator tick).
        It checks each session's lease and renews if within the renewal
        threshold.

        Args:
            sessions: List of active sessions to check.

        Returns:
            List of sessions that lost their claim (renewal failed).
            These sessions should be terminated by the caller.
        """
        lost_sessions: list["Session"] = []
        now = datetime.now()
        renewal_threshold_seconds = self._config.renewal_threshold_seconds()

        for session in sessions:
            # Skip sessions without claims
            if not session.lease_id or not session.lease_expires_at:
                continue

            # Check if within renewal window
            time_until_expiry = (session.lease_expires_at - now).total_seconds()

            if time_until_expiry <= renewal_threshold_seconds:
                logger.info(
                    "Attempting lease renewal for issue #%d (expires in %.1fs)",
                    session.issue.number,
                    time_until_expiry,
                )

                success = self._claim_manager.renew_claim(
                    session.issue.number,
                    session.lease_id,
                )

                if success:
                    # Update expiry time on session
                    new_expiry = now + timedelta(seconds=self._config.lease_seconds)
                    # Note: Session is a dataclass, so we need to update in place
                    # This requires the session to be mutable or the caller to handle updates
                    session.lease_expires_at = new_expiry

                    logger.info(
                        "Renewed lease for issue #%d (new expiry: %s)",
                        session.issue.number,
                        new_expiry,
                    )
                    self._emit_renewal_event(session, new_expiry)
                else:
                    logger.warning(
                        "Failed to renew lease for issue #%d - claim lost",
                        session.issue.number,
                    )
                    lost_sessions.append(session)
                    self._emit_claim_lost_event(session, "renewal_failed")

        return lost_sessions

    def check_single_session(self, session: "Session") -> bool:
        """Check and potentially renew a single session's lease.

        This is useful for on-demand renewal checks, e.g., before
        starting a long-running operation.

        Args:
            session: The session to check.

        Returns:
            True if the session still owns its claim, False if lost.
        """
        if not session.lease_id:
            return True  # No claim system active

        is_winner = self._claim_manager.check_winner(
            session.issue.number,
            session.lease_id,
        )

        if not is_winner:
            logger.warning(
                "Claim lost for issue #%d during on-demand check",
                session.issue.number,
            )
            self._emit_claim_lost_event(session, "on_demand_check")
            return False

        return True

    def _emit_renewal_event(self, session: "Session", new_expiry: datetime) -> None:
        """Emit lease renewed event."""
        try:
            from ..events.catalog import EventName
            from ..ports.event_sink import TraceEvent

            self._events.publish(TraceEvent(
                EventName.CLAIM_RENEWED,
                {
                    "issue_number": session.issue.number,
                    "lease_id": session.lease_id,
                    "new_expiry": new_expiry.isoformat(),
                },
            ))
        except Exception as e:
            logger.debug("Failed to emit renewal event: %s", e)

    def _emit_claim_lost_event(self, session: "Session", reason: str) -> None:
        """Emit claim lost event."""
        try:
            from ..events.catalog import EventName
            from ..ports.event_sink import TraceEvent

            self._events.publish(TraceEvent(
                EventName.CLAIM_LOST,
                {
                    "issue_number": session.issue.number,
                    "lease_id": session.lease_id,
                    "session_id": session.terminal_id,
                    "reason": reason,
                },
            ))
        except Exception as e:
            logger.debug("Failed to emit claim lost event: %s", e)
