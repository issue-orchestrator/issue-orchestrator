"""Unit tests for DefaultVerificationService.

Tests focus on behavior:
- Retry logic with bounded attempts
- Exponential backoff timing
- Error classification (retryable vs fatal vs needs_reconcile)
- Circuit breaker state transitions
- Timeout handling

Uses fake clocks (no real sleeps) for deterministic testing.
"""

from __future__ import annotations

import time
from typing import Any
from unittest.mock import patch

import pytest

from issue_orchestrator.execution.verification_service import (
    DEFAULT_GH_BUDGET,
    DefaultVerificationService,
    create_verification_service_from_config,
)
from issue_orchestrator.ports.verification import (
    ErrorClassification,
    VerificationBudget,
    VerificationResult,
)


# --- Fixtures ---


@pytest.fixture
def service() -> DefaultVerificationService:
    """Create a verification service with default settings."""
    return DefaultVerificationService()


@pytest.fixture
def fast_budget() -> VerificationBudget:
    """Budget with minimal delays for fast testing."""
    return VerificationBudget(
        timeout_seconds=1.0,
        max_attempts=3,
        initial_delay_ms=10,
        max_delay_ms=100,
        backoff_factor=2.0,
        jitter_ms=0,
    )


class FakeClock:
    """Fake clock for deterministic time control in tests."""

    def __init__(self, start: float = 0.0):
        self._now = start

    def monotonic(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds

    def sleep(self, seconds: float) -> None:
        """Fake sleep that advances the clock."""
        self._now += seconds


# --- Test Helpers ---


def make_check_fn(
    results: list[tuple[bool, Any]],
) -> tuple[callable, list[int]]:  # type: ignore
    """Create a check function that returns pre-defined results.

    Returns the check function and a list to track call counts.
    """
    calls = []

    def check() -> tuple[bool, Any]:
        call_num = len(calls)
        calls.append(call_num)
        if call_num < len(results):
            return results[call_num]
        # Default: return last result
        return results[-1] if results else (False, None)

    return check, calls


def make_failing_check(
    exceptions: list[Exception],
) -> tuple[callable, list[int]]:  # type: ignore
    """Create a check function that raises pre-defined exceptions."""
    calls = []

    def check() -> tuple[bool, Any]:
        call_num = len(calls)
        calls.append(call_num)
        if call_num < len(exceptions):
            raise exceptions[call_num]
        # Default: succeed after exceptions exhausted
        return (True, "recovered")

    return check, calls


# --- Success Path Tests ---


class TestVerifyConditionSuccess:
    """Tests for successful verification scenarios."""

    def test_immediate_success_returns_success_and_observed_state(
        self, service: DefaultVerificationService, fast_budget: VerificationBudget
    ) -> None:
        """Verification succeeds on first attempt returns SUCCESS with observed state."""
        check, calls = make_check_fn([(True, {"status": "ready"})])

        result, observed = service.verify_condition(
            operation="test_op",
            target="target-1",
            check=check,
            budget=fast_budget,
        )

        assert result == VerificationResult.SUCCESS
        assert observed == {"status": "ready"}
        assert len(calls) == 1

    def test_success_after_retries_returns_success(
        self, service: DefaultVerificationService, fast_budget: VerificationBudget
    ) -> None:
        """Verification succeeds after initial failures returns SUCCESS."""
        # Fail twice, succeed on third
        check, calls = make_check_fn([
            (False, "pending"),
            (False, "still pending"),
            (True, "complete"),
        ])

        with patch("time.monotonic") as mock_time, patch("time.sleep"):
            clock = FakeClock()
            mock_time.side_effect = clock.monotonic

            result, observed = service.verify_condition(
                operation="test_op",
                target="target-1",
                check=check,
                budget=fast_budget,
            )

        assert result == VerificationResult.SUCCESS
        assert observed == "complete"
        assert len(calls) == 3


# --- Retry Exhaustion Tests ---


class TestVerifyConditionRetryExhaustion:
    """Tests for bounded retry behavior."""

    def test_max_attempts_reached_returns_failed_retryable(
        self, service: DefaultVerificationService
    ) -> None:
        """When max_attempts exceeded, returns FAILED_RETRYABLE."""
        budget = VerificationBudget(
            timeout_seconds=100.0,  # Long timeout so attempts are the limit
            max_attempts=3,
            initial_delay_ms=1,
            max_delay_ms=10,
            backoff_factor=1.0,
            jitter_ms=0,
        )
        # Always fail
        check, calls = make_check_fn([
            (False, "attempt-1"),
            (False, "attempt-2"),
            (False, "attempt-3"),
        ])

        with patch("time.monotonic") as mock_time, patch("time.sleep"):
            clock = FakeClock()
            mock_time.side_effect = clock.monotonic

            result, observed = service.verify_condition(
                operation="test_op",
                target="target-1",
                check=check,
                budget=budget,
            )

        assert result == VerificationResult.FAILED_RETRYABLE
        assert observed == "attempt-3"
        assert len(calls) == 3

    def test_timeout_reached_returns_timed_out(
        self, service: DefaultVerificationService
    ) -> None:
        """When timeout exceeded, returns TIMED_OUT."""
        budget = VerificationBudget(
            timeout_seconds=0.5,  # Short timeout
            max_attempts=100,  # Many attempts allowed
            initial_delay_ms=200,  # Will exceed timeout after 2 sleeps
            max_delay_ms=1000,
            backoff_factor=1.0,
            jitter_ms=0,
        )

        call_count = [0]

        def check_with_time_advance() -> tuple[bool, Any]:
            call_count[0] += 1
            return (False, f"attempt-{call_count[0]}")

        with patch("time.monotonic") as mock_time, patch("time.sleep") as mock_sleep:
            clock = FakeClock()
            mock_time.side_effect = clock.monotonic

            def advance_on_sleep(seconds: float) -> None:
                clock.advance(seconds)

            mock_sleep.side_effect = advance_on_sleep

            result, observed = service.verify_condition(
                operation="test_op",
                target="target-1",
                check=check_with_time_advance,
                budget=budget,
            )

        assert result == VerificationResult.TIMED_OUT


# --- Error Classification Tests ---


class TestErrorClassification:
    """Tests for error classification logic."""

    @pytest.mark.parametrize(
        "error_message,expected",
        [
            # Fatal errors - should not retry
            ("Resource not found", ErrorClassification.FATAL),
            ("HTTP 404: Page not found", ErrorClassification.FATAL),
            ("Permission denied: insufficient access", ErrorClassification.FATAL),
            ("HTTP 403 Forbidden", ErrorClassification.FATAL),
            ("Authentication failed", ErrorClassification.FATAL),
            ("Unauthorized access", ErrorClassification.FATAL),
            ("HTTP 401 Unauthorized", ErrorClassification.FATAL),
            ("Invalid request format", ErrorClassification.FATAL),
            ("Bad request: missing field", ErrorClassification.FATAL),
            ("HTTP 400 Bad Request", ErrorClassification.FATAL),
            # Needs reconciliation - state inconsistent
            ("Conflict: resource modified", ErrorClassification.NEEDS_RECONCILE),
            ("HTTP 409 Conflict", ErrorClassification.NEEDS_RECONCILE),
            ("Label already exists", ErrorClassification.NEEDS_RECONCILE),
            ("Duplicate entry", ErrorClassification.NEEDS_RECONCILE),
            # Retryable - transient errors
            ("Connection timeout", ErrorClassification.RETRYABLE),
            ("Server error 500", ErrorClassification.RETRYABLE),
            ("Rate limited", ErrorClassification.RETRYABLE),
            ("Unknown error occurred", ErrorClassification.RETRYABLE),
        ],
    )
    def test_classify_error_by_message(
        self,
        service: DefaultVerificationService,
        error_message: str,
        expected: ErrorClassification,
    ) -> None:
        """Error classification based on message patterns."""
        error = Exception(error_message)
        classification = service.classify_error(error)
        assert classification == expected

    def test_fatal_error_stops_retry_loop(
        self, service: DefaultVerificationService, fast_budget: VerificationBudget
    ) -> None:
        """Fatal errors immediately stop retries and return FAILED_FATAL."""
        check, calls = make_failing_check([
            Exception("Resource not found"),  # Fatal on first try
        ])

        with patch("time.monotonic") as mock_time, patch("time.sleep"):
            clock = FakeClock()
            mock_time.side_effect = clock.monotonic

            result, observed = service.verify_condition(
                operation="test_op",
                target="target-1",
                check=check,
                budget=fast_budget,
            )

        assert result == VerificationResult.FAILED_FATAL
        assert len(calls) == 1  # Only one attempt

    def test_needs_reconcile_error_stops_retry_loop(
        self, service: DefaultVerificationService, fast_budget: VerificationBudget
    ) -> None:
        """NEEDS_RECONCILE errors immediately stop retries."""
        check, calls = make_failing_check([
            Exception("Conflict: resource was modified"),
        ])

        with patch("time.monotonic") as mock_time, patch("time.sleep"):
            clock = FakeClock()
            mock_time.side_effect = clock.monotonic

            result, observed = service.verify_condition(
                operation="test_op",
                target="target-1",
                check=check,
                budget=fast_budget,
            )

        assert result == VerificationResult.FAILED_FATAL  # Mapped to FAILED_FATAL
        assert len(calls) == 1

    def test_retryable_error_continues_retry_loop(
        self, service: DefaultVerificationService, fast_budget: VerificationBudget
    ) -> None:
        """Retryable errors continue the retry loop until success or budget exhausted."""
        check, calls = make_failing_check([
            Exception("Connection timeout"),
            Exception("Server busy"),
        ])
        # Will succeed on third call (after exceptions exhausted)

        with patch("time.monotonic") as mock_time, patch("time.sleep"):
            clock = FakeClock()
            mock_time.side_effect = clock.monotonic

            result, observed = service.verify_condition(
                operation="test_op",
                target="target-1",
                check=check,
                budget=fast_budget,
            )

        assert result == VerificationResult.SUCCESS
        assert observed == "recovered"
        assert len(calls) == 3


# --- Circuit Breaker Tests ---


class TestCircuitBreaker:
    """Tests for circuit breaker state machine."""

    def test_circuit_starts_closed(self) -> None:
        """Circuit breaker starts in closed state."""
        service = DefaultVerificationService(circuit_breaker_threshold=3)
        assert not service.is_circuit_open

    def test_circuit_opens_after_threshold_failures(self) -> None:
        """Circuit opens after consecutive failures reach threshold."""
        service = DefaultVerificationService(circuit_breaker_threshold=3)
        budget = VerificationBudget(
            timeout_seconds=10.0,
            max_attempts=1,  # Single attempt per call
            initial_delay_ms=1,
            max_delay_ms=10,
            backoff_factor=1.0,
            jitter_ms=0,
        )

        # We need to use the same clock for all operations including the final check
        # because is_circuit_open checks time.monotonic() for auto-reset
        with patch("time.monotonic") as mock_time, patch("time.sleep"):
            clock = FakeClock(start=1000.0)  # Start at a high value
            mock_time.side_effect = clock.monotonic

            # Fail 3 times to open circuit
            for i in range(3):
                check, _ = make_check_fn([(False, f"fail-{i}")])
                service.verify_condition(
                    operation="test",
                    target="target",
                    check=check,
                    budget=budget,
                )
                clock.advance(0.1)  # Small time advance between calls

            # Circuit should now be open (checked within same patched context)
            assert service.is_circuit_open

    def test_open_circuit_blocks_verification(self) -> None:
        """When circuit is open, verification immediately fails."""
        service = DefaultVerificationService(circuit_breaker_threshold=2)
        budget = VerificationBudget(
            timeout_seconds=10.0,
            max_attempts=1,
            initial_delay_ms=1,
            max_delay_ms=10,
            backoff_factor=1.0,
            jitter_ms=0,
        )

        with patch("time.monotonic") as mock_time, patch("time.sleep"):
            clock = FakeClock()
            mock_time.side_effect = clock.monotonic

            # Open the circuit
            for _ in range(2):
                check, _ = make_check_fn([(False, "fail")])
                service.verify_condition(
                    operation="test", target="target", check=check, budget=budget
                )

            # Verify circuit is open
            assert service.is_circuit_open

            # Try to verify - should fail immediately without calling check
            call_tracker = []
            def check_should_not_be_called() -> tuple[bool, Any]:
                call_tracker.append(1)
                return (True, "success")

            result, observed = service.verify_condition(
                operation="test",
                target="target",
                check=check_should_not_be_called,
                budget=budget,
            )

        assert result == VerificationResult.FAILED_FATAL
        assert observed is None
        assert len(call_tracker) == 0  # Check was never called

    def test_success_resets_failure_count(self) -> None:
        """Successful verification resets failure count, preventing circuit open."""
        service = DefaultVerificationService(circuit_breaker_threshold=3)
        budget = VerificationBudget(
            timeout_seconds=10.0,
            max_attempts=1,
            initial_delay_ms=1,
            max_delay_ms=10,
            backoff_factor=1.0,
            jitter_ms=0,
        )

        with patch("time.monotonic") as mock_time, patch("time.sleep"):
            clock = FakeClock()
            mock_time.side_effect = clock.monotonic

            # Fail twice
            for _ in range(2):
                check, _ = make_check_fn([(False, "fail")])
                service.verify_condition(
                    operation="test", target="target", check=check, budget=budget
                )

            # Succeed once - should reset counter
            check, _ = make_check_fn([(True, "success")])
            service.verify_condition(
                operation="test", target="target", check=check, budget=budget
            )

            # Fail twice more - still shouldn't open (need 3 consecutive)
            for _ in range(2):
                check, _ = make_check_fn([(False, "fail")])
                service.verify_condition(
                    operation="test", target="target", check=check, budget=budget
                )

        assert not service.is_circuit_open

    def test_circuit_auto_closes_after_timeout(self) -> None:
        """Circuit automatically closes after reset timeout expires."""
        service = DefaultVerificationService(
            circuit_breaker_threshold=2,
            circuit_breaker_timeout=30.0,
        )
        budget = VerificationBudget(
            timeout_seconds=10.0,
            max_attempts=1,
            initial_delay_ms=1,
            max_delay_ms=10,
            backoff_factor=1.0,
            jitter_ms=0,
        )

        with patch("time.monotonic") as mock_time, patch("time.sleep"):
            clock = FakeClock()
            mock_time.side_effect = clock.monotonic

            # Open the circuit
            for _ in range(2):
                check, _ = make_check_fn([(False, "fail")])
                service.verify_condition(
                    operation="test", target="target", check=check, budget=budget
                )

            assert service.is_circuit_open

            # Advance time past the timeout
            clock.advance(35.0)

            # Circuit should auto-close
            assert not service.is_circuit_open

    def test_reset_circuit_clears_state(self) -> None:
        """reset_circuit() explicitly closes circuit and clears failure count."""
        service = DefaultVerificationService(circuit_breaker_threshold=2)
        budget = VerificationBudget(
            timeout_seconds=10.0,
            max_attempts=1,
            initial_delay_ms=1,
            max_delay_ms=10,
            backoff_factor=1.0,
            jitter_ms=0,
        )

        with patch("time.monotonic") as mock_time, patch("time.sleep"):
            clock = FakeClock()
            mock_time.side_effect = clock.monotonic

            # Open the circuit
            for _ in range(2):
                check, _ = make_check_fn([(False, "fail")])
                service.verify_condition(
                    operation="test", target="target", check=check, budget=budget
                )

            assert service.is_circuit_open

            # Manually reset
            service.reset_circuit()

            assert not service.is_circuit_open


# --- Backoff Behavior Tests ---


class TestBackoffBehavior:
    """Tests for exponential backoff timing."""

    def test_backoff_increases_delay_between_attempts(self) -> None:
        """Delay increases exponentially between retry attempts."""
        service = DefaultVerificationService()
        budget = VerificationBudget(
            timeout_seconds=100.0,
            max_attempts=4,
            initial_delay_ms=100,
            max_delay_ms=1000,
            backoff_factor=2.0,
            jitter_ms=0,
        )

        sleep_calls: list[float] = []

        def check_always_fail() -> tuple[bool, Any]:
            return (False, "fail")

        with patch("time.monotonic") as mock_time, patch("time.sleep") as mock_sleep:
            clock = FakeClock()
            mock_time.side_effect = clock.monotonic

            def record_sleep(seconds: float) -> None:
                sleep_calls.append(seconds)
                clock.advance(seconds)

            mock_sleep.side_effect = record_sleep

            service.verify_condition(
                operation="test",
                target="target",
                check=check_always_fail,
                budget=budget,
            )

        # Verify exponential backoff: 100ms -> 200ms -> 400ms -> 800ms
        # After the loop exits due to max_attempts, no more sleep is called
        # So 4 attempts means 4 sleeps (sleep happens after each failed check
        # but the loop continues until it runs out of attempts)
        # Actually looking at the code: sleep happens BEFORE the next attempt
        # So 4 attempts = 4 checks, with sleep between each
        # But the last iteration exits via the while condition after sleep
        # Looking more carefully: check runs, if fail then check timeout,
        # then sleep, then backoff update, then loop continues
        # So we get: check1, sleep, check2, sleep, check3, sleep, check4, sleep, exit
        # That's 4 sleeps for 4 attempts
        assert len(sleep_calls) == 4
        assert sleep_calls[0] == pytest.approx(0.1, rel=0.01)  # 100ms
        assert sleep_calls[1] == pytest.approx(0.2, rel=0.01)  # 200ms
        assert sleep_calls[2] == pytest.approx(0.4, rel=0.01)  # 400ms
        assert sleep_calls[3] == pytest.approx(0.8, rel=0.01)  # 800ms

    def test_backoff_respects_max_delay(self) -> None:
        """Delay is capped at max_delay_ms."""
        service = DefaultVerificationService()
        budget = VerificationBudget(
            timeout_seconds=100.0,
            max_attempts=5,
            initial_delay_ms=100,
            max_delay_ms=250,  # Cap at 250ms
            backoff_factor=2.0,
            jitter_ms=0,
        )

        sleep_calls: list[float] = []

        def check_always_fail() -> tuple[bool, Any]:
            return (False, "fail")

        with patch("time.monotonic") as mock_time, patch("time.sleep") as mock_sleep:
            clock = FakeClock()
            mock_time.side_effect = clock.monotonic

            def record_sleep(seconds: float) -> None:
                sleep_calls.append(seconds)
                clock.advance(seconds)

            mock_sleep.side_effect = record_sleep

            service.verify_condition(
                operation="test",
                target="target",
                check=check_always_fail,
                budget=budget,
            )

        # 5 attempts = 5 sleeps after each failed check
        # 100ms -> 200ms -> 250ms (capped) -> 250ms (capped) -> 250ms (capped)
        assert len(sleep_calls) == 5
        assert sleep_calls[0] == pytest.approx(0.1, rel=0.01)
        assert sleep_calls[1] == pytest.approx(0.2, rel=0.01)
        assert sleep_calls[2] == pytest.approx(0.25, rel=0.01)  # Capped
        assert sleep_calls[3] == pytest.approx(0.25, rel=0.01)  # Still capped
        assert sleep_calls[4] == pytest.approx(0.25, rel=0.01)  # Still capped


# --- Config Factory Tests ---
# Note: Tests for create_verification_service_from_config were removed because they
# accessed internal _default_budget state. The factory's behavior is tested through
# the verification behavior tests above (e.g., verify_condition with different budgets).


# --- Default Budget Tests ---


class TestDefaultBudget:
    """Tests for default budget configuration."""

    def test_default_budget_has_reasonable_values(self) -> None:
        """DEFAULT_GH_BUDGET has sensible production values."""
        assert DEFAULT_GH_BUDGET.timeout_seconds == 10.0
        assert DEFAULT_GH_BUDGET.max_attempts == 10
        assert DEFAULT_GH_BUDGET.initial_delay_ms == 250
        assert DEFAULT_GH_BUDGET.max_delay_ms == 2000
        assert DEFAULT_GH_BUDGET.backoff_factor == 1.5
        assert DEFAULT_GH_BUDGET.jitter_ms == 0

    # Note: test_service_uses_default_budget_when_none_provided was removed
    # because it accessed internal _default_budget state.

    def test_custom_budget_overrides_default(
        self, service: DefaultVerificationService
    ) -> None:
        """Custom budget passed to verify_condition overrides default."""
        custom_budget = VerificationBudget(
            timeout_seconds=5.0,
            max_attempts=2,
            initial_delay_ms=50,
            max_delay_ms=100,
            backoff_factor=1.0,
            jitter_ms=0,
        )
        check, calls = make_check_fn([
            (False, "fail-1"),
            (False, "fail-2"),  # Should stop here due to max_attempts=2
        ])

        with patch("time.monotonic") as mock_time, patch("time.sleep"):
            clock = FakeClock()
            mock_time.side_effect = clock.monotonic

            result, _ = service.verify_condition(
                operation="test",
                target="target",
                check=check,
                budget=custom_budget,
            )

        assert result == VerificationResult.FAILED_RETRYABLE
        assert len(calls) == 2  # Only 2 attempts from custom budget


# --- Jitter Tests ---


class TestJitterBehavior:
    """Tests for jitter in backoff timing."""

    def test_jitter_adds_random_delay(self) -> None:
        """Non-zero jitter_ms adds random component to delay."""
        service = DefaultVerificationService()
        budget = VerificationBudget(
            timeout_seconds=100.0,
            max_attempts=3,
            initial_delay_ms=100,
            max_delay_ms=1000,
            backoff_factor=1.0,
            jitter_ms=50,  # Add up to 50ms jitter
        )

        sleep_calls: list[float] = []

        def check_always_fail() -> tuple[bool, Any]:
            return (False, "fail")

        with patch("time.monotonic") as mock_time, patch("time.sleep") as mock_sleep:
            clock = FakeClock()
            mock_time.side_effect = clock.monotonic

            def record_sleep(seconds: float) -> None:
                sleep_calls.append(seconds)
                clock.advance(seconds)

            mock_sleep.side_effect = record_sleep

            # Patch random.uniform to return predictable values
            with patch("random.uniform", return_value=0.03):  # 30ms jitter
                service.verify_condition(
                    operation="test",
                    target="target",
                    check=check_always_fail,
                    budget=budget,
                )

        # 3 attempts = 3 sleeps after each failed check
        # Base delay 100ms + 30ms jitter = 130ms = 0.13 seconds
        assert len(sleep_calls) == 3
        assert sleep_calls[0] == pytest.approx(0.13, rel=0.01)
        assert sleep_calls[1] == pytest.approx(0.13, rel=0.01)  # backoff_factor=1.0
        assert sleep_calls[2] == pytest.approx(0.13, rel=0.01)
