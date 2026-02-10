"""Unit tests for process_state domain models."""

from datetime import datetime

import pytest

from issue_orchestrator.domain import ProcessState, ProcessExitInfo


class TestProcessState:
    """Tests for ProcessState enum."""

    def test_values(self):
        """Test ProcessState enum values."""
        assert ProcessState.RUNNING.value == "running"
        assert ProcessState.EXITED.value == "exited"
        assert ProcessState.SIGNALED.value == "signaled"
        assert ProcessState.UNKNOWN.value == "unknown"

    def test_all_states(self):
        """Test all ProcessState values exist."""
        states = list(ProcessState)
        assert len(states) == 4
        assert ProcessState.RUNNING in states
        assert ProcessState.EXITED in states
        assert ProcessState.SIGNALED in states
        assert ProcessState.UNKNOWN in states


class TestProcessExitInfo:
    """Tests for ProcessExitInfo dataclass."""

    def test_default_values(self):
        """Test ProcessExitInfo with default values."""
        info = ProcessExitInfo()
        assert info.exit_code is None
        assert info.signal is None
        assert info.exit_time is None

    def test_with_exit_code(self):
        """Test ProcessExitInfo with exit code."""
        info = ProcessExitInfo(exit_code=0)
        assert info.exit_code == 0
        assert info.signal is None

    def test_with_signal(self):
        """Test ProcessExitInfo with signal."""
        info = ProcessExitInfo(signal="SIGKILL")
        assert info.signal == "SIGKILL"
        assert info.exit_code is None

    def test_with_exit_time(self):
        """Test ProcessExitInfo with exit time."""
        now = datetime.now()
        info = ProcessExitInfo(exit_time=now)
        assert info.exit_time == now

    def test_all_fields(self):
        """Test ProcessExitInfo with all fields."""
        now = datetime.now()
        info = ProcessExitInfo(exit_code=137, signal="SIGKILL", exit_time=now)
        assert info.exit_code == 137
        assert info.signal == "SIGKILL"
        assert info.exit_time == now

    def test_success_true(self):
        """Test success property returns True for exit code 0."""
        info = ProcessExitInfo(exit_code=0)
        assert info.success is True

    def test_success_false_nonzero(self):
        """Test success property returns False for non-zero exit code."""
        info = ProcessExitInfo(exit_code=1)
        assert info.success is False

        info = ProcessExitInfo(exit_code=137)
        assert info.success is False

    def test_success_false_when_none(self):
        """Test success property returns False when exit_code is None."""
        info = ProcessExitInfo()
        assert info.success is False

    def test_was_signaled_true(self):
        """Test was_signaled returns True when signal is present."""
        info = ProcessExitInfo(signal="SIGTERM")
        assert info.was_signaled is True

    def test_was_signaled_false(self):
        """Test was_signaled returns False when no signal."""
        info = ProcessExitInfo(exit_code=0)
        assert info.was_signaled is False

        info = ProcessExitInfo()
        assert info.was_signaled is False

    def test_str_with_exit_code(self):
        """Test __str__ with exit code."""
        info = ProcessExitInfo(exit_code=1)
        assert str(info) == "exit code 1"

    def test_str_with_signal(self):
        """Test __str__ with signal."""
        info = ProcessExitInfo(signal="SIGKILL")
        assert str(info) == "killed by SIGKILL"

    def test_str_with_both(self):
        """Test __str__ with both signal and exit code."""
        info = ProcessExitInfo(exit_code=137, signal="SIGKILL")
        # Signal takes precedence
        assert str(info) == "killed by SIGKILL"

    def test_str_unknown(self):
        """Test __str__ with no info."""
        info = ProcessExitInfo()
        assert str(info) == "unknown exit"

    def test_frozen(self):
        """Test ProcessExitInfo is frozen (immutable)."""
        info = ProcessExitInfo(exit_code=0)
        with pytest.raises(AttributeError):
            info.exit_code = 1  # type: ignore
