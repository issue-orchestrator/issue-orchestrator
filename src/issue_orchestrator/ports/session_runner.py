"""Session runner port for terminal session management.

This port defines the interface for managing terminal sessions where AI agents
run. The orchestrator calls these methods without knowing the implementation
(tmux, iTerm2, Wezterm, etc.).

This abstraction keeps terminal backend details out of the core.
"""

from typing import Protocol


class SessionRunner(Protocol):
    """Port for managing terminal sessions.

    Implementations may use tmux, iTerm2, or other terminal multiplexers.
    The orchestrator doesn't know or care about the specific backend.
    """

    def create_session(
        self,
        session_id: int,
        command: str,
        working_dir: str,
        title: str | None = None,
    ) -> bool:
        """Create a new terminal session for an agent.

        Args:
            session_id: Numeric ID (typically issue number)
            command: Shell command to execute
            working_dir: Working directory path
            title: Optional human-readable title

        Returns:
            True if created successfully, False otherwise.
        """
        ...

    def session_exists(self, session_id: int) -> bool:
        """Check if a session exists and is running.

        Args:
            session_id: Numeric ID to check

        Returns:
            True if session exists and is running.
        """
        ...

    def kill_session(self, session_id: int) -> None:
        """Kill/close a terminal session.

        Args:
            session_id: Numeric ID to kill
        """
        ...

    def discover_running_sessions(self) -> list[dict]:
        """Discover sessions that survived an orchestrator restart.

        Returns:
            List of dicts with session info (issue_number, tab_name, is_review).
        """
        ...

    def cleanup_idle_sessions(self) -> int:
        """Clean up sessions where the agent has exited.

        Returns:
            Number of sessions cleaned up.
        """
        ...

    def get_session_output(self, session_id: int, lines: int = 50) -> str | None:
        """Get recent output from a session.

        Args:
            session_id: Numeric ID
            lines: Number of lines to retrieve

        Returns:
            Terminal output string, or None if not available.
        """
        ...

    def send_to_session(self, session_id: int, text: str) -> bool:
        """Send text to a running session.

        Args:
            session_id: Numeric ID
            text: Text to send (e.g., "/exit")

        Returns:
            True if sent successfully, False otherwise.
        """
        ...


class NullSessionRunner:
    """No-op session runner for testing."""

    def create_session(
        self,
        session_id: int,
        command: str,
        working_dir: str,
        title: str | None = None,
    ) -> bool:
        return True

    def session_exists(self, session_id: int) -> bool:
        return False

    def kill_session(self, session_id: int) -> None:
        pass

    def discover_running_sessions(self) -> list[dict]:
        return []

    def cleanup_idle_sessions(self) -> int:
        return 0

    def get_session_output(self, session_id: int, lines: int = 50) -> str | None:
        return None

    def send_to_session(self, session_id: int, text: str) -> bool:
        return False
