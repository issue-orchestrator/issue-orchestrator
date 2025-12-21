"""Lifecycle plugin that broadcasts trace events via SSE.

This plugin implements the on_trace_event hook and forwards all events
to SSE subscribers (web dashboard clients).

This mirrors LifecycleIPCPlugin - both receive the same events via pluggy,
but deliver them through different channels.
"""

import asyncio
import logging

from ..hookspec import hookimpl

logger = logging.getLogger(__name__)


class LifecycleSSEPlugin:
    """Plugin that broadcasts trace events via SSE.

    Implements the single on_trace_event hook and forwards all events
    to web dashboard clients via Server-Sent Events.
    """

    def _broadcast(self, event: str, data: dict) -> None:
        """Broadcast an event to SSE clients.

        Args:
            event: Event name (e.g., "session.started")
            data: Event data dictionary
        """
        try:
            from ..web import broadcast_event, _event_subscribers

            if not _event_subscribers:
                logger.debug("[SSE] No subscribers, skipping event: %s", event)
                return

            try:
                loop = asyncio.get_running_loop()
                loop.create_task(broadcast_event(event, data))
                logger.debug("[SSE] Scheduled broadcast of %s", event)
            except RuntimeError:
                logger.debug("[SSE] No event loop, skipping event: %s", event)

        except ImportError:
            logger.debug("[SSE] Web module not available, skipping event: %s", event)
        except Exception as e:
            logger.warning("[SSE] Failed to broadcast event %s: %s", event, e)

    @hookimpl
    def on_trace_event(self, event: str, data: dict) -> None:
        """Forward trace event to SSE clients."""
        self._broadcast(event, data)
