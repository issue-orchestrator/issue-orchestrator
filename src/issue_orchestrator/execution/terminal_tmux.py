"""Tmux terminal plugin.

Implements terminal hooks using tmux as the backend.
"""

import logging
import os
from pathlib import Path
from typing import Optional

from ..infra.hooks.hookspec import hookimpl
from ..adapters.terminal._tmux import TmuxManager, TmuxHealth, create_tmux_manager

logger = logging.getLogger(__name__)


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
        mode = os.environ.get("ORCHESTRATOR_TMUX_SESSION_MODE", "shared").strip().lower()
        if mode not in ("shared", "per_session"):
            logger.warning("[TMUX] Unknown session mode %r; defaulting to shared", mode)
            mode = "shared"
        self._mode = mode

    @property
    def manager(self) -> TmuxManager:
        """Get the underlying TmuxManager."""
        return self._manager

    # Lifecycle hooks

    @hookimpl
    def on_orchestrator_startup(self) -> None:
        """Called when orchestrator starts - create tmux session.

        Logs health status and attempts recovery if needed.
        """
        logger.info("[TMUX] Initializing tmux backend...")

        # Check initial health
        health = self._manager.health_check()
        if not health.server_running:
            logger.warning("[TMUX] Server not running: %s", health.error)
            logger.info("[TMUX] Attempting to start tmux server...")
            if not self._manager.ensure_server_running():
                logger.error("[TMUX] FAILED to start tmux server - sessions will not work!")
                return

        if self._mode == "shared":
            # Create shared orchestrator session
            try:
                self._manager.create_orchestrator_session()
                health = self._manager.health_check()
                if health.healthy:
                    logger.info(
                        "[TMUX] Ready - server running, session '%s' created",
                        self._manager.session_name,
                    )
                else:
                    logger.error("[TMUX] Session creation may have failed: %s", health.error)
            except Exception as e:
                logger.error("[TMUX] Failed to create session: %s", e)
        else:
            logger.info("[TMUX] Per-session mode enabled; no shared session created")

    def health_check(self) -> TmuxHealth:
        """Check health of tmux backend.

        Returns:
            TmuxHealth with server_running, session_exists, and error details.
        """
        return self._manager.health_check()

    @hookimpl
    def terminal_health_check(self) -> dict:
        """Hook implementation for terminal health check.

        Returns health status as a dict for the hook system.
        """
        health = self._manager.health_check()
        if self._mode == "per_session":
            healthy = health.server_running
            session_exists = True  # No single session in this mode
        else:
            healthy = health.healthy
            session_exists = health.session_exists
        return {
            "healthy": healthy,
            "server_running": health.server_running,
            "session_exists": session_exists,
            "error": health.error,
            "backend": "tmux",
            "session_name": self._manager.session_name,
            "session_mode": self._mode,
        }

    @hookimpl
    def on_orchestrator_shutdown(self) -> None:
        """Called when orchestrator shuts down - kill tmux session.

        This atomically removes ALL agent windows for guaranteed cleanup.
        """
        if self._mode == "shared":
            self._manager.kill_orchestrator_session()
        else:
            for name in self._manager.list_tmux_sessions():
                if name.startswith(("issue-", "review-", "rework-", "triage-")):
                    self._manager.kill_tmux_session(name)

    @hookimpl
    def create_session(
        self,
        session_id: int,
        command: str,
        working_dir: str,
        title: str | None,
        session_name: str | None = None,
    ) -> bool:
        """Create a new tmux window for an issue."""
        try:
            if self._mode == "shared":
                self._manager.create_issue_window(
                    issue_number=session_id,
                    command=command,
                    working_dir=Path(working_dir),
                    title=title,
                )
            else:
                name = session_name or f"issue-{session_id}"
                self._manager.create_standalone_session(
                    session_name=name,
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
    def session_exists(self, session_id: int, session_name: str | None = None) -> bool:
        """Check if a tmux window exists for the session ID."""
        if self._mode == "shared":
            return self._manager.window_exists(session_id)
        name = session_name or f"issue-{session_id}"
        return self._manager.tmux_session_exists(name)

    @hookimpl
    def kill_session(self, session_id: int, session_name: str | None = None) -> bool:
        """Kill a tmux window."""
        if self._mode == "shared":
            self._manager.kill_window(session_id)
        else:
            name = session_name or f"issue-{session_id}"
            self._manager.kill_tmux_session(name)
        return True

    @hookimpl
    def discover_running_sessions(self) -> list[dict]:
        """Discover windows that are currently open."""
        if self._mode == "shared":
            issue_numbers = self._manager.list_issue_windows()
            return [
                {
                    "issue_number": num,
                    "tab_name": f"issue-{num}",
                    "is_review": False,
                }
                for num in issue_numbers
            ]

        sessions = []
        for name in self._manager.list_tmux_sessions():
            if name.startswith(("issue-", "review-", "rework-", "triage-")):
                try:
                    session_type, number_str = name.split("-", 1)
                    number = int(number_str)
                except ValueError:
                    continue
                sessions.append(
                    {
                        "issue_number": number,
                        "tab_name": name,
                        "is_review": session_type == "review",
                    }
                )
        return sessions

    @hookimpl
    def cleanup_idle_sessions(self) -> int:
        """Clean up idle sessions.

        Tmux doesn't have built-in idle detection. Returns 0 for now.
        """
        return 0

    @hookimpl
    def get_session_output(self, session_id: int, lines: int, session_name: str | None = None) -> str | None:
        """Get recent output from a tmux window."""
        if self._mode == "shared":
            return self._manager.capture_pane_output(session_id, lines)
        name = session_name or f"issue-{session_id}"
        return self._manager.capture_session_output(name, lines)

    @hookimpl
    def send_to_session(self, session_id: int, text: str, session_name: str | None = None) -> bool:
        """Send text to a tmux window."""
        try:
            if self._mode == "shared":
                self._manager.send_keys(session_id, text)
            else:
                name = session_name or f"issue-{session_id}"
                return self._manager.send_keys_to_session(name, text)
            return True
        except Exception:
            return False

    @hookimpl
    def session_exists_by_name(self, session_name: str) -> bool:
        """Check if a tmux window exists by its full name."""
        if self._mode == "shared":
            return self._manager.window_exists_by_name(session_name)
        return self._manager.tmux_session_exists(session_name)

    @hookimpl
    def send_to_session_by_name(self, session_name: str, text: str) -> bool:
        """Send text to a tmux window by name."""
        if self._mode == "shared":
            return self._manager.send_keys_by_name(session_name, text)
        return self._manager.send_keys_to_session(session_name, text)

    @hookimpl
    def focus_session(self, session_id: int, session_name: str | None = None) -> bool:
        """Focus a tmux window by issue number."""
        if self._mode == "shared":
            return self._manager.select_window(session_id)
        name = session_name or f"issue-{session_id}"
        return self._manager.select_tmux_session(name)
