"""Tmux terminal plugin.

Implements terminal hooks using tmux as the backend.
"""

from pathlib import Path

from ..hookspec import hookimpl
from .._tmux_impl import TmuxManager


class TmuxPlugin:
    """Terminal plugin for tmux backend.

    Uses the existing TmuxManager which manages a single tmux session
    with multiple windows (one per issue).
    """

    def __init__(self):
        self._manager = TmuxManager()

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

        Tmux doesn't have built-in idle detection like iTerm2's 'is processing'.
        Returns 0 for now.
        """
        return 0

    @hookimpl
    def get_session_output(self, session_id: int, lines: int) -> str | None:
        """Get recent output from a tmux window."""
        return self._manager.capture_pane_output(session_id, lines)
