"""Port definition for issue claim management.

This module defines the protocol for claiming issues in a multi-orchestrator
environment. The claim system ensures only one orchestrator works on an issue
at a time.
"""

from typing import Protocol

from ..domain.claim import Claim, ClaimResult


class ClaimManager(Protocol):
    """Protocol for managing issue claims.

    The ClaimManager coordinates which orchestrator instance is allowed to
    work on a particular issue. Claims are stored as comments on GitHub issues
    and resolved through a convergence protocol.

    Implementations must be thread-safe as the orchestrator may call these
    methods from multiple contexts (tick loop, completion handler, etc.).
    """

    def attempt_claim(self, issue_number: int) -> ClaimResult:
        """Attempt to claim an issue.

        This creates a claim comment on the issue and adds the claim label.
        Does NOT run convergence - caller must call run_convergence() after.

        Args:
            issue_number: The GitHub issue number to claim.

        Returns:
            ClaimResult with success=True if claim was posted, or
            ClaimResult with error if posting failed.
        """
        ...

    def run_convergence(self, issue_number: int, lease_id: str) -> bool:
        """Run the convergence protocol to confirm claim ownership.

        Polls the issue comments to verify we are the winner. Requires
        seeing ourselves as winner for N consecutive polls (configured via
        LeaseConfig.convergence_required_wins).

        Args:
            issue_number: The GitHub issue number.
            lease_id: Our claim's lease_id to check.

        Returns:
            True if convergence succeeded (we are the winner),
            False if we lost to another claimant or timed out.
        """
        ...

    def renew_claim(self, issue_number: int, lease_id: str) -> bool:
        """Renew an existing claim's lease.

        Updates the claim comment with a new expiry time. Should be called
        periodically for long-running sessions.

        Args:
            issue_number: The GitHub issue number.
            lease_id: The lease_id of the claim to renew.

        Returns:
            True if renewal succeeded, False if claim was lost or
            renewal failed.
        """
        ...

    def release_claim(self, issue_number: int, lease_id: str) -> None:
        """Release a claim on an issue.

        Removes the claim label and optionally deletes/edits the claim comment.
        Called when a session completes or is terminated.

        Args:
            issue_number: The GitHub issue number.
            lease_id: The lease_id of the claim to release.
        """
        ...

    def check_winner(self, issue_number: int, lease_id: str) -> bool:
        """Check if we are currently the winning claimant.

        Used for write-boundary verification before external mutations.
        Does a single fresh read (no convergence loop).

        Args:
            issue_number: The GitHub issue number.
            lease_id: Our claim's lease_id to check.

        Returns:
            True if we are the current winner, False otherwise.
        """
        ...

    def get_current_claim(self, issue_number: int) -> Claim | None:
        """Get the current winning claim for an issue.

        Reads all claim comments and determines the current winner.

        Args:
            issue_number: The GitHub issue number.

        Returns:
            The winning Claim if one exists, None if no valid claims.
        """
        ...
