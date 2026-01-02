"""Port interface for write verification service.

This module defines the protocol for a centralized verification service
that handles write-verify patterns with retry budgets, classifiers, and
circuit breakers.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Protocol, Callable, TypeVar, Any
import logging

logger = logging.getLogger(__name__)


class VerificationResult(Enum):
    """Result of a verification attempt."""
    SUCCESS = "success"
    FAILED_RETRYABLE = "failed_retryable"  # Failed but can retry
    FAILED_FATAL = "failed_fatal"  # Failed and should not retry
    TIMED_OUT = "timed_out"  # Hit timeout budget


class FailureType(Enum):
    """Classification of verification failure scope.

    Used to determine how the orchestrator should respond to failures:
    - SYSTEMIC: API down, rate limited, network issues -> pause orchestrator, probe, resume
    - ISSUE_LOCAL: Write didn't take effect for this issue -> apply needs-reconcile label, skip
    """
    SYSTEMIC = "systemic"  # API/infrastructure failure affecting all operations
    ISSUE_LOCAL = "issue_local"  # Failure specific to one issue


class ErrorClassification(Enum):
    """Classification of an error for retry decisions."""
    RETRYABLE = "retryable"  # Transient error, retry is appropriate
    FATAL = "fatal"  # Permanent error, do not retry
    NEEDS_RECONCILE = "needs_reconcile"  # State inconsistent, needs manual intervention


@dataclass(frozen=True)
class VerificationBudget:
    """Budget constraints for verification attempts."""
    timeout_seconds: float = 10.0  # Total time budget
    max_attempts: int = 10  # Maximum number of attempts
    initial_delay_ms: int = 250  # Initial delay between attempts
    max_delay_ms: int = 2000  # Maximum delay between attempts
    backoff_factor: float = 1.5  # Delay multiplier after each attempt
    jitter_ms: int = 0  # Random jitter to add to delay


@dataclass
class VerificationState:
    """State of a verification attempt for logging/diagnostics."""
    operation: str  # What operation is being verified
    target: str  # What entity (e.g., issue number)
    attempt: int  # Current attempt number
    elapsed_seconds: float  # Time elapsed so far
    last_observed: Any | None  # Last observed state (for logging)
    last_error: Exception | None  # Last error encountered


T = TypeVar('T')


class VerificationService(Protocol):
    """Protocol for verification services.

    A verification service handles the retry/backoff logic for write-verify
    patterns. It provides:
    - Retry budgets (time and attempt limits)
    - Error classification (retryable vs fatal)
    - Circuit breaker support (pause vs needs-reconcile)
    """

    def verify_condition(
        self,
        *,
        operation: str,
        target: str,
        check: Callable[[], tuple[bool, Any]],
        budget: VerificationBudget | None = None,
    ) -> tuple[VerificationResult, Any]:
        """Verify a condition with retries.

        Args:
            operation: Name of the operation being verified (for logging)
            target: Target entity (e.g., "issue#123") for logging
            check: Callable that returns (success, observed_state).
                   The observed_state is logged on failure.
            budget: Retry budget. If None, uses default budget.

        Returns:
            Tuple of (result, last_observed_state)
        """
        ...

    def classify_error(self, error: Exception) -> ErrorClassification:
        """Classify an error to determine retry behavior.

        Args:
            error: The exception to classify

        Returns:
            Classification determining whether to retry
        """
        ...

    @property
    def is_circuit_open(self) -> bool:
        """Check if the circuit breaker is open (too many failures)."""
        ...

    def reset_circuit(self) -> None:
        """Reset the circuit breaker."""
        ...
