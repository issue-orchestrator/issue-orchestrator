"""iTerm2 terminal plugin.

Implements terminal hooks using iTerm2 as the backend (macOS only).
"""

from ..infra.hooks.hookspec import hookimpl
from ..adapters.terminal._iterm2 import (
    ITermSessionManager,
    discover_running_sessions,
    cleanup_idle_tabs,
)


class ITermPlugin:
    """Terminal plugin for iTerm2 backend (macOS only).

    Uses AppleScript to manage iTerm2 tabs as agent sessions.
    Each issue gets its own tab.
    """

    def __init__(self):
        self._manager = ITermSessionManager()

    @hookimpl
    def create_session(
        self,
        session_id: int,
        command: str,
        working_dir: str,
        title: str | None,
    ) -> bool:
        """Create a new iTerm2 tab for an issue."""
        return self._manager.create_session(
            issue_number=session_id,
            command=command,
            working_dir=working_dir,
            title=title,
        )

    @hookimpl
    def session_exists(self, session_id: int) -> bool:
        """Check if an iTerm2 tab exists and is running."""
        return self._manager.session_exists(session_id)

    @hookimpl
    def kill_session(self, session_id: int) -> bool:
        """Close an iTerm2 tab."""
        return self._manager.kill_session(session_id)

    @hookimpl
    def discover_running_sessions(self) -> list[dict]:
        """Discover iTerm2 tabs that are actively running."""
        return discover_running_sessions()

    @hookimpl
    def cleanup_idle_sessions(self) -> int:
        """Clean up iTerm2 tabs that are at shell prompt."""
        return cleanup_idle_tabs()

    @hookimpl
    def get_session_output(self, session_id: int, lines: int) -> str | None:
        """Get recent output from an iTerm2 tab.

        Note: Not currently implemented for iTerm2.
        """
        return None

    @hookimpl
    def send_to_session(self, session_id: int, text: str) -> bool:
        """Send text to an iTerm2 tab."""
        return self._manager.send_to_session(session_id, text)
