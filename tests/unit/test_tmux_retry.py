"""Unit tests for tmux retry logic using tenacity."""

from unittest.mock import MagicMock, call, patch

import pytest
from libtmux.exc import (
    BadSessionName,
    LibTmuxException,
    OptionError,
    TmuxCommandNotFound,
    VersionTooLow,
)
from tenacity import RetryError

from issue_orchestrator.adapters.terminal._tmux_retry import (
    FATAL_EXCEPTIONS,
    tmux_retry,
)


class TestTmuxRetryDecorator:
    """Tests for the tmux_retry decorator."""

    def test_success_on_first_try(self):
        """Test that successful calls don't retry."""
        call_count = 0

        @tmux_retry
        def successful_func():
            nonlocal call_count
            call_count += 1
            return "success"

        result = successful_func()
        assert result == "success"
        assert call_count == 1

    def test_retries_transient_errors(self):
        """Test that transient errors (LibTmuxException) are retried."""
        call_count = 0

        @tmux_retry
        def transient_failure():
            nonlocal call_count
            call_count += 1
            if call_count < 3:
                raise LibTmuxException("transient error")
            return "success"

        result = transient_failure()
        assert result == "success"
        assert call_count == 3  # Failed twice, succeeded on third

    def test_retries_generic_exceptions(self):
        """Test that generic exceptions are retried."""
        call_count = 0

        @tmux_retry
        def generic_failure():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise RuntimeError("generic error")
            return "success"

        result = generic_failure()
        assert result == "success"
        assert call_count == 2  # Failed once, succeeded on second

    def test_exhausts_retries_and_reraizes(self):
        """Test that after max attempts, the exception is re-raised."""
        call_count = 0

        @tmux_retry
        def always_fails():
            nonlocal call_count
            call_count += 1
            raise LibTmuxException("persistent failure")

        with pytest.raises(LibTmuxException, match="persistent failure"):
            always_fails()

        # Should have tried 3 times (stop_after_attempt(3))
        assert call_count == 3

    def test_no_retry_for_tmux_command_not_found(self):
        """Test that TmuxCommandNotFound is not retried (fatal error)."""
        call_count = 0

        @tmux_retry
        def tmux_not_installed():
            nonlocal call_count
            call_count += 1
            raise TmuxCommandNotFound()

        with pytest.raises(TmuxCommandNotFound):
            tmux_not_installed()

        # Should only be called once (no retries for fatal errors)
        assert call_count == 1

    def test_no_retry_for_bad_session_name(self):
        """Test that BadSessionName is not retried (fatal error)."""
        call_count = 0

        @tmux_retry
        def bad_session():
            nonlocal call_count
            call_count += 1
            raise BadSessionName("invalid:name")

        with pytest.raises(BadSessionName):
            bad_session()

        assert call_count == 1

    def test_no_retry_for_option_error(self):
        """Test that OptionError is not retried (fatal error)."""
        call_count = 0

        @tmux_retry
        def option_error():
            nonlocal call_count
            call_count += 1
            raise OptionError("invalid option")

        with pytest.raises(OptionError):
            option_error()

        assert call_count == 1

    def test_no_retry_for_version_too_low(self):
        """Test that VersionTooLow is not retried (fatal error)."""
        call_count = 0

        @tmux_retry
        def old_tmux():
            nonlocal call_count
            call_count += 1
            raise VersionTooLow("tmux 1.0 < 2.4")

        with pytest.raises(VersionTooLow):
            old_tmux()

        assert call_count == 1

    def test_fatal_exceptions_list(self):
        """Test that FATAL_EXCEPTIONS contains expected exceptions."""
        assert TmuxCommandNotFound in FATAL_EXCEPTIONS
        assert BadSessionName in FATAL_EXCEPTIONS
        assert OptionError in FATAL_EXCEPTIONS
        assert VersionTooLow in FATAL_EXCEPTIONS
        # LibTmuxException should NOT be in fatal (it's transient)
        assert LibTmuxException not in FATAL_EXCEPTIONS


class TestRetryWithTmuxManager:
    """Integration-style tests for retry decorator with TmuxManager methods."""

    def test_health_check_no_retry_reports_actual_state(self):
        """Test that health_check reports state without retrying.

        health_check should NOT have @tmux_retry because it handles exceptions
        internally and returns a health status. Retrying would mask failures.
        """
        from issue_orchestrator.adapters.terminal._tmux import TmuxManager

        manager = TmuxManager()
        mock_server = MagicMock()
        manager._server = mock_server

        # Configure server.sessions to raise (simulating unhealthy server)
        type(mock_server).sessions = property(
            lambda self: (_ for _ in ()).throw(LibTmuxException("server down"))
        )

        # health_check should return unhealthy status, not retry
        health = manager.health_check()
        assert health.server_running is False
        assert "server down" in health.error

    def test_create_session_no_retry_on_bad_name(self):
        """Test that create_orchestrator_session doesn't retry on BadSessionName."""
        from issue_orchestrator.adapters.terminal._tmux import TmuxManager

        manager = TmuxManager(session_name="invalid:name")
        mock_server = MagicMock()
        manager._server = mock_server

        # Server's sessions.filter raises BadSessionName (invalid character in name)
        mock_server.sessions.filter.side_effect = BadSessionName("invalid:name")
        mock_server.new_session.side_effect = BadSessionName("invalid:name")

        with pytest.raises(BadSessionName):
            manager.create_orchestrator_session()

        # Should only try once
        assert mock_server.sessions.filter.call_count == 1

    def test_ensure_server_running_retries(self):
        """Test that ensure_server_running retries transient failures."""
        from issue_orchestrator.adapters.terminal._tmux import TmuxManager

        call_count = 0

        def is_alive_effect():
            nonlocal call_count
            call_count += 1
            if call_count < 2:
                raise LibTmuxException("socket error")
            return True

        with patch("issue_orchestrator.adapters.terminal._tmux.libtmux.Server") as mock_server_cls:
            mock_server = MagicMock()
            mock_server.is_alive.side_effect = is_alive_effect
            mock_server_cls.return_value = mock_server

            manager = TmuxManager()
            manager._server = mock_server

            result = manager.ensure_server_running()

            assert result is True
            assert call_count == 2  # Retried once


class TestRetryLogging:
    """Tests for retry logging behavior."""

    def test_logs_warning_on_retry(self, caplog):
        """Test that retries are logged at WARNING level."""
        import logging

        @tmux_retry
        def fails_then_succeeds():
            if not hasattr(fails_then_succeeds, "_called"):
                fails_then_succeeds._called = True
                raise LibTmuxException("temporary error")
            return "success"

        with caplog.at_level(logging.WARNING):
            result = fails_then_succeeds()

        assert result == "success"
        # Check that a warning was logged (tenacity logs retries)
        assert any("Retrying" in record.message for record in caplog.records)
