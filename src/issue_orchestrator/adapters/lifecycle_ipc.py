"""Lifecycle plugin that broadcasts events via IPC.

This plugin implements lifecycle hooks and forwards all events to the
IPC EventServer, enabling external UI processes to receive real-time
notifications.

This is the bridge between the pluggy hook system and the IPC layer,
allowing any process to subscribe to orchestrator lifecycle events.
"""

import asyncio
import logging
from typing import Any

from ..hookspec import hookimpl
from ..ipc import EventServer

logger = logging.getLogger(__name__)


class LifecycleIPCPlugin:
    """Plugin that broadcasts lifecycle events via IPC.

    This plugin receives lifecycle hook calls from the PluginManager
    and forwards them to all connected IPC clients (UI processes).

    Usage:
        server = EventServer()
        await server.start()

        plugin = LifecycleIPCPlugin(server)
        plugin_manager.register_plugin(plugin, name="lifecycle_ipc")
    """

    def __init__(self, event_server: EventServer):
        """Initialize the plugin.

        Args:
            event_server: The EventServer to broadcast events to
        """
        self.event_server = event_server
        self._loop: asyncio.AbstractEventLoop | None = None

    def _broadcast(self, event_type: str, data: dict[str, Any]) -> None:
        """Broadcast an event to IPC clients.

        This handles the async/sync boundary - hook calls are sync,
        but broadcasting is async.

        Args:
            event_type: Type of event (e.g., "session_started")
            data: Event data dictionary
        """
        event = {"type": event_type, **data}

        # Get or create event loop
        try:
            loop = asyncio.get_running_loop()
            # We're in an async context, schedule the broadcast
            loop.create_task(self.event_server.broadcast(event))
        except RuntimeError:
            # No running loop, try to get the default loop
            try:
                loop = asyncio.get_event_loop()
                if loop.is_running():
                    loop.create_task(self.event_server.broadcast(event))
                else:
                    loop.run_until_complete(self.event_server.broadcast(event))
            except Exception as e:
                logger.warning(f"Failed to broadcast event {event_type}: {e}")

    @hookimpl
    def on_issue_claimed(
        self,
        issue_number: int,
        title: str,
        agent_type: str,
    ) -> None:
        """Forward issue claimed event to IPC."""
        self._broadcast("issue_claimed", {
            "issue_number": issue_number,
            "title": title,
            "agent_type": agent_type,
        })

    @hookimpl
    def on_session_started(
        self,
        issue_number: int,
        session_id: str,
        worktree_path: str,
        branch_name: str,
    ) -> None:
        """Forward session started event to IPC."""
        self._broadcast("session_started", {
            "issue_number": issue_number,
            "session_id": session_id,
            "worktree_path": worktree_path,
            "branch_name": branch_name,
        })

    @hookimpl
    def on_session_completed(
        self,
        issue_number: int,
        session_id: str,
        pr_url: str | None,
        runtime_minutes: float | None,
    ) -> None:
        """Forward session completed event to IPC."""
        self._broadcast("session_completed", {
            "issue_number": issue_number,
            "session_id": session_id,
            "pr_url": pr_url,
            "runtime_minutes": runtime_minutes,
        })

    @hookimpl
    def on_session_failed(
        self,
        issue_number: int,
        session_id: str,
        error: str | None,
        runtime_minutes: float | None,
    ) -> None:
        """Forward session failed event to IPC."""
        self._broadcast("session_failed", {
            "issue_number": issue_number,
            "session_id": session_id,
            "error": error,
            "runtime_minutes": runtime_minutes,
        })

    @hookimpl
    def on_issue_blocked(
        self,
        issue_number: int,
        reason: str | None,
    ) -> None:
        """Forward issue blocked event to IPC."""
        self._broadcast("issue_blocked", {
            "issue_number": issue_number,
            "reason": reason,
        })

    @hookimpl
    def on_issue_needs_human(
        self,
        issue_number: int,
        reason: str | None,
    ) -> None:
        """Forward issue needs human event to IPC."""
        self._broadcast("issue_needs_human", {
            "issue_number": issue_number,
            "reason": reason,
        })

    @hookimpl
    def on_pr_created(
        self,
        issue_number: int,
        pr_number: int,
        pr_url: str,
        title: str,
    ) -> None:
        """Forward PR created event to IPC."""
        self._broadcast("pr_created", {
            "issue_number": issue_number,
            "pr_number": pr_number,
            "pr_url": pr_url,
            "title": title,
        })

    @hookimpl
    def on_review_requested(
        self,
        pr_number: int,
        issue_number: int,
        review_type: str,
    ) -> None:
        """Forward review requested event to IPC."""
        self._broadcast("review_requested", {
            "pr_number": pr_number,
            "issue_number": issue_number,
            "review_type": review_type,
        })

    @hookimpl
    def on_review_completed(
        self,
        pr_number: int,
        issue_number: int,
        result: str,
        rework_count: int,
    ) -> None:
        """Forward review completed event to IPC."""
        self._broadcast("review_completed", {
            "pr_number": pr_number,
            "issue_number": issue_number,
            "result": result,
            "rework_count": rework_count,
        })

    @hookimpl
    def on_review_escalated(
        self,
        pr_number: int,
        issue_number: int,
        rework_count: int,
        max_rework_cycles: int,
    ) -> None:
        """Forward review escalated event to IPC.

        This is a critical event - the bounded review loop has failed.
        """
        self._broadcast("review_escalated", {
            "pr_number": pr_number,
            "issue_number": issue_number,
            "rework_count": rework_count,
            "max_rework_cycles": max_rework_cycles,
        })

    @hookimpl
    def on_orchestrator_state_changed(
        self,
        active_count: int,
        paused: bool,
        completed_today: int,
    ) -> None:
        """Forward orchestrator state changed event to IPC."""
        self._broadcast("state_changed", {
            "active_count": active_count,
            "paused": paused,
            "completed_today": completed_today,
        })
