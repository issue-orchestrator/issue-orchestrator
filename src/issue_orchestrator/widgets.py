"""Reusable Textual widgets for the dashboard."""

from textual.widgets import Static
from textual.reactive import reactive
from rich.text import Text


class StatusIndicator(Static):
    """Display a status with a visual indicator icon.

    This widget shows a status message with an icon that changes based on the status type.
    Useful for displaying task states, health checks, or any categorical status.

    Args:
        status: The status type (pending, success, failed, running)
        message: Optional custom message to display with the status

    Example:
        ```python
        indicator = StatusIndicator(status="success", message="Tests passed")
        ```
    """

    status: reactive[str] = reactive("pending")
    message: reactive[str] = reactive("")

    # Icon and color mappings for each status type
    STATUS_STYLES = {
        "pending": {"icon": "⏳", "color": "yellow"},
        "success": {"icon": "✓", "color": "green"},
        "failed": {"icon": "✗", "color": "red"},
        "running": {"icon": "▶", "color": "blue"},
        "paused": {"icon": "⏸", "color": "yellow"},
        "stopped": {"icon": "⏹", "color": "dim"},
    }

    def __init__(
        self,
        status: str = "pending",
        message: str = "",
        **kwargs
    ) -> None:
        """Initialize the status indicator.

        Args:
            status: Initial status type
            message: Initial message to display
            **kwargs: Additional widget arguments
        """
        super().__init__(**kwargs)
        self.status = status
        self.message = message

    def render(self) -> Text:
        """Render the status indicator with icon and message.

        Returns:
            Formatted Text object with icon, status, and optional message
        """
        style_info = self.STATUS_STYLES.get(
            self.status,
            {"icon": "?", "color": "white"}
        )

        icon = style_info["icon"]
        color = style_info["color"]

        # Build the display text
        text = Text()
        text.append(f"{icon} ", style=color)
        text.append(self.status.upper(), style=f"bold {color}")

        if self.message:
            text.append(f": {self.message}")

        return text

    def set_status(self, status: str, message: str = "") -> None:
        """Update the status and optional message.

        Args:
            status: New status type
            message: New message to display (defaults to empty string)
        """
        self.status = status
        self.message = message
