"""Tests for Dashboard module."""

from unittest.mock import Mock, AsyncMock, patch
import pytest

from issue_orchestrator.dashboard import DashboardApp


class TestDashboardApp:
    """Tests for the DashboardApp class."""

    @pytest.mark.asyncio
    async def test_action_show_version_displays_version(self):
        """Verify show version action displays version information."""
        mock_orchestrator = Mock()
        mock_orchestrator.config = Mock()
        mock_orchestrator.state = Mock()
        mock_orchestrator.state.active_sessions = []

        app = DashboardApp(mock_orchestrator)

        # Mock the notify method to capture the message
        app.notify = Mock()

        # Mock importlib.metadata.version within the action method
        with patch('importlib.metadata.version', return_value="0.1.0"):
            await app.action_show_version()

        # Verify notify was called with version info
        app.notify.assert_called_once()
        call_args = app.notify.call_args[0][0]
        assert "issue-orchestrator" in call_args
        assert "v" in call_args
        assert "0.1.0" in call_args

    @pytest.mark.asyncio
    async def test_action_show_version_handles_package_not_found(self):
        """Verify show version gracefully handles when package is not installed."""
        from importlib.metadata import PackageNotFoundError

        mock_orchestrator = Mock()
        mock_orchestrator.config = Mock()
        mock_orchestrator.state = Mock()
        mock_orchestrator.state.active_sessions = []

        app = DashboardApp(mock_orchestrator)
        app.notify = Mock()

        # Mock version() to raise PackageNotFoundError
        with patch('importlib.metadata.version', side_effect=PackageNotFoundError("not found")):
            await app.action_show_version()

        # Should still show dev version
        app.notify.assert_called_once()
        call_args = app.notify.call_args[0][0]
        assert "0.1.0 (dev)" in call_args
