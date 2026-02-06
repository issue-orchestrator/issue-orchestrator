"""Lifecycle plugin that broadcasts trace events via SSE.

This plugin implements the on_trace_event hook and forwards all events
to SSE subscribers (web dashboard clients).
"""

import asyncio
import logging

from ..infra.hooks.hookspec import hookimpl

logger = logging.getLogger(__name__)


class LifecycleSSEPlugin:
    """Plugin that broadcasts trace events via SSE.

    Implements the single on_trace_event hook and forwards all events
    to web dashboard clients via Server-Sent Events.
    """

    def _broadcast(self, event: str, data: dict) -> None:
        """Broadcast an event to SSE clients.

        This method is thread-safe and can be called from worker threads
        (e.g., tick running via asyncio.to_thread).

        Args:
            event: Event name (e.g., "session.started")
            data: Event data dictionary
        """
        try:
            from ..entrypoints.web import broadcast_event, event_subscribers_snapshot, get_main_loop

            if not event_subscribers_snapshot():
                logger.debug("[SSE] No subscribers, skipping event: %s", event)
                return

            # Try to get the running loop (if called from async context)
            try:
                loop = asyncio.get_running_loop()
                loop.create_task(broadcast_event(event, data))
                logger.debug("[SSE] Scheduled broadcast of %s", event)
            except RuntimeError:
                # No running loop - we're in a worker thread.
                # Use the main loop reference stored by web.py
                main_loop = get_main_loop()
                if main_loop is not None:
                    main_loop.call_soon_threadsafe(
                        lambda: main_loop.create_task(broadcast_event(event, data))
                    )
                    logger.debug("[SSE] Thread-safe scheduled broadcast of %s", event)
                else:
                    logger.debug("[SSE] No main loop available, skipping event: %s", event)

        except ImportError:
            logger.debug("[SSE] Web module not available, skipping event: %s", event)
        except Exception as e:
            logger.warning("[SSE] Failed to broadcast event %s: %s", event, e)

    @hookimpl
    def on_trace_event(self, event: str, data: dict) -> None:
        """Forward trace event to SSE clients."""
        self._broadcast(event, data)
