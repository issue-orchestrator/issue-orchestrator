"""Port definition for issue claim management.

This module defines the protocol for claiming issues in a multi-orchestrator
environment. The claim system ensures only one orchestrator works on an issue
at a time.

Exception contract:
    Methods that read from external storage (check_winner, get_current_claim,
    renew_claim) may raise ``ClaimFetchError`` when the backing store is
    unreachable. Callers must handle this according to their policy:
    - **Write gates** (ClaimGate): fail-closed — block the mutation
    - **Liveness checks** (LeaseRenewer): fail-open — keep the session alive
    - **Stale-claim scans**: skip the issue and retry next tick

    NullClaimManager never raises ClaimFetchError because it has no
    external dependency.
"""

from typing import Protocol

from ..domain.claim import Claim, ClaimResult


class ClaimManager(Protocol):
    """Protocol for managing issue claims.

    The ClaimManager coordinates which orchestrator instance is allowed to
    work on a particular issue. Implementations hide the backing coordination
    primitive, such as GitHub refs or issue comments, behind this behavior-level
    port.

    Implementations must be thread-safe as the orchestrator may call these
    methods from multiple contexts (tick loop, completion handler, etc.).
    """

    def attempt_claim(self, issue_number: int) -> ClaimResult:
        """Attempt to claim an issue.

        This attempts to acquire the backing claim record and adds the claim
        label when acquisition succeeds. Some implementations may acquire
        atomically; callers still invoke run_convergence() as the confirmation
        step.

        Args:
            issue_number: The GitHub issue number to claim.

        Returns:
            ClaimResult with success=True if claim was posted, or
            ClaimResult with error if posting failed.
        """
        ...

    def run_convergence(self, issue_number: int, lease_id: str) -> bool:
        """Run the convergence protocol to confirm claim ownership.

        Confirms that the given lease is the current winner. Atomic
        compare-and-swap implementations should treat this as post-write
        confirmation with bounded read retries for transient backing-store
        failures, not as another race-arbitration step. Legacy comment-backed
        implementations may require repeated winner reads before confirming.

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

        Updates the backing claim record with a new expiry time. Should be
        called periodically for long-running sessions.

        Args:
            issue_number: The GitHub issue number.
            lease_id: The lease_id of the claim to renew.

        Returns:
            True if renewal succeeded, False if claim was lost or
            renewal failed.

        Raises:
            ClaimFetchError: If the backing store is unreachable during
                the ownership verification step.
        """
        ...

    def release_claim(self, issue_number: int, lease_id: str) -> None:
        """Release a claim on an issue.

        Removes the claim label and releases the backing claim record.
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

        Raises:
            ClaimFetchError: If the backing store is unreachable.
        """
        ...

    def get_current_claim(self, issue_number: int) -> Claim | None:
        """Get the current winning claim for an issue.

        Reads the backing coordination store and determines the current winner.

        Args:
            issue_number: The GitHub issue number.

        Returns:
            The winning Claim if one exists, None if no valid claims.

        Raises:
            ClaimFetchError: If the backing store is unreachable.
        """
        ...


class NullClaimManager:
    """No-op ClaimManager for when claims are disabled.

    This follows the "No Nulls" pattern - instead of Optional[ClaimManager],
    we inject NullClaimManager which behaves as if all claims succeed instantly
    and there's never any contention.

    Use this when:
    - Running in single-orchestrator mode
    - Claims are explicitly disabled in config
    - Testing without claim infrastructure
    """

    def attempt_claim(self, issue_number: int) -> ClaimResult:
        """Always succeeds with a dummy lease_id."""
        from ..domain.claim import ClaimResult, ClaimState
        return ClaimResult(
            success=True,
            lease_id=f"null-claim-{issue_number}",
            state=ClaimState.CLAIMED,  # Immediately claimed, no convergence needed
        )

    def run_convergence(self, issue_number: int, lease_id: str) -> bool:
        """Always succeeds - no convergence needed in single-orchestrator mode."""
        # Parameters unused in null implementation
        _ = issue_number, lease_id
        return True

    def renew_claim(self, issue_number: int, lease_id: str) -> bool:
        """Always succeeds - no renewal needed."""
        _ = issue_number, lease_id
        return True

    def release_claim(self, issue_number: int, lease_id: str) -> None:
        """No-op - nothing to release."""
        _ = issue_number, lease_id

    def check_winner(self, issue_number: int, lease_id: str) -> bool:
        """Always the winner - no contention in single-orchestrator mode."""
        _ = issue_number, lease_id
        return True

    def get_current_claim(self, issue_number: int) -> Claim | None:
        """No claims tracked."""
        _ = issue_number
        return None
