"""Implementation of the VerificationService.

This module provides a centralized verification service for write-verify
patterns. It handles retry budgets, error classification, and circuit
breaker logic.
"""

import logging
import random
import time
from dataclasses import dataclass
from typing import Any, Callable

from ..ports.verification import (
    ErrorClassification,
    VerificationBudget,
    VerificationResult,
    VerificationState,
)

logger = logging.getLogger(__name__)


# Default budget for GitHub write verification
DEFAULT_GH_BUDGET = VerificationBudget(
    timeout_seconds=10.0,
    max_attempts=10,
    initial_delay_ms=250,
    max_delay_ms=2000,
    backoff_factor=1.5,
    jitter_ms=0,
)


@dataclass
class CircuitBreakerState:
    """State for the circuit breaker."""
    failure_count: int = 0
    last_failure_time: float | None = None
    is_open: bool = False
    # Open circuit after this many consecutive failures
    open_threshold: int = 5
    # Auto-close after this many seconds
    reset_timeout_seconds: float = 60.0


class DefaultVerificationService:
    """Default implementation of VerificationService.

    This service handles:
    - Retry budgets (time and attempt limits)
    - Exponential backoff with jitter
    - Error classification (retryable vs fatal)
    - Circuit breaker (pauses verification after repeated failures)
    """

    def __init__(
        self,
        default_budget: VerificationBudget | None = None,
        circuit_breaker_threshold: int = 5,
        circuit_breaker_timeout: float = 60.0,
    ):
        """Initialize the verification service.

        Args:
            default_budget: Default budget for verification operations.
            circuit_breaker_threshold: Number of consecutive failures to open circuit.
            circuit_breaker_timeout: Seconds before auto-closing the circuit.
        """
        self._default_budget = default_budget or DEFAULT_GH_BUDGET
        self._circuit = CircuitBreakerState(
            open_threshold=circuit_breaker_threshold,
            reset_timeout_seconds=circuit_breaker_timeout,
        )

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
            operation: Name of the operation being verified
            target: Target entity for logging
            check: Callable that returns (success, observed_state)
            budget: Retry budget. If None, uses default budget.

        Returns:
            Tuple of (result, last_observed_state)
        """
        effective_budget = budget or self._default_budget

        # Check circuit breaker
        if self.is_circuit_open:
            logger.warning(
                "[VERIFY] Circuit breaker open, skipping verification for %s on %s",
                operation,
                target,
            )
            return (VerificationResult.FAILED_FATAL, None)

        start_time = time.monotonic()
        deadline = start_time + effective_budget.timeout_seconds
        delay_ms = effective_budget.initial_delay_ms
        max_delay_ms = effective_budget.max_delay_ms
        attempt = 0
        last_observed: Any = None
        last_error: Exception | None = None

        while attempt < effective_budget.max_attempts:
            attempt += 1
            elapsed = time.monotonic() - start_time

            _state = VerificationState(
                operation=operation,
                target=target,
                attempt=attempt,
                elapsed_seconds=elapsed,
                last_observed=last_observed,
                last_error=last_error,
            )

            try:
                success, observed = check()
                last_observed = observed

                if success:
                    logger.debug(
                        "[VERIFY] %s on %s succeeded after %d attempts (%.2fs)",
                        operation,
                        target,
                        attempt,
                        elapsed,
                    )
                    self._record_success()
                    return (VerificationResult.SUCCESS, observed)

            except Exception as e:
                last_error = e
                classification = self.classify_error(e)

                if classification == ErrorClassification.FATAL:
                    logger.warning(
                        "[VERIFY] %s on %s failed with fatal error: %s",
                        operation,
                        target,
                        e,
                    )
                    self._record_failure()
                    return (VerificationResult.FAILED_FATAL, last_observed)

                if classification == ErrorClassification.NEEDS_RECONCILE:
                    logger.warning(
                        "[VERIFY] %s on %s needs reconciliation: %s",
                        operation,
                        target,
                        e,
                    )
                    self._record_failure()
                    return (VerificationResult.FAILED_FATAL, last_observed)

                logger.debug(
                    "[VERIFY] %s on %s attempt %d failed (retryable): %s",
                    operation,
                    target,
                    attempt,
                    e,
                )

            # Check time budget
            if time.monotonic() >= deadline:
                logger.warning(
                    "[VERIFY] %s on %s timed out after %d attempts (%.2fs). Last observed: %s",
                    operation,
                    target,
                    attempt,
                    effective_budget.timeout_seconds,
                    last_observed,
                )
                self._record_failure()
                return (VerificationResult.TIMED_OUT, last_observed)

            # Calculate sleep duration with backoff and jitter
            sleep_seconds = delay_ms / 1000.0
            if effective_budget.jitter_ms > 0:
                sleep_seconds += random.uniform(0, effective_budget.jitter_ms / 1000.0)

            # Don't sleep past deadline
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            sleep_seconds = min(sleep_seconds, remaining)

            time.sleep(sleep_seconds)

            # Apply backoff for next iteration
            delay_ms = min(delay_ms * effective_budget.backoff_factor, max_delay_ms)

        logger.warning(
            "[VERIFY] %s on %s failed after %d attempts. Last observed: %s",
            operation,
            target,
            attempt,
            last_observed,
        )
        self._record_failure()
        return (VerificationResult.FAILED_RETRYABLE, last_observed)

    def classify_error(self, error: Exception) -> ErrorClassification:
        """Classify an error to determine retry behavior.

        This method uses heuristics based on common error patterns.

        Args:
            error: The exception to classify

        Returns:
            Classification determining whether to retry
        """
        error_msg = str(error).lower()

        # Fatal errors - no point retrying
        fatal_patterns = [
            "not found",
            "404",
            "permission denied",
            "403",
            "authentication",
            "unauthorized",
            "401",
            "invalid",
            "bad request",
            "400",
        ]
        for pattern in fatal_patterns:
            if pattern in error_msg:
                return ErrorClassification.FATAL

        # Needs reconciliation - state is inconsistent
        reconcile_patterns = [
            "conflict",
            "409",
            "already exists",
            "duplicate",
        ]
        for pattern in reconcile_patterns:
            if pattern in error_msg:
                return ErrorClassification.NEEDS_RECONCILE

        # Default to retryable for unknown errors
        return ErrorClassification.RETRYABLE

    @property
    def is_circuit_open(self) -> bool:
        """Check if the circuit breaker is open.

        The circuit auto-closes after the timeout period.
        """
        if not self._circuit.is_open:
            return False

        # Check if timeout has elapsed
        if self._circuit.last_failure_time is not None:
            elapsed = time.monotonic() - self._circuit.last_failure_time
            if elapsed >= self._circuit.reset_timeout_seconds:
                logger.info("[VERIFY] Circuit breaker auto-closed after timeout")
                self.reset_circuit()
                return False

        return True

    def reset_circuit(self) -> None:
        """Reset the circuit breaker."""
        self._circuit.failure_count = 0
        self._circuit.is_open = False
        self._circuit.last_failure_time = None

    def _record_success(self) -> None:
        """Record a successful verification (resets failure count)."""
        self._circuit.failure_count = 0

    def _record_failure(self) -> None:
        """Record a failed verification (may open circuit)."""
        self._circuit.failure_count += 1
        self._circuit.last_failure_time = time.monotonic()

        if self._circuit.failure_count >= self._circuit.open_threshold:
            if not self._circuit.is_open:
                logger.warning(
                    "[VERIFY] Circuit breaker opened after %d consecutive failures",
                    self._circuit.failure_count,
                )
            self._circuit.is_open = True


def create_verification_service_from_config(config) -> DefaultVerificationService:
    """Create a verification service from config.

    Args:
        config: Config object with gh_write_verify_* settings

    Returns:
        Configured verification service
    """
    budget = VerificationBudget(
        timeout_seconds=getattr(config, 'gh_write_verify_timeout_seconds', 10.0),
        max_attempts=10,
        initial_delay_ms=getattr(config, 'gh_write_verify_initial_delay_ms', 250),
        max_delay_ms=getattr(config, 'gh_write_verify_max_delay_ms', 2000),
        backoff_factor=getattr(config, 'gh_write_verify_backoff', 1.5),
        jitter_ms=getattr(config, 'gh_write_verify_jitter_ms', 0),
    )
    return DefaultVerificationService(default_budget=budget)
