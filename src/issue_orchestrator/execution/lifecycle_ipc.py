"""Lifecycle plugin that broadcasts trace events via IPC.

This plugin implements the on_trace_event hook and forwards all events
to the IPC EventServer, enabling external UI processes to receive
real-time notifications.

This is the bridge between the pluggy trace event system and the IPC layer.
"""

import asyncio
import logging
from typing import Any

from ..hookspec import hookimpl
from ..ipc import EventServer

logger = logging.getLogger(__name__)


class LifecycleIPCPlugin:
    """Plugin that broadcasts trace events via IPC.

    Implements the single on_trace_event hook and forwards all events
    to connected IPC clients (UI processes, monitoring tools, etc.).
    """

    def __init__(self, event_server: EventServer):
        """Initialize the plugin.

        Args:
            event_server: The EventServer to broadcast events to
        """
        self.event_server = event_server

    def _broadcast(self, event: str, data: dict[str, Any]) -> None:
        """Broadcast an event to IPC clients.

        Args:
            event: Event name (e.g., "session.started")
            data: Event data dictionary
        """
        # Package as IPC message with type field for client filtering
        message = {"type": event, **data}

        try:
            loop = asyncio.get_running_loop()
            loop.create_task(self.event_server.broadcast(message))
        except RuntimeError:
            # No running loop
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(self.event_server.broadcast(message))
                else:
                    loop.run_until_complete(self.event_server.broadcast(message))
            except Exception as e:
                logger.warning(f"Failed to broadcast event {event}: {e}")

    @hookimpl
    def on_trace_event(self, event: str, data: dict) -> None:
        """Forward trace event to IPC clients."""
        self._broadcast(event, data)
