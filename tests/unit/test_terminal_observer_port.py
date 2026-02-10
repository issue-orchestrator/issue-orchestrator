"""Unit tests for TerminalObserver port interface."""

import pytest

from issue_orchestrator.domain import ProcessState
from issue_orchestrator.ports import NullTerminalObserver


class TestNullTerminalObserver:
    """Tests for NullTerminalObserver no-op implementation."""

    @pytest.fixture
    def observer(self) -> NullTerminalObserver:
        return NullTerminalObserver()

    def test_get_process_state_returns_unknown(self, observer):
        """Test get_process_state returns UNKNOWN for any terminal_id."""
        assert observer.get_process_state("issue-123") == ProcessState.UNKNOWN
        assert observer.get_process_state("review-456") == ProcessState.UNKNOWN
        assert observer.get_process_state("nonexistent") == ProcessState.UNKNOWN

    def test_get_exit_info_returns_none(self, observer):
        """Test get_exit_info returns None for any terminal_id."""
        assert observer.get_exit_info("issue-123") is None
        assert observer.get_exit_info("review-456") is None

    def test_is_process_alive_returns_false(self, observer):
        """Test is_process_alive returns False for any terminal_id."""
        assert observer.is_process_alive("issue-123") is False
        assert observer.is_process_alive("review-456") is False

    def test_capture_full_output_returns_none(self, observer):
        """Test capture_full_output returns None for any terminal_id."""
        assert observer.capture_full_output("issue-123") is None
        assert observer.capture_full_output("review-456") is None

    def test_implements_protocol(self):
        """Test NullTerminalObserver satisfies TerminalObserver protocol."""
        observer = NullTerminalObserver()
        # If this doesn't raise, the protocol is satisfied at runtime
        assert hasattr(observer, "get_process_state")
        assert hasattr(observer, "get_exit_info")
        assert hasattr(observer, "is_process_alive")
        assert hasattr(observer, "capture_full_output")
