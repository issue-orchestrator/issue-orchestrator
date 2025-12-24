"""Lifecycle plugin that logs all trace events.

This plugin implements the on_trace_event hook and logs all events,
providing visibility into orchestrator activity for debugging and e2e tests.
"""

import logging
from typing import Any

from ..hookspec import hookimpl

logger = logging.getLogger(__name__)


class LifecycleLoggingPlugin:
    """Plugin that logs all trace events.

    Implements the single on_trace_event hook and logs events to the
    issue_orchestrator.events logger.
    """

    def __init__(self, level: int = logging.INFO):
        """Initialize the plugin.

        Args:
            level: Logging level for events (default: INFO)
        """
        self._level = level
        self._event_logger = logging.getLogger("issue_orchestrator.events")

    @hookimpl
    def on_trace_event(self, event: str, data: dict[str, Any]) -> None:
        """Log trace event."""
        # Format data for readability
        if data:
            data_str = ", ".join(f"{k}={v}" for k, v in data.items())
            self._event_logger.log(self._level, "[EVENT] %s: %s", event, data_str)
        else:
            self._event_logger.log(self._level, "[EVENT] %s", event)
