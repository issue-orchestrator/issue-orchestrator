"""Session runner adapter wrapping pluggy terminal hooks.

This adapter implements the SessionRunner port by delegating to pluggy hooks.
It's the bridge between the core's abstract session management and the concrete
terminal implementations (tmux, iTerm2, etc.).

This is the ONLY place pluggy is used for session management.
"""

import logging
from typing import TYPE_CHECKING

from ..ports.session_runner import SessionRunner

if TYPE_CHECKING:
    import pluggy

logger = logging.getLogger(__name__)


class PluggySessionRunner:
    """SessionRunner implementation that delegates to pluggy terminal hooks.

    This adapter:
    - Implements the SessionRunner protocol
    - Wraps a pluggy PluginManager
    - Delegates to terminal hooks (create_session, session_exists, etc.)
    """

    def __init__(self, plugin_manager: "pluggy.PluginManager"):
        """Initialize with a configured pluggy PluginManager.

        Args:
            plugin_manager: A pluggy PluginManager with terminal hooks registered.
        """
        self._pm = plugin_manager

    def create_session(
        self,
        session_id: int,
        command: str,
        working_dir: str,
        title: str | None = None,
    ) -> bool:
        """Create a terminal session via pluggy hook."""
        result = self._pm.hook.create_session(
            session_id=session_id,
            command=command,
            working_dir=working_dir,
            title=title,
        )
        return result if result is not None else False

    def session_exists(self, session_id: int) -> bool:
        """Check if session exists via pluggy hook."""
        result = self._pm.hook.session_exists(session_id=session_id)
        return result if result is not None else False

    def kill_session(self, session_id: int) -> None:
        """Kill session via pluggy hook."""
        self._pm.hook.kill_session(session_id=session_id)

    def discover_running_sessions(self) -> list[dict]:
        """Discover running sessions via pluggy hook."""
        result = self._pm.hook.discover_running_sessions()
        return result if result is not None else []

    def cleanup_idle_sessions(self) -> int:
        """Clean up idle sessions via pluggy hook."""
        result = self._pm.hook.cleanup_idle_sessions()
        return result if result is not None else 0

    def get_session_output(self, session_id: int, lines: int = 50) -> str | None:
        """Get session output via pluggy hook."""
        return self._pm.hook.get_session_output(session_id=session_id, lines=lines)

    def send_to_session(self, session_id: int, text: str) -> bool:
        """Send text to a session via pluggy hook."""
        result = self._pm.hook.send_to_session(session_id=session_id, text=text)
        return result if result is not None else False
