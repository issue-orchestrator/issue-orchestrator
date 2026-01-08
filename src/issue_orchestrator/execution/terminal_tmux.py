"""Tmux terminal plugin.

Implements terminal hooks using tmux as the backend.
"""

from pathlib import Path
from typing import Optional

from ..infra.hooks.hookspec import hookimpl
from ..adapters.terminal._tmux import TmuxManager, create_tmux_manager


class TmuxPlugin:
    """Terminal plugin for tmux backend.

    Uses session-per-orchestrator architecture:
    - Each TmuxPlugin owns one TmuxManager
    - TmuxManager owns one tmux session
    - Multiple windows (agent sessions) within that session
    - Atomic cleanup on shutdown via kill_orchestrator_session()
    """

    def __init__(
        self,
        manager: Optional[TmuxManager] = None,
        session_name: Optional[str] = None,
    ):
        """Initialize the TmuxPlugin.

        Args:
            manager: Optional pre-configured TmuxManager (for testing/DI).
            session_name: Session name if creating a new manager.
        """
        if manager is not None:
            self._manager = manager
        elif session_name is not None:
            self._manager = create_tmux_manager(session_name=session_name)
        else:
            self._manager = TmuxManager()  # Default: uses SESSION_NAME

    @property
    def manager(self) -> TmuxManager:
        """Get the underlying TmuxManager."""
        return self._manager

    # Lifecycle hooks

    @hookimpl
    def on_orchestrator_startup(self) -> None:
        """Called when orchestrator starts - create tmux session."""
        self._manager.create_orchestrator_session()

    @hookimpl
    def on_orchestrator_shutdown(self) -> None:
        """Called when orchestrator shuts down - kill tmux session.

        This atomically removes ALL agent windows for guaranteed cleanup.
        """
        self._manager.kill_orchestrator_session()

    @hookimpl
    def create_session(
        self,
        session_id: int,
        command: str,
        working_dir: str,
        title: str | None,
    ) -> bool:
        """Create a new tmux window for an issue."""
        try:
            self._manager.create_issue_window(
                issue_number=session_id,
                command=command,
                working_dir=Path(working_dir),
                title=title,
            )
            return True
        except ValueError:
            # Window already exists
            return False
        except Exception:
            return False

    @hookimpl
    def session_exists(self, session_id: int) -> bool:
        """Check if a tmux window exists for the session ID."""
        return self._manager.window_exists(session_id)

    @hookimpl
    def kill_session(self, session_id: int) -> bool:
        """Kill a tmux window."""
        self._manager.kill_window(session_id)
        return True

    @hookimpl
    def discover_running_sessions(self) -> list[dict]:
        """Discover windows that are currently open."""
        issue_numbers = self._manager.list_issue_windows()
        return [
            {
                "issue_number": num,
                "tab_name": f"issue-{num}",
                "is_review": False,
            }
            for num in issue_numbers
        ]

    @hookimpl
    def cleanup_idle_sessions(self) -> int:
        """Clean up idle sessions.

        Tmux doesn't have built-in idle detection. Returns 0 for now.
        """
        return 0

    @hookimpl
    def get_session_output(self, session_id: int, lines: int) -> str | None:
        """Get recent output from a tmux window."""
        return self._manager.capture_pane_output(session_id, lines)

    @hookimpl
    def send_to_session(self, session_id: int, text: str) -> bool:
        """Send text to a tmux window."""
        try:
            self._manager.send_keys(session_id, text)
            return True
        except Exception:
            return False

    @hookimpl
    def session_exists_by_name(self, session_name: str) -> bool:
        """Check if a tmux window exists by its full name."""
        return self._manager.window_exists_by_name(session_name)

    @hookimpl
    def send_to_session_by_name(self, session_name: str, text: str) -> bool:
        """Send text to a tmux window by name."""
        return self._manager.send_keys_by_name(session_name, text)

    @hookimpl
    def focus_session(self, session_id: int) -> bool:
        """Focus a tmux window by issue number."""
        return self._manager.select_window(session_id)
