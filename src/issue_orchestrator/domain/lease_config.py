"""Configuration for the lease/claim system.

This module defines the configuration parameters for distributed issue
coordination between multiple orchestrator instances.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class LeaseConfig:
    """Configuration for lease timing and convergence behavior.

    Attributes:
        lease_seconds: How long a lease is valid before expiring (default 15 min)
        renew_interval_seconds: How often to renew (default 5 min before expiry)
        convergence_timeout_seconds: Max time to wait for convergence
        convergence_poll_min_ms: Minimum poll interval during convergence
        convergence_poll_max_ms: Maximum poll interval (jitter range)
    """

    lease_seconds: int = 900  # 15 minutes
    renew_interval_seconds: int = 300  # 5 minutes
    convergence_timeout_seconds: float = 5.0
    convergence_poll_min_ms: int = 250
    convergence_poll_max_ms: int = 500
    convergence_max_polls: int = 15  # Safety limit on API calls during convergence

    @classmethod
    def for_testing(cls) -> "LeaseConfig":
        """Create a config with short times for faster E2E tests."""
        return cls(
            lease_seconds=30,  # 30 seconds
            renew_interval_seconds=10,  # 10 seconds
            convergence_timeout_seconds=3.0,
            convergence_poll_min_ms=100,
            convergence_poll_max_ms=200,
        )

    def renewal_trigger_threshold(self) -> int:
        """Get the time-remaining threshold that triggers renewal.

        Returns:
            When this many seconds remain before expiry, renewal should
            be attempted. E.g., with 15-min lease and 5-min renew interval,
            returns 600 (renew when 10 min remain = 5 min before expiry).
        """
        return self.lease_seconds - self.renew_interval_seconds


@dataclass(frozen=True)
class ClaimConfig:
    """Top-level claim system configuration.

    Attributes:
        enabled: Whether the claim system is active
        lease: Lease timing configuration
        claimant_id: Unique identifier for this orchestrator instance
    """

    enabled: bool = False
    lease: LeaseConfig = LeaseConfig()
    claimant_id: str = ""

    def __post_init__(self) -> None:
        """Validate configuration."""
        if self.enabled and not self.claimant_id:
            # Generate a default claimant_id if not provided
            import socket
            import os
            object.__setattr__(
                self,
                "claimant_id",
                f"{socket.gethostname()}-{os.getpid()}",
            )
