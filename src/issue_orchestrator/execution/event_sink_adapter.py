"""Pluggy-backed EventSink adapter.

This adapter implements the EventSink port by forwarding events to pluggy hooks.
It's the bridge between the core's abstract event emission and the concrete
pluggy-based plugin system.

This is the ONLY place pluggy is used for event emission.
"""

import asyncio
import logging
from typing import TYPE_CHECKING

from ..ports.event_sink import EventSink, TraceEvent

if TYPE_CHECKING:
    import pluggy

logger = logging.getLogger(__name__)


class PluggyEventSink:
    """EventSink implementation that forwards to pluggy hooks.

    This adapter:
    - Implements the EventSink protocol
    - Wraps a pluggy PluginManager
    - Forwards TraceEvents to on_trace_event hooks
    - Swallows all exceptions (fire-and-forget semantics)
    """

    def __init__(self, plugin_manager: "pluggy.PluginManager"):
        """Initialize with a configured pluggy PluginManager.

        Args:
            plugin_manager: A pluggy PluginManager with hooks registered.
                           This should already have on_trace_event hookspec.
        """
        self._pm = plugin_manager

    def publish(self, event: TraceEvent) -> None:
        """Emit a trace event to all registered plugins.

        This method:
        - Never raises exceptions
        - Never blocks the caller
        - Logs warnings on failure but doesn't propagate them

        Args:
            event: The trace event to publish
        """
        try:
            self._pm.hook.on_trace_event(event=event.name, data=event.data)
        except Exception as e:
            # Fire-and-forget: log but don't raise
            logger.warning("Failed to publish event %s: %s", event.name, e)


class CompositeEventSink:
    """EventSink that fans out to multiple sinks.

    Useful when you want events to go to multiple destinations
    (e.g., logging + metrics + UI) without each knowing about the others.
    """

    def __init__(self, *sinks: EventSink):
        """Initialize with multiple sinks.

        Args:
            sinks: EventSink instances to fan out to
        """
        self._sinks = list(sinks)

    def add_sink(self, sink: EventSink) -> None:
        """Add another sink at runtime."""
        self._sinks.append(sink)

    def publish(self, event: TraceEvent) -> None:
        """Publish to all sinks. Failures in one don't affect others."""
        for sink in self._sinks:
            try:
                sink.publish(event)
            except Exception as e:
                logger.warning("Sink %s failed for event %s: %s",
                             type(sink).__name__, event.name, e)


class LoggingEventSink:
    """EventSink that logs all events.

    Useful for debugging and audit trails.
    """

    def __init__(self, logger_name: str = "issue_orchestrator.events"):
        self._logger = logging.getLogger(logger_name)

    def publish(self, event: TraceEvent) -> None:
        """Log the event."""
        self._logger.info("[EVENT] %s: %s", event.name, event.data)
