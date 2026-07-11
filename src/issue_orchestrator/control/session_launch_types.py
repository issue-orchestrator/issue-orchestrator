"""Shared result types for session launch flows."""

from dataclasses import dataclass
from datetime import datetime

from ..domain.models import Session


@dataclass
class LaunchResult:
    """Result of a session launch attempt."""

    session: Session | None
    success: bool
    reason: str = ""
    keep_queued: bool = False  # Terminal already running; keep pending item queued.
    # Launch failed before the session started on a transient required-input
    # error (e.g. triage prep could not read the DB/log/filesystem). The
    # pending item should be RETAINED for retry — bounded by the queue owner
    # (PendingSessionQueues.retain_triage_for_retry), never retried forever.
    retry_queued: bool = False


@dataclass
class ClaimAcquisitionResult:
    """Result of attempting to acquire a distributed claim for an issue.

    Used to track claim state through the launch process so cleanup
    can release claims on failure.
    """

    success: bool
    lease_id: str | None = None
    lease_acquired_at: datetime | None = None
    lease_expires_at: datetime | None = None
    error: str | None = None

    def as_launch_failure(self) -> LaunchResult:
        """Convert a failed claim to a launch result."""
        return LaunchResult(None, False, self.error or "Claim acquisition failed")
