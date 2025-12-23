"""Unit tests for e2e test helper functions.

These test the wait_with_process_check utility without requiring
actual GitHub or orchestrator infrastructure.
"""

import pytest
from unittest.mock import Mock, MagicMock


# Import the helper - it's in e2e/conftest.py
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent.parent / "e2e"))
from conftest import wait_with_process_check


class TestWaitWithProcessCheck:
    """Tests for wait_with_process_check helper."""

    def test_returns_result_when_condition_met_immediately(self):
        """Should return immediately when condition is met."""
        condition = Mock(return_value="success")

        result = wait_with_process_check(
            condition,
            timeout=10,
            interval=1,
            description="test",
        )

        assert result == "success"
        assert condition.call_count == 1

    def test_retries_until_condition_met(self):
        """Should retry until condition returns truthy value."""
        # Fail twice, then succeed
        condition = Mock(side_effect=[None, None, "success"])

        result = wait_with_process_check(
            condition,
            timeout=10,
            interval=0.01,  # Fast polling for test
            description="test",
        )

        assert result == "success"
        assert condition.call_count == 3

    def test_returns_none_on_timeout(self):
        """Should return None when timeout expires."""
        condition = Mock(return_value=None)

        result = wait_with_process_check(
            condition,
            timeout=0.05,
            interval=0.01,
            description="test",
        )

        assert result is None
        assert condition.call_count >= 1

    def test_raises_when_orchestrator_crashes(self):
        """Should raise RuntimeError when orchestrator process crashes."""
        condition = Mock(return_value=None)

        # Mock orchestrator that appears crashed
        orchestrator = MagicMock()
        orchestrator.is_running.return_value = False
        orchestrator.stop.return_value = ("stdout output", "stderr output")

        with pytest.raises(RuntimeError) as exc_info:
            wait_with_process_check(
                condition,
                timeout=10,
                orchestrator=orchestrator,
                interval=0.01,
                description="test condition",
            )

        assert "crashed" in str(exc_info.value)
        assert "test condition" in str(exc_info.value)
        assert "stdout output" in str(exc_info.value)

    def test_succeeds_with_healthy_orchestrator(self):
        """Should succeed normally when orchestrator stays healthy."""
        condition = Mock(side_effect=[None, "success"])

        orchestrator = MagicMock()
        orchestrator.is_running.return_value = True

        result = wait_with_process_check(
            condition,
            timeout=10,
            orchestrator=orchestrator,
            interval=0.01,
            description="test",
        )

        assert result == "success"
        # Should have checked process health
        assert orchestrator.is_running.call_count >= 1

    def test_no_orchestrator_check_when_none(self):
        """Should work fine without orchestrator parameter."""
        condition = Mock(return_value="success")

        result = wait_with_process_check(
            condition,
            timeout=10,
            orchestrator=None,
            interval=0.01,
            description="test",
        )

        assert result == "success"
