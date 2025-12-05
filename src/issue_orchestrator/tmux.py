"""Tmux session management using libtmux.

Uses a single-session, multi-window architecture:
- One tmux session named "orchestrator"
- Each issue gets its own window
- Dashboard runs in window 0
- Easy switching between windows
"""

import os
from pathlib import Path
from typing import Optional

import libtmux

# Constants
SESSION_NAME = "orchestrator"
DASHBOARD_WINDOW = "dashboard"


class TmuxManager:
    """Manages the orchestrator's tmux session and windows."""

    def __init__(self):
        self._server: Optional[libtmux.Server] = None
        self._session: Optional[libtmux.Session] = None

    @property
    def server(self) -> libtmux.Server:
        """Get or create the tmux server connection."""
        if self._server is None:
            self._server = libtmux.Server()
        return self._server

    @property
    def session(self) -> Optional[libtmux.Session]:
        """Get the orchestrator session if it exists."""
        if self._session is None:
            try:
                self._session = self.server.sessions.get(session_name=SESSION_NAME)
            except Exception:
                self._session = None
        return self._session

    def ensure_session(self) -> libtmux.Session:
        """Ensure the orchestrator session exists, create if needed."""
        if self.session is None:
            self._session = self.server.new_session(
                session_name=SESSION_NAME,
                window_name=DASHBOARD_WINDOW,
            )
        # At this point, self._session is guaranteed to be a Session (not None)
        assert self._session is not None
        return self._session

    def has_session(self) -> bool:
        """Check if the orchestrator session exists."""
        return self.session is not None

    def create_issue_window(
        self,
        issue_number: int,
        command: str,
        working_dir: Path,
    ) -> libtmux.Window:
        """Create a new window for an issue and run the command.

        Args:
            issue_number: GitHub issue number
            command: Command to run (e.g., claude with prompt)
            working_dir: Working directory for the window

        Returns:
            The created window
        """
        session = self.ensure_session()
        window_name = f"issue-{issue_number}"

        # Check if window already exists
        existing = session.windows.filter(window_name=window_name)
        if existing:
            raise ValueError(f"Window {window_name} already exists")

        # Create new window
        window = session.new_window(
            window_name=window_name,
            start_directory=str(working_dir),
        )

        # Send the command
        pane = window.active_pane
        if pane is not None:
            pane.send_keys(command)

        return window

    def window_exists(self, issue_number: int) -> bool:
        """Check if a window exists for the given issue."""
        if self.session is None:
            return False
        window_name = f"issue-{issue_number}"
        return bool(self.session.windows.filter(window_name=window_name))

    def get_window(self, issue_number: int) -> Optional[libtmux.Window]:
        """Get the window for an issue, or None if it doesn't exist."""
        if self.session is None:
            return None
        window_name = f"issue-{issue_number}"
        windows = self.session.windows.filter(window_name=window_name)
        return windows[0] if windows else None

    def kill_window(self, issue_number: int) -> None:
        """Kill the window for an issue."""
        window = self.get_window(issue_number)
        if window:
            window.kill()

    def select_window(self, issue_number: int) -> bool:
        """Switch to the window for an issue.

        Returns:
            True if window was selected, False if it doesn't exist
        """
        window = self.get_window(issue_number)
        if window:
            window.select()
            return True
        return False

    def select_dashboard(self) -> bool:
        """Switch to the dashboard window.

        Returns:
            True if dashboard was selected, False if it doesn't exist
        """
        if self.session is None:
            return False
        windows = self.session.windows.filter(window_name=DASHBOARD_WINDOW)
        if windows:
            windows[0].select()
            return True
        return False

    def list_issue_windows(self) -> list[int]:
        """List all issue numbers that have active windows."""
        if self.session is None:
            return []
        issue_numbers = []
        for window in self.session.windows:
            if window.name and window.name.startswith("issue-"):
                try:
                    num = int(window.name.replace("issue-", ""))
                    issue_numbers.append(num)
                except ValueError:
                    pass
        return issue_numbers

    def capture_pane_output(self, issue_number: int, lines: int = 20) -> Optional[str]:
        """Capture recent output from an issue's pane.

        Args:
            issue_number: GitHub issue number
            lines: Number of lines to capture

        Returns:
            The captured output, or None if window doesn't exist
        """
        window = self.get_window(issue_number)
        if window is None:
            return None
        pane = window.active_pane
        if pane is None:
            return None
        output = pane.capture_pane(start=-lines)
        return "\n".join(output) if output else ""

    def kill_session(self) -> None:
        """Kill the entire orchestrator session."""
        if self.session:
            self.session.kill()
            self._session = None


# Global manager instance
_manager: Optional[TmuxManager] = None


def get_manager() -> TmuxManager:
    """Get the global TmuxManager instance."""
    global _manager
    if _manager is None:
        _manager = TmuxManager()
    return _manager


# Backward-compatible functions (for existing code)

def create_session(session_name: str, command: str, working_dir: Path) -> None:
    """Create a window for an issue (backward-compatible wrapper).

    Note: session_name is expected to be "issue-{number}"
    """
    if not session_name.startswith("issue-"):
        raise ValueError(f"Expected session name like 'issue-42', got '{session_name}'")

    issue_number = int(session_name.replace("issue-", ""))
    manager = get_manager()
    manager.create_issue_window(issue_number, command, working_dir)


def session_exists(session_name: str) -> bool:
    """Check if a window exists for the issue (backward-compatible wrapper)."""
    if not session_name.startswith("issue-"):
        return False
    issue_number = int(session_name.replace("issue-", ""))
    manager = get_manager()
    return manager.window_exists(issue_number)


def kill_session(session_name: str) -> None:
    """Kill a window (backward-compatible wrapper)."""
    if not session_name.startswith("issue-"):
        return
    issue_number = int(session_name.replace("issue-", ""))
    manager = get_manager()
    manager.kill_window(issue_number)


def list_sessions() -> list[str]:
    """List all issue session names (backward-compatible wrapper)."""
    manager = get_manager()
    return [f"issue-{num}" for num in manager.list_issue_windows()]


def attach_session(session_name: str) -> None:
    """Attach to a session (replaces current process)."""
    # This still needs to use os.execvp for true attachment
    os.execvp("tmux", ["tmux", "attach-session", "-t", SESSION_NAME])


def send_keys(session_name: str, keys: str, enter: bool = True) -> None:
    """Send keys to a session's pane (backward-compatible wrapper)."""
    if not session_name.startswith("issue-"):
        return
    issue_number = int(session_name.replace("issue-", ""))
    manager = get_manager()
    window = manager.get_window(issue_number)
    if window:
        pane = window.active_pane
        if pane is not None:
            if enter:
                pane.send_keys(keys)
            else:
                pane.send_keys(keys, enter=False)
