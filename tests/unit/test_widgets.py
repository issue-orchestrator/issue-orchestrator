"""Tests for reusable Textual widgets."""

import pytest
from rich.text import Text

from issue_orchestrator.widgets import StatusIndicator


class TestStatusIndicator:
    """Test cases for the StatusIndicator widget."""

    def test_init_with_defaults(self):
        """Test StatusIndicator initializes with default values."""
        indicator = StatusIndicator()
        assert indicator.status == "pending"
        assert indicator.message == ""

    def test_init_with_custom_status(self):
        """Test StatusIndicator initializes with custom status."""
        indicator = StatusIndicator(status="success", message="Task completed")
        assert indicator.status == "success"
        assert indicator.message == "Task completed"

    def test_render_pending_status(self):
        """Test rendering pending status."""
        indicator = StatusIndicator(status="pending")
        result = indicator.render()

        assert isinstance(result, Text)
        assert "⏳" in result.plain
        assert "PENDING" in result.plain

    def test_render_success_status(self):
        """Test rendering success status."""
        indicator = StatusIndicator(status="success")
        result = indicator.render()

        assert isinstance(result, Text)
        assert "✓" in result.plain
        assert "SUCCESS" in result.plain

    def test_render_failed_status(self):
        """Test rendering failed status."""
        indicator = StatusIndicator(status="failed")
        result = indicator.render()

        assert isinstance(result, Text)
        assert "✗" in result.plain
        assert "FAILED" in result.plain

    def test_render_running_status(self):
        """Test rendering running status."""
        indicator = StatusIndicator(status="running")
        result = indicator.render()

        assert isinstance(result, Text)
        assert "▶" in result.plain
        assert "RUNNING" in result.plain

    def test_render_paused_status(self):
        """Test rendering paused status."""
        indicator = StatusIndicator(status="paused")
        result = indicator.render()

        assert isinstance(result, Text)
        assert "⏸" in result.plain
        assert "PAUSED" in result.plain

    def test_render_stopped_status(self):
        """Test rendering stopped status."""
        indicator = StatusIndicator(status="stopped")
        result = indicator.render()

        assert isinstance(result, Text)
        assert "⏹" in result.plain
        assert "STOPPED" in result.plain

    def test_render_with_message(self):
        """Test rendering with a custom message."""
        indicator = StatusIndicator(status="success", message="All tests passed")
        result = indicator.render()

        assert isinstance(result, Text)
        assert "SUCCESS" in result.plain
        assert "All tests passed" in result.plain
        assert ":" in result.plain  # Separator between status and message

    def test_render_unknown_status(self):
        """Test rendering with an unknown status type."""
        indicator = StatusIndicator(status="unknown_status")
        result = indicator.render()

        assert isinstance(result, Text)
        assert "?" in result.plain
        assert "UNKNOWN_STATUS" in result.plain

    def test_set_status(self):
        """Test updating status dynamically."""
        indicator = StatusIndicator(status="pending")
        assert indicator.status == "pending"

        indicator.set_status("success")
        assert indicator.status == "success"
        assert indicator.message == ""

    def test_set_status_with_message(self):
        """Test updating status with a new message."""
        indicator = StatusIndicator(status="pending", message="Starting task")

        indicator.set_status("running", message="Processing items")
        assert indicator.status == "running"
        assert indicator.message == "Processing items"

    def test_set_status_updates_render(self):
        """Test that set_status changes the rendered output."""
        indicator = StatusIndicator(status="pending")
        pending_render = indicator.render()
        assert "PENDING" in pending_render.plain

        indicator.set_status("success", message="Done")
        success_render = indicator.render()
        assert "SUCCESS" in success_render.plain
        assert "Done" in success_render.plain

    def test_all_status_types_have_icons(self):
        """Test that all defined status types render with icons."""
        status_types = ["pending", "success", "failed", "running", "paused", "stopped"]

        for status_type in status_types:
            indicator = StatusIndicator(status=status_type)
            result = indicator.render()

            # All status types should have some icon
            assert len(result.plain) > 0
            assert status_type.upper() in result.plain

    def test_message_persistence(self):
        """Test that message persists when only status changes."""
        indicator = StatusIndicator(status="pending", message="Initial message")

        # Set status without message argument
        indicator.set_status("running")

        # Message should be cleared when not provided
        assert indicator.message == ""

        # Set status with new message
        indicator.set_status("success", message="New message")
        assert indicator.message == "New message"

    def test_reactive_status_updates(self):
        """Test that reactive status attribute updates correctly."""
        indicator = StatusIndicator()

        # Update via reactive attribute
        indicator.status = "running"
        assert indicator.status == "running"

        result = indicator.render()
        assert "RUNNING" in result.plain

    def test_reactive_message_updates(self):
        """Test that reactive message attribute updates correctly."""
        indicator = StatusIndicator(status="success")

        # Update via reactive attribute
        indicator.message = "Task completed successfully"
        assert indicator.message == "Task completed successfully"

        result = indicator.render()
        assert "Task completed successfully" in result.plain
