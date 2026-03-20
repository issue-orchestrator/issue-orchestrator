"""ClaimGate - verifies claim ownership before write operations.

This module provides write-boundary protection for multi-orchestrator
coordination. Before any external mutation (git push, label change, comment),
the ClaimGate verifies the current orchestrator still owns the claim.
"""

import logging
from typing import TYPE_CHECKING

from ..domain.claim import ClaimFetchError

if TYPE_CHECKING:
    from ..ports.claim_manager import ClaimManager
    from ..ports.event_sink import EventSink

logger = logging.getLogger(__name__)


class ClaimLostError(Exception):
    """Raised when claim is lost before a write operation.

    This error indicates the orchestrator no longer has the right to
    modify the issue because another orchestrator has won the claim.
    """

    def __init__(self, issue_number: int, operation: str):
        self.issue_number = issue_number
        self.operation = operation
        super().__init__(
            f"Claim lost for issue #{issue_number} before {operation}"
        )


class ClaimGate:
    """Verifies claim ownership before external writes.

    The ClaimGate is used at write boundaries to ensure the current
    orchestrator still owns the claim for an issue before making
    any external mutations. This prevents conflicts when multiple
    orchestrators are coordinating on the same repository.

    Usage:
        gate = ClaimGate(claim_manager, events)

        # Before any write operation:
        if not gate.verify_before_write(issue_number, lease_id, "push"):
            # Don't proceed with write
            return

        # Or use the verify_or_raise helper:
        gate.verify_or_raise(issue_number, lease_id, "add_label")
        # Will raise ClaimLostError if not owner
    """

    def __init__(
        self,
        claim_manager: "ClaimManager",
        events: "EventSink",
    ):
        """Initialize the claim gate.

        Args:
            claim_manager: ClaimManager for checking claim ownership.
            events: EventSink for emitting claim lost events.
        """
        self._claim_manager = claim_manager
        self._events = events

    def verify_before_write(
        self,
        issue_number: int,
        lease_id: str | None,
        operation: str,
    ) -> bool:
        """Verify claim ownership before a write operation.

        Fails CLOSED: if ownership cannot be verified (API error), the
        write is blocked. This is the opposite of liveness checks
        (LeaseRenewer), which fail open to avoid killing sessions.

        Args:
            issue_number: The GitHub issue number.
            lease_id: The session's lease_id, or None if no claim system.
            operation: Description of the operation (for logging/events).

        Returns:
            True if write should proceed, False if claim lost or
            ownership could not be verified.
        """
        # No lease_id means no claim system active - allow write
        if not lease_id:
            return True

        try:
            is_winner = self._claim_manager.check_winner(issue_number, lease_id)
        except ClaimFetchError:
            logger.warning(
                "Cannot verify claim for issue #%d before %s - "
                "blocking write (fail-closed)",
                issue_number,
                operation,
            )
            return False

        if not is_winner:
            logger.warning(
                "Claim lost for issue #%d before %s (lease_id=%s)",
                issue_number,
                operation,
                lease_id,
            )
            self._emit_claim_lost_event(issue_number, lease_id, operation)
            return False

        logger.debug(
            "Claim verified for issue #%d before %s",
            issue_number,
            operation,
        )
        return True

    def verify_or_raise(
        self,
        issue_number: int,
        lease_id: str | None,
        operation: str,
    ) -> None:
        """Verify claim ownership or raise ClaimLostError.

        This is a throwing version of verify_before_write for use
        in code paths where an exception is the appropriate response.

        Args:
            issue_number: The GitHub issue number.
            lease_id: The session's lease_id, or None if no claim system.
            operation: Description of the operation (for logging/events).

        Raises:
            ClaimLostError: If the claim has been lost.
        """
        if not self.verify_before_write(issue_number, lease_id, operation):
            raise ClaimLostError(issue_number, operation)

    def _emit_claim_lost_event(
        self,
        issue_number: int,
        lease_id: str,
        operation: str,
    ) -> None:
        """Emit claim lost before write event."""
        try:
            from ..events.catalog import EventName
            from ..ports.event_sink import make_trace_event

            self._events.publish(make_trace_event(
                EventName.CLAIM_LOST_BEFORE_WRITE,
                {
                    "issue_number": issue_number,
                    "lease_id": lease_id,
                    "operation": operation,
                },
            ))
        except Exception as e:
            logger.debug("Failed to emit claim lost event: %s", e)
