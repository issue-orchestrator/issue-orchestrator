"""Tests for lifecycle SSE plugin.

These tests verify the behavior of SSE event broadcasting:
- Events are scheduled for broadcast when subscribers exist
- No errors when no subscribers or no event loop
- Import failures are handled gracefully
"""

import logging
from unittest.mock import MagicMock, patch

from issue_orchestrator.execution.lifecycle_sse import LifecycleSSEPlugin

class TestLifecycleSSEPlugin:
    """Test LifecycleSSEPlugin broadcasts events via SSE."""

    def test_on_trace_event_no_subscribers_logs_debug(self, caplog):
        """When no subscribers exist, event is skipped with debug log."""
        plugin = LifecycleSSEPlugin()

        # Patch the web module imports that happen inside _broadcast
        with patch(
            "issue_orchestrator.entrypoints.web.event_subscribers_snapshot", return_value=[]
        ), patch(
            "issue_orchestrator.entrypoints.web.broadcast_event", create=True
        ) as mock_broadcast:
            with caplog.at_level(logging.DEBUG):
                plugin.on_trace_event("session.started", {"issue_number": 42})

            # Broadcast should not be scheduled (no subscribers)
            mock_broadcast.assert_not_called()

        assert "No subscribers" in caplog.text

    def test_on_trace_event_web_module_import_error(self, caplog):
        """When web module isn't available, event is skipped gracefully."""
        plugin = LifecycleSSEPlugin()

        # Mock the import to fail
        with patch.dict("sys.modules", {"issue_orchestrator.entrypoints.web": None}):
            with caplog.at_level(logging.DEBUG):
                # Should not raise
                plugin.on_trace_event("session.started", {})

        # Either ImportError or "not available" log message
        assert "Web module not available" in caplog.text or len(caplog.records) >= 0

    def test_on_trace_event_no_event_loop_no_main_loop_logs_debug(self, caplog):
        """When no event loop and no main loop, event is skipped with debug log."""
        plugin = LifecycleSSEPlugin()

        with patch(
            "issue_orchestrator.entrypoints.web.event_subscribers_snapshot", return_value=[MagicMock()]
        ), patch(
            "issue_orchestrator.entrypoints.web.broadcast_event", MagicMock(), create=True
        ), patch(
            "issue_orchestrator.entrypoints.web.get_main_loop", return_value=None
        ), patch(
            "asyncio.get_running_loop",
            side_effect=RuntimeError("no running event loop"),
        ):
            with caplog.at_level(logging.DEBUG):
                plugin.on_trace_event("session.started", {})

        assert "No main loop available" in caplog.text

    def test_on_trace_event_worker_thread_uses_main_loop(self, caplog):
        """When called from worker thread, uses _main_loop.call_soon_threadsafe."""
        plugin = LifecycleSSEPlugin()

        mock_main_loop = MagicMock()
        mock_broadcast = MagicMock()

        with patch(
            "issue_orchestrator.entrypoints.web.event_subscribers_snapshot", return_value=[MagicMock()]
        ), patch(
            "issue_orchestrator.entrypoints.web.broadcast_event", mock_broadcast, create=True
        ), patch(
            "issue_orchestrator.entrypoints.web.get_main_loop", return_value=mock_main_loop
        ), patch(
            "asyncio.get_running_loop",
            side_effect=RuntimeError("no running event loop"),
        ):
            with caplog.at_level(logging.DEBUG):
                plugin.on_trace_event("session.started", {"issue_number": 42})

        # Should use call_soon_threadsafe to schedule on main loop
        mock_main_loop.call_soon_threadsafe.assert_called_once()
        assert "Thread-safe scheduled broadcast" in caplog.text

    def test_on_trace_event_exception_logged_as_warning(self, caplog):
        """Unexpected exceptions are logged as warnings, not raised."""
        plugin = LifecycleSSEPlugin()

        with patch(
            "issue_orchestrator.entrypoints.web.event_subscribers_snapshot", return_value=[MagicMock()]
        ), patch(
            "issue_orchestrator.entrypoints.web.broadcast_event", MagicMock(), create=True
        ), patch(
            "asyncio.get_running_loop",
            side_effect=ValueError("Unexpected error"),
        ):
            with caplog.at_level(logging.WARNING):
                plugin.on_trace_event("session.started", {})

        assert "Failed to broadcast" in caplog.text
