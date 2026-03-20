"""LeaseRenewer - handles lease renewal for long-running sessions.

This module manages lease renewal for active sessions to prevent
claim expiration during long-running work.
"""

import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from ..domain.claim import ClaimFetchError

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
        It performs two types of checks:

        1. **Periodic claim verification** (every lease/3 seconds):
           Verifies we're still the claim winner. This catches claim theft
           early, before the renewal window.

        2. **Lease renewal** (when within renewal threshold):
           Renews the lease to extend expiry time.

        Args:
            sessions: List of active sessions to check.

        Returns:
            List of sessions that lost their claim.
            These sessions should be terminated by the caller.
        """
        lost_sessions: list["Session"] = []
        now = datetime.now()
        renewal_trigger_threshold = self._config.renewal_trigger_threshold()
        verification_interval = self._config.lease_seconds // 3  # Check every lease/3

        for session in sessions:
            # Skip sessions without claims
            if not session.lease_id or not session.lease_expires_at:
                continue

            # Check if periodic verification is due (every lease/3 seconds)
            if self._should_verify_claim(session, now, verification_interval):
                if not self._verify_claim_ownership(session):
                    lost_sessions.append(session)
                    continue  # Skip renewal check, session is lost

            # Check if within renewal window
            time_until_expiry = (session.lease_expires_at - now).total_seconds()

            if time_until_expiry <= renewal_trigger_threshold:
                logger.info(
                    "Attempting lease renewal for issue #%d (expires in %.1fs)",
                    session.issue.number,
                    time_until_expiry,
                )

                try:
                    success = self._claim_manager.renew_claim(
                        session.issue.number,
                        session.lease_id,
                    )
                except ClaimFetchError:
                    # API error during renewal — don't treat as claim loss.
                    # The claim may still be valid; we just couldn't verify
                    # or post the renewal. Retry next tick.
                    logger.warning(
                        "API error during lease renewal for issue #%d - "
                        "will retry next tick",
                        session.issue.number,
                    )
                    continue

                if success:
                    # Update expiry time on session
                    new_expiry = now + timedelta(seconds=self._config.lease_seconds)
                    session.lease_expires_at = new_expiry
                    session.last_claim_verified_at = now  # Renewal implies verification

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

    def _should_verify_claim(
        self,
        session: "Session",
        now: datetime,
        interval_seconds: int,
    ) -> bool:
        """Check if periodic claim verification is due.

        Args:
            session: The session to check.
            now: Current time.
            interval_seconds: Verification interval (typically lease/3).

        Returns:
            True if verification is due.
        """
        if session.last_claim_verified_at is None:
            # First check - use lease_acquired_at as baseline
            baseline = session.lease_acquired_at or session.started_at
            elapsed = (now - baseline).total_seconds()
            return elapsed >= interval_seconds

        elapsed = (now - session.last_claim_verified_at).total_seconds()
        return elapsed >= interval_seconds

    def _verify_claim_ownership(self, session: "Session") -> bool:
        """Verify we're still the claim winner.

        Fails OPEN: if the API is unreachable, assumes we still own the
        claim to avoid killing a session due to a transient GitHub blip.
        Updates last_claim_verified_at on success.

        Args:
            session: The session to verify (must have lease_id).

        Returns:
            True if still the winner (or API unreachable), False if
            claim was definitively lost.
        """
        # Defensive check - caller should ensure lease_id exists
        if not session.lease_id:
            return True

        logger.debug(
            "Periodic claim verification for issue #%d",
            session.issue.number,
        )

        try:
            is_winner = self._claim_manager.check_winner(
                session.issue.number,
                session.lease_id,
            )
        except ClaimFetchError:
            logger.warning(
                "Cannot verify claim for issue #%d due to API error - "
                "assuming still owner (fail-open for liveness)",
                session.issue.number,
            )
            return True

        now = datetime.now()
        session.last_claim_verified_at = now

        if not is_winner:
            logger.warning(
                "Claim lost for issue #%d during periodic verification",
                session.issue.number,
            )
            self._emit_claim_lost_event(session, "periodic_verification")
            return False

        return True

    def check_single_session(self, session: "Session") -> bool:
        """Check and potentially renew a single session's lease.

        Fails OPEN: returns True on API errors to avoid killing sessions.

        Args:
            session: The session to check.

        Returns:
            True if the session still owns its claim (or API unreachable),
            False if claim definitively lost.
        """
        if not session.lease_id:
            return True  # No claim system active

        try:
            is_winner = self._claim_manager.check_winner(
                session.issue.number,
                session.lease_id,
            )
        except ClaimFetchError:
            logger.warning(
                "Cannot verify claim for issue #%d during on-demand check - "
                "assuming still owner (fail-open for liveness)",
                session.issue.number,
            )
            return True

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
            from ..ports.event_sink import make_trace_event

            self._events.publish(make_trace_event(
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
            from ..ports.event_sink import make_trace_event

            self._events.publish(make_trace_event(
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
