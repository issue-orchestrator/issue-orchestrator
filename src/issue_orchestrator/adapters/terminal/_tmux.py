"""Tmux session management using libtmux.

Uses a session-per-orchestrator architecture:
- Each orchestrator instance owns one tmux session
- Each agent session (code, review, rework, triage) gets its own window
- Atomic cleanup: kill_session() removes ALL windows at once
- No global state - TmuxManager instances are created via factory

LIBTMUX QUIRKS (documented with tests in tests/integration/test_tmux_live.py):
==============================================================================

1. pane.pane_title is ALWAYS None:
   - libtmux does NOT auto-populate the pane_title attribute
   - You MUST use pane.cmd("display-message", "-p", "#{pane_title}") to get it
   - This is the root cause of many session lookup/focus/kill bugs
   - Use self._get_pane_title(pane) helper method, NEVER getattr(pane, "pane_title")

2. server.sessions.filter() returns empty list, not exception:
   - filter(session_name="nonexistent") returns [] not raises
   - Contrast with .get() which may raise
   - Always check len() of filter result

3. Use libtmux API for all tmux operations:
   - server.new_session() instead of subprocess.run(["tmux", "new-session", ...])
   - session.new_window() instead of subprocess.run(["tmux", "new-window", ...])
   - session.kill() instead of subprocess.run(["tmux", "kill-session", ...])
   - pane.cmd("select-pane", "-T", title) to set pane title
"""

import logging
import os
import platform
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Optional

import libtmux
from libtmux import Window as LibtmuxWindow
from libtmux.exc import LibTmuxException
from libtmux.constants import PaneDirection

from issue_orchestrator.domain import ProcessState, ProcessExitInfo

from ._tmux_retry import tmux_retry, PaneAlreadyExistsError

logger = logging.getLogger(__name__)

# Constants
# Allow override via environment variable for e2e test isolation (legacy)
SESSION_NAME = os.environ.get("ORCHESTRATOR_TMUX_SESSION", "orchestrator")
DASHBOARD_WINDOW = "dashboard"
AGENTS_WINDOW = "agents"  # Window where all agent panes live
PANE_SESSION_ID_OPTION = "@orchestrator-session-id"  # Custom tmux option for session ID

# TTL for cached pane state (milliseconds)
PANE_STATE_TTL_MS = 500

@dataclass
class TmuxHealth:
    """Health status of tmux server and session."""

    server_running: bool
    session_exists: bool
    error: str | None = None

    @property
    def healthy(self) -> bool:
        """True if tmux server is running and session exists."""
        return self.server_running and self.session_exists


class TmuxPaneState:
    """Cached pane state with TTL refresh.

    Caches pane attributes like pane_dead, pane_dead_status to avoid
    repeated tmux queries. Uses TTL-based refresh for efficiency.

    tmux pane attributes used:
    - pane_dead: '1' if process exited, '0' if running
    - pane_dead_status: Exit code as string (e.g., '0', '1', '137')
    - pane_dead_signal: Signal name if killed by signal (e.g., 'SIGTERM')
    """

    def __init__(self, pane: libtmux.Pane, ttl_ms: int = PANE_STATE_TTL_MS):
        """Initialize pane state wrapper.

        Args:
            pane: The libtmux Pane to observe
            ttl_ms: Cache TTL in milliseconds
        """
        self._pane = pane
        self._ttl_ms = ttl_ms
        self._last_refresh: float = 0

    def _ensure_fresh(self) -> None:
        """Refresh pane attributes if TTL expired."""
        now = time.monotonic() * 1000  # Convert to milliseconds
        if now - self._last_refresh > self._ttl_ms:
            self._pane.refresh()
            self._last_refresh = now

    @property
    def is_dead(self) -> bool:
        """True if the process in the pane has exited."""
        self._ensure_fresh()
        return getattr(self._pane, "pane_dead", "0") == "1"

    @property
    def exit_code(self) -> int | None:
        """Exit code of the process, or None if still running or unknown."""
        self._ensure_fresh()
        status = getattr(self._pane, "pane_dead_status", None)
        if status is not None and status != "":
            try:
                return int(status)
            except (ValueError, TypeError):
                return None
        return None

    @property
    def signal(self) -> str | None:
        """Signal that killed the process, or None if not signaled."""
        self._ensure_fresh()
        sig = getattr(self._pane, "pane_dead_signal", None)
        if sig and sig != "":
            return sig
        return None

    @property
    def pane(self) -> libtmux.Pane:
        """The underlying pane object."""
        return self._pane


class TmuxManager:
    """Manages one tmux session for an orchestrator instance.

    Session-per-orchestrator architecture:
    - Each TmuxManager owns one tmux session
    - Multiple windows (agent sessions) within that session
    - kill_session() provides atomic cleanup of all windows

    Mappings are handled at the orchestrator level:
    - issue_number → terminal_id: via state.active_sessions
    - terminal_id → window: via _find_window_by_name() (iterates windows)
    """

    def __init__(
        self,
        server: Optional[libtmux.Server] = None,
        session_name: str = SESSION_NAME,
    ):
        """Initialize TmuxManager.

        Args:
            server: libtmux Server instance. If None, creates default server.
            session_name: Name for the tmux session this manager owns.
        """
        self._server = server
        self._session_name = session_name
        self._session: Optional[libtmux.Session] = None

    @property
    def server(self) -> libtmux.Server:
        """Get or create the tmux server connection."""
        if self._server is None:
            self._server = libtmux.Server()
        return self._server

    @property
    def session(self) -> Optional[libtmux.Session]:
        """Get the orchestrator session if it exists.

        Note: Session cache is cleared by @tmux_retry on operation failures,
        handling the case where tmux session was killed externally.
        """
        if self._session is None:
            try:
                self._session = self.server.sessions.get(session_name=self._session_name)
            except LibTmuxException as e:
                logger.warning("[TMUX] Cannot get session - server may be down: %s", str(e).strip())
                self._session = None
            except Exception:
                self._session = None
        return self._session

    @property
    def session_name(self) -> str:
        """Get the session name."""
        return self._session_name

    def health_check(self) -> TmuxHealth:
        """Check health of tmux server and session.

        Note: No retry - health_check should report actual state, not mask failures.
        """
        try:
            sessions = self.server.sessions
            session_exists = any(s.session_name == self._session_name for s in sessions)
            return TmuxHealth(server_running=True, session_exists=session_exists)
        except LibTmuxException as e:
            return TmuxHealth(server_running=False, session_exists=False, error=str(e).strip())

    @tmux_retry
    def ensure_server_running(self) -> bool:
        """Ensure tmux server is running, creating fresh connection if needed."""
        if self.server.is_alive():
            return True
        # Create fresh server connection (libtmux starts server on first command)
        self._server = libtmux.Server()
        return self._server.is_alive()

    def ensure_session_exists(self) -> bool:
        """Ensure our tmux session exists, creating it if necessary.

        This provides auto-recovery when the session was killed or server restarted.

        Returns:
            True if session now exists, False if creation failed.
        """
        # First ensure server is running
        if not self.ensure_server_running():
            return False

        health = self.health_check()
        if health.session_exists:
            return True

        logger.warning("[TMUX] Session '%s' not found, creating...", self._session_name)

        try:
            self.create_orchestrator_session()
            logger.info("[TMUX] Session '%s' created successfully", self._session_name)
            return True
        except Exception as e:
            logger.error("[TMUX] Failed to create session: %s", e)
            return False

    @tmux_retry
    def create_orchestrator_session(
        self,
        tmux_bindings: list[str] | None = None,
    ) -> libtmux.Session:
        """Create the tmux session for this orchestrator, or reuse existing.

        Configures:
        - Mouse mode (click to select panes)
        - Custom key bindings (default: double-click to zoom/unzoom panes)
        - Dashboard window for orchestrator status
        - Agents window where all agent panes live (tiled layout)

        Args:
            tmux_bindings: List of tmux commands to run (e.g., bind-key commands).
                          If None, uses default (double-click to zoom).
        """
        if self._session is not None:
            return self._session

        # Try to find existing session first
        existing_sessions = self.server.sessions.filter(session_name=self._session_name)
        if existing_sessions:
            self._session = existing_sessions[0]
            return self._session

        # Create new session with dashboard window
        self._session = self.server.new_session(
            session_name=self._session_name,
            window_name=DASHBOARD_WINDOW,
        )

        # Configure mouse mode (session-level)
        try:
            self._session.set_option("mouse", "on")
        except Exception as e:
            logger.warning("[TMUX] Failed to enable mouse mode: %s", e)

        # Apply custom tmux bindings (default: double-click to zoom)
        if tmux_bindings is None:
            tmux_bindings = ["bind-key -T root DoubleClick1Pane resize-pane -Z -t ="]

        for binding in tmux_bindings:
            try:
                # Parse the binding command and execute via server.cmd
                # binding is like "bind-key -T root DoubleClick1Pane resize-pane -Z -t ="
                parts = binding.split()
                if parts:
                    self.server.cmd(*parts)
            except Exception as e:
                logger.warning("[TMUX] Failed to apply binding '%s': %s", binding, e)

        # Create the agents window (where all agent panes will live)
        try:
            agents_window = self._session.new_window(window_name=AGENTS_WINDOW)
            # Set initial tiled layout
            agents_window.select_layout("tiled")
        except Exception as e:
            logger.warning("[TMUX] Failed to create agents window: %s", e)

        # Switch back to dashboard window
        try:
            dashboard = self._session.windows.filter(window_name=DASHBOARD_WINDOW)
            if dashboard:
                dashboard[0].select()
        except Exception:
            pass

        return self._session

    def _get_agents_window(self) -> libtmux.Window | None:
        """Get the agents window where all agent panes live.

        Returns:
            The agents window, or None if session doesn't exist or no agents window.
        """
        # Use session property to trigger re-lookup if _session is None
        sess = self.session
        if sess is None:
            return None
        try:
            windows = sess.windows.filter(window_name=AGENTS_WINDOW)
            return windows[0] if windows else None
        except Exception:
            return None

    def _is_pane_mode(self) -> bool:
        """Check if we're in pane mode (agents window exists) or window mode.

        Returns:
            True if agents window exists (pane mode), False otherwise (window mode).
        """
        return self._get_agents_window() is not None

    def _get_pane_title(self, pane: libtmux.Pane) -> str:
        """Get the title of a pane.

        libtmux doesn't automatically fetch pane_title, so we use cmd to get it.

        Args:
            pane: The pane to get the title from.

        Returns:
            The pane title, or empty string if unavailable.
        """
        try:
            result = pane.cmd("display-message", "-p", "#{pane_title}")
            if result.stdout:
                return result.stdout[0]
        except Exception:
            pass
        return ""

    def _set_pane_session_id(self, pane: libtmux.Pane, session_id: str) -> None:
        """Set the orchestrator session ID on a pane.

        Uses a custom tmux option (@orchestrator-session-id) that applications
        cannot overwrite, unlike pane_title which Claude Code modifies.

        Args:
            pane: The pane to set the session ID on.
            session_id: The session identifier (e.g., "#123-issue-title").
        """
        try:
            pane.cmd("set-option", "-p", PANE_SESSION_ID_OPTION, session_id)
        except Exception as e:
            logger.warning("[TMUX] Failed to set session ID on pane: %s", e)

    def _get_pane_session_id(self, pane: libtmux.Pane) -> str:
        """Get the orchestrator session ID from a pane.

        Reads the custom tmux option that we set when creating the pane.
        This is more reliable than pane_title which can be overwritten.

        Args:
            pane: The pane to get the session ID from.

        Returns:
            The session ID, or empty string if not set.
        """
        try:
            result = pane.cmd("show-options", "-p", "-v", PANE_SESSION_ID_OPTION)
            if result.stdout:
                return result.stdout[0]
        except Exception:
            pass
        return ""

    def _find_pane_by_session_id(self, session_id: str) -> libtmux.Pane | None:
        """Find a pane by its orchestrator session ID.

        Uses the custom @orchestrator-session-id option which is reliable,
        unlike pane_title which Claude Code can overwrite.

        Args:
            session_id: The session identifier to search for.

        Returns:
            The pane with matching session ID, or None if not found.
        """
        agents_window = self._get_agents_window()
        if agents_window is None:
            return None
        try:
            for pane in agents_window.panes:
                pane_session_id = self._get_pane_session_id(pane)
                if pane_session_id == session_id:
                    return pane
            return None
        except Exception:
            return None

    def _find_pane(self, session_id: str) -> libtmux.Pane | None:
        """Find a pane in the agents window by its session ID.

        First checks the @orchestrator-session-id option (reliable), then
        falls back to pane_title (for backwards compatibility with old panes).

        Args:
            session_id: The session identifier to search for.

        Returns:
            The pane with matching session ID, or None if not found.
        """
        # First try by session ID option (reliable, not affected by Claude Code)
        pane = self._find_pane_by_session_id(session_id)
        if pane is not None:
            return pane

        # Fall back to pane title (for old panes without session ID option)
        agents_window = self._get_agents_window()
        if agents_window is None:
            return None
        try:
            for pane in agents_window.panes:
                pane_title = self._get_pane_title(pane)
                if pane_title == session_id:
                    return pane
            return None
        except Exception:
            return None

    # Backwards compatibility alias
    _find_pane_by_title = _find_pane

    def _is_pane_empty(self, pane: libtmux.Pane) -> bool:
        """Check if a pane is the initial empty/waiting pane.

        Args:
            pane: The pane to check.

        Returns:
            True if the pane appears to be an empty placeholder.
        """
        try:
            # Check if pane is running just a shell (no command)
            cmd = getattr(pane, "pane_current_command", "")
            if cmd in ("bash", "zsh", "sh", "fish", ""):
                return True
            return False
        except Exception:
            return False

    def kill_orchestrator_session(self) -> None:
        """Kill the entire orchestrator session.

        This atomically removes ALL agent windows in this session.
        Should be called at orchestrator shutdown for guaranteed cleanup.
        """
        if self._session is not None:
            try:
                self._session.kill()
            except Exception:
                pass  # Session may already be killed
            self._session = None

    # Legacy compatibility methods
    def ensure_session(self) -> libtmux.Session:
        """Ensure the orchestrator session exists, create if needed.

        Legacy method - prefer create_orchestrator_session() for new code.
        """
        return self.create_orchestrator_session()

    def has_session(self) -> bool:
        """Check if the orchestrator session exists."""
        return self.session is not None

    @tmux_retry
    def create_issue_window(
        self,
        issue_number: int,
        command: str,
        working_dir: Path,
        title: str | None = None,
    ) -> libtmux.Pane | libtmux.Window:
        """Create a new pane or window for an issue and run the command.

        Adaptive mode:
        - If "agents" window exists (pane mode): creates a pane in tiled layout
        - If no "agents" window (window mode): creates a separate window/tab

        Args:
            issue_number: The GitHub issue number.
            command: The command to run.
            working_dir: Working directory for the command.
            title: Optional title for the issue.

        Returns:
            The created pane (pane mode) or window (window mode).

        Raises:
            PaneAlreadyExistsError: If a session with the same ID already exists.
        """
        session = self.ensure_session()

        # Build session identifier - MUST match what session_launcher uses for terminal_id
        # The title is only used for display, not for the session ID
        session_id = f"issue-{issue_number}"

        # Check if session already exists (works for both modes)
        if self._find_session_by_id(session_id) is not None:
            raise PaneAlreadyExistsError(f"Session {session_id} already exists")

        # Adaptive: use pane mode if agents window exists, otherwise window mode
        if self._is_pane_mode():
            return self._create_pane(session, session_id, command, working_dir, title)
        else:
            return self._create_window(session, session_id, command, working_dir, title)

    def _create_pane(
        self,
        session: libtmux.Session,
        session_id: str,
        command: str,
        working_dir: Path,
        title: str | None = None,
    ) -> libtmux.Pane:
        """Create a pane in the agents window (pane mode)."""
        agents_window = self._get_agents_window()
        assert agents_window is not None  # Caller verified pane mode

        # Create pane: reuse empty placeholder pane or split from last pane
        panes = agents_window.panes
        if len(panes) == 1 and self._is_pane_empty(panes[0]):
            pane = panes[0]
        else:
            pane = panes[-1].split(direction=PaneDirection.Right)

        # Set pane title for display (may be overwritten by Claude Code)
        # Use title if provided, otherwise fall back to session_id
        display_title = title[:30] if title else session_id
        pane.cmd("select-pane", "-T", display_title)

        # Set session ID option for reliable identification (not affected by Claude)
        self._set_pane_session_id(pane, session_id)

        # Enable remain-on-exit for exit code capture
        try:
            pane.cmd("set-option", "-p", "remain-on-exit", "on")
        except Exception:
            pass

        # Apply tiled layout
        agents_window.select_layout("tiled")

        # Set up and run
        self._setup_and_run(pane, command, working_dir, session_id)
        return pane

    def _create_window(
        self,
        session: libtmux.Session,
        session_id: str,
        command: str,
        working_dir: Path,
        title: str | None = None,
    ) -> libtmux.Window:
        """Create a separate window (window mode)."""
        # Use title for display if provided, otherwise use session_id
        window_name = title[:30] if title else session_id
        window = session.new_window(window_name=window_name)
        pane = window.active_pane
        if pane is None:
            # Shouldn't happen, but handle gracefully
            raise RuntimeError(f"New window {session_id} has no active pane")

        # Enable remain-on-exit for exit code capture
        try:
            pane.cmd("set-option", "-p", "remain-on-exit", "on")
        except Exception:
            pass

        # Set up and run
        self._setup_and_run(pane, command, working_dir, session_id)
        return window

    def _setup_and_run(
        self,
        pane: libtmux.Pane,
        command: str,
        working_dir: Path,
        session_id: str,
    ) -> None:
        """Set up isolated environment and run command in pane."""
        from ...control.isolation import build_isolation_prefix
        wrapper_dir = Path(__file__).parent.parent.parent / "scripts"
        isolation_prefix = build_isolation_prefix(
            worktree=working_dir,
            isolation_mode="standard",
            scrub_env=True,
            isolate_home=False,
        )
        # CRITICAL: cd to working directory first, then set up isolation
        setup_cmd = f'cd "{working_dir}" && export PATH="{wrapper_dir}:$PATH" && {isolation_prefix}'

        # Enable pane logging with ANSI code stripping
        # Uses ansifilter if available (brew install ansifilter), otherwise sed
        try:
            log_dir = working_dir / ".issue-orchestrator"
            log_dir.mkdir(parents=True, exist_ok=True)
            log_file = log_dir / "pane.log"
            # ansifilter produces cleaner output; sed is good enough fallback
            filter_cmd = (
                "if command -v ansifilter >/dev/null 2>&1; then ansifilter; "
                "else sed -E 's/\\x1b\\[[0-9;]*[a-zA-Z]//g'; fi"
            )
            pane.cmd("pipe-pane", "-o", f"exec cat - | {filter_cmd} >> '{log_file}'")
            logger.debug("[TMUX] Enabled pane logging to %s", log_file)
        except Exception as e:
            logger.warning("[TMUX] Failed to enable pane logging: %s", e)

        # Send setup and command
        pane.send_keys(setup_cmd)
        pane.send_keys(command)

    def _find_session_by_id(self, session_id: str) -> libtmux.Pane | libtmux.Window | None:
        """Find a session (pane or window) by its ID.

        Adaptive: searches panes in pane mode, windows in window mode.
        """
        if self._is_pane_mode():
            return self._find_pane_by_title(session_id)
        else:
            return self._find_window_by_session_id(session_id)

    def _find_window_by_session_id(self, session_id: str) -> libtmux.Window | None:
        """Find a window by its name (window mode)."""
        if self._session is None:
            return None
        try:
            windows = self._session.windows.filter(window_name=session_id)
            return windows[0] if windows else None
        except Exception:
            return None

    def _find_issue_session(self, issue_number: int) -> libtmux.Pane | libtmux.Window | None:
        """Find session for an issue by number (adaptive: pane or window mode).

        Handles both naming conventions: #{number}-{title} and issue-{number}.
        """
        if self._is_pane_mode():
            return self._find_issue_pane_internal(issue_number)
        else:
            return self._find_issue_window_internal(issue_number)

    def _find_issue_pane_internal(self, issue_number: int) -> libtmux.Pane | None:
        """Find pane for an issue in agents window (pane mode).

        Checks @orchestrator-session-id option first (reliable), then
        falls back to pane_title (for backwards compatibility).
        """
        agents_window = self._get_agents_window()
        if agents_window is None:
            return None
        try:
            for pane in agents_window.panes:
                # Check session ID option first (reliable)
                session_id = self._get_pane_session_id(pane)
                if session_id.startswith(f"#{issue_number}-"):
                    return pane
                if session_id == f"issue-{issue_number}":
                    return pane

                # Fall back to pane title (for old panes)
                pane_title = self._get_pane_title(pane)
                if pane_title.startswith(f"#{issue_number}-"):
                    return pane
                if pane_title == f"issue-{issue_number}":
                    return pane
            return None
        except LibTmuxException as e:
            logger.warning("[TMUX] _find_issue_pane_internal(%d) failed: %s", issue_number, str(e).strip())
            return None

    def _find_issue_window_internal(self, issue_number: int) -> libtmux.Window | None:
        """Find window for an issue (window mode)."""
        if self._session is None:
            return None
        try:
            for window in self._session.windows:
                name = window.window_name or ""
                if name.startswith(f"#{issue_number}-"):
                    return window
                if name == f"issue-{issue_number}":
                    return window
            return None
        except LibTmuxException as e:
            logger.warning("[TMUX] _find_issue_window_internal(%d) failed: %s", issue_number, str(e).strip())
            return None

    def _find_session_by_name(self, terminal_id: str) -> libtmux.Pane | libtmux.Window | None:
        """Find session by terminal_id (adaptive: pane or window mode)."""
        if self._is_pane_mode():
            return self._find_pane_by_name_internal(terminal_id)
        else:
            return self._find_window_by_session_id(terminal_id)

    def _find_pane_by_name_internal(self, terminal_id: str) -> libtmux.Pane | None:
        """Find pane by its terminal_id in agents window (pane mode).

        Checks @orchestrator-session-id option first (reliable), then
        falls back to pane_title (for backwards compatibility).
        """
        agents_window = self._get_agents_window()
        if agents_window is None:
            return None
        try:
            for pane in agents_window.panes:
                # Check session ID option first (reliable)
                session_id = self._get_pane_session_id(pane)
                if session_id == terminal_id:
                    return pane

                # Fall back to pane title (for old panes)
                pane_title = self._get_pane_title(pane)
                if pane_title == terminal_id:
                    return pane
            return None
        except LibTmuxException as e:
            logger.warning("[TMUX] _find_pane_by_name_internal(%s) failed: %s", terminal_id, str(e).strip())
            return None

    # Legacy aliases
    def _find_issue_pane(self, issue_number: int) -> libtmux.Pane | libtmux.Window | None:
        """Find session for an issue (adaptive)."""
        return self._find_issue_session(issue_number)

    def _find_pane_by_name(self, terminal_id: str) -> libtmux.Pane | libtmux.Window | None:
        """Find session by name (adaptive)."""
        return self._find_session_by_name(terminal_id)

    def _get_issue_pane_for_io(self, issue_number: int) -> libtmux.Pane | None:
        """Get pane for an issue, for I/O operations (capture, send_keys).

        In window mode, extracts the active pane from the window.
        """
        session = self._find_issue_session(issue_number)
        if session is None:
            return None
        if isinstance(session, LibtmuxWindow):
            # Window mode: get active pane
            return session.active_pane
        # Pane mode or compatible mock
        return session

    def _get_pane_for_io_by_name(self, terminal_id: str) -> libtmux.Pane | None:
        """Get pane by terminal_id, for I/O operations (capture, send_keys).

        In window mode, extracts the active pane from the window.
        """
        session = self._find_session_by_name(terminal_id)
        if session is None:
            return None
        if isinstance(session, LibtmuxWindow):
            # Window mode: get active pane
            return session.active_pane
        # Pane mode or compatible mock
        return session

    def _find_issue_window(self, issue_number: int) -> libtmux.Pane | libtmux.Window | None:
        """Find session for an issue (legacy alias)."""
        return self._find_issue_session(issue_number)

    def _find_window_by_name(self, session_name: str) -> libtmux.Pane | libtmux.Window | None:
        """Find session by name (legacy alias)."""
        return self._find_session_by_name(session_name)

    def window_exists(self, issue_number: int) -> bool:
        """Check if a session exists for the given issue (adaptive)."""
        return self._find_issue_session(issue_number) is not None

    def window_exists_by_name(self, session_name: str) -> bool:
        """Check if a session exists by terminal_id (adaptive)."""
        return self._find_session_by_name(session_name) is not None

    def get_window(self, issue_number: int) -> libtmux.Pane | libtmux.Window | None:
        """Get the session for an issue (adaptive)."""
        return self._find_issue_session(issue_number)

    def wait_for_issue_session(
        self,
        issue_number: int,
        timeout_s: float = 30.0,
        poll_interval_s: float = 1.0,
    ) -> libtmux.Pane | libtmux.Window | None:
        """Wait for a session to exist for an issue (adaptive).

        Uses tenacity for robust retry semantics with exponential backoff.
        Handles timing issues where the session is being created but not yet visible.

        Args:
            issue_number: The issue number to find.
            timeout_s: Maximum time to wait in seconds.
            poll_interval_s: Initial poll interval (grows with jitter).

        Returns:
            The pane or window if found within timeout, None otherwise.
        """
        from tenacity import (
            retry,
            retry_if_result,
            stop_after_delay,
            wait_exponential_jitter,
        )

        # Scale max interval to ~1/3 of timeout (e.g., 10s max for 30s timeout)
        # This ensures we get at least a few retries with reasonable intervals
        max_interval = min(max(timeout_s / 3, poll_interval_s * 2), 15.0)

        # Retry while result is None (session not found)
        @retry(
            retry=retry_if_result(lambda x: x is None),
            wait=wait_exponential_jitter(initial=poll_interval_s, max=max_interval, jitter=0.5),
            stop=stop_after_delay(timeout_s),
            reraise=False,
        )
        def _find_with_retry() -> libtmux.Pane | libtmux.Window | None:
            return self._find_issue_session(issue_number)

        try:
            return _find_with_retry()
        except Exception:
            return None

    def kill_window(self, issue_number: int) -> None:
        """Kill the session for an issue (adaptive)."""
        session = self._find_issue_session(issue_number)
        if session:
            self._kill_session_item(session)

    def kill_window_by_name(self, session_name: str) -> None:
        """Kill a session by its terminal_id (adaptive)."""
        session = self._find_session_by_name(session_name)
        if session:
            self._kill_session_item(session)

    def _kill_session_item(self, item: libtmux.Pane | libtmux.Window) -> None:
        """Kill a pane or window (adaptive)."""
        # Use duck typing: panes have pane_title, windows have window_name
        if hasattr(item, "pane_title"):
            self._kill_pane(item)  # type: ignore[arg-type]
        else:
            self._kill_window_item(item)  # type: ignore[arg-type]

    def _kill_pane(self, pane: libtmux.Pane) -> None:
        """Kill a pane and re-tile the remaining panes."""
        # Stop logging first
        try:
            pane.cmd("pipe-pane")
        except Exception:
            pass
        # Kill the pane
        try:
            pane.kill()
        except Exception:
            pass
        # Re-tile remaining panes
        agents_window = self._get_agents_window()
        if agents_window:
            try:
                agents_window.select_layout("tiled")
            except Exception:
                pass

    def _kill_window_item(self, window: libtmux.Window) -> None:
        """Kill a window."""
        # Stop logging on active pane first
        try:
            if window.active_pane:
                window.active_pane.cmd("pipe-pane")
        except Exception:
            pass
        # Kill the window
        try:
            window.kill()
        except Exception:
            pass

    def select_window(self, issue_number: int) -> bool:
        """Switch to the session for an issue and open terminal if needed (adaptive).

        Returns:
            True if session was selected, False if it doesn't exist
        """
        session = self._find_issue_session(issue_number)
        if session is None:
            return False

        # In pane mode, select agents window first, then the pane
        if self._is_pane_mode():
            agents_window = self._get_agents_window()
            if agents_window:
                agents_window.select()
        session.select()

        # Open terminal if no client is attached
        self._ensure_terminal_attached()
        return True

    def _ensure_terminal_attached(self) -> None:
        """Ensure a terminal is attached to the tmux session.

        On macOS, opens Terminal.app if no client is connected.
        Disabled during tests (PYTEST_CURRENT_TEST env var).
        """
        # Don't open terminal windows during tests
        if os.environ.get("PYTEST_CURRENT_TEST"):
            return

        if self.session is None:
            return

        # Check if any client is attached by listing clients for this session
        try:
            result = subprocess.run(
                ["tmux", "list-clients", "-t", self._session_name],
                capture_output=True,
                text=True,
                timeout=2,
            )
            if result.returncode == 0 and result.stdout.strip():
                logger.debug("Client already attached to tmux session")
                return
        except Exception:
            pass  # Proceed to open terminal

        # Open terminal based on platform
        if platform.system() == "Darwin":
            self._open_macos_terminal()
        else:
            logger.info(
                "No terminal attached. Attach manually with: tmux attach -t %s",
                self._session_name,
            )

    def _open_macos_terminal(self) -> None:
        """Open Terminal.app on macOS and attach to tmux session.

        Creates a temporary shell script and uses 'open' to launch Terminal.
        This avoids AppleScript which has proven unreliable.
        """
        import tempfile

        session_name = self._session_name

        # Create a temp script that attaches to tmux
        script_content = f"""#!/bin/bash
tmux attach-session -t {session_name}
"""
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", suffix=".command", delete=False
            ) as f:
                f.write(script_content)
                script_path = f.name

            # Make executable
            os.chmod(script_path, 0o755)

            # Open with Terminal.app (macOS opens .command files in Terminal)
            subprocess.Popen(
                ["open", script_path],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            logger.info("Opened Terminal.app with tmux session %s", session_name)

            # Clean up script after a delay (give Terminal time to read it)
            def cleanup_script():
                time.sleep(2)
                try:
                    os.unlink(script_path)
                except Exception:
                    pass

            import threading
            threading.Thread(target=cleanup_script, daemon=True).start()

        except Exception as e:
            logger.warning("Failed to open Terminal.app: %s", e)

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
        """List all issue numbers that have active panes.

        Checks @orchestrator-session-id option first (reliable), then
        falls back to pane_title (for backwards compatibility).
        """
        agents_window = self._get_agents_window()
        if agents_window is None:
            return []
        issue_numbers = []
        for pane in agents_window.panes:
            # Try session ID option first (reliable)
            session_id = self._get_pane_session_id(pane)
            if session_id:
                if session_id.startswith("#"):
                    try:
                        num = int(session_id.split("-")[0][1:])
                        issue_numbers.append(num)
                        continue
                    except (ValueError, IndexError):
                        pass
                elif session_id.startswith("issue-"):
                    try:
                        num = int(session_id.replace("issue-", ""))
                        issue_numbers.append(num)
                        continue
                    except ValueError:
                        pass

            # Fall back to pane title (for old panes)
            pane_title = self._get_pane_title(pane)
            if not pane_title:
                continue
            # New format: #{number}-{title}
            if pane_title.startswith("#"):
                try:
                    num = int(pane_title.split("-")[0][1:])  # Extract number after #
                    issue_numbers.append(num)
                except (ValueError, IndexError):
                    pass
            # Old format: issue-{number}
            elif pane_title.startswith("issue-"):
                try:
                    num = int(pane_title.replace("issue-", ""))
                    issue_numbers.append(num)
                except ValueError:
                    pass
        return issue_numbers

    def _stop_pipe(self, window: libtmux.Window) -> None:
        """Stop tmux pipe-pane logging so subprocesses don't linger."""
        try:
            pane = window.active_pane
        except Exception:
            return
        if pane is None:
            return
        try:
            pane.cmd("pipe-pane")
        except Exception:
            pass

    def capture_pane_output(self, issue_number: int, lines: int = 20) -> Optional[str]:
        """Capture recent output from an issue's pane.

        Args:
            issue_number: GitHub issue number
            lines: Number of lines to capture

        Returns:
            The captured output, or None if pane doesn't exist
        """
        pane = self._get_issue_pane_for_io(issue_number)
        if pane is None:
            return None
        output = pane.capture_pane(start=-lines)
        return "\n".join(output) if output else ""

    def send_keys(self, issue_number: int, keys: str, enter: bool = True) -> None:
        """Send keys to an issue's pane.

        Args:
            issue_number: GitHub issue number
            keys: Keys/text to send
            enter: Whether to press enter after sending
        """
        pane = self._get_issue_pane_for_io(issue_number)
        if pane is None:
            return
        if enter:
            pane.send_keys(keys)
        else:
            pane.send_keys(keys, enter=False)

    def send_keys_by_name(self, session_name: str, keys: str, enter: bool = True) -> bool:
        """Send keys to a pane by its terminal_id.

        Args:
            session_name: Terminal ID / pane title (e.g., 'review-456')
            keys: Keys/text to send
            enter: Whether to press enter after sending

        Returns:
            True if keys were sent, False if pane not found
        """
        pane = self._get_pane_for_io_by_name(session_name)
        if pane is None:
            return False
        if enter:
            pane.send_keys(keys)
        else:
            pane.send_keys(keys, enter=False)
        return True

    def kill_session(self) -> None:
        """Kill the entire orchestrator session.

        Alias for kill_orchestrator_session() for backward compatibility.
        """
        self.kill_orchestrator_session()

    # -------------------------------------------------------------------------
    # TerminalObserver implementation
    # -------------------------------------------------------------------------

    def _get_pane_by_terminal_id(self, terminal_id: str) -> Optional[libtmux.Pane]:
        """Get pane for a terminal_id (adaptive: pane or window mode)."""
        return self._get_pane_for_io_by_name(terminal_id)

    def get_process_state(self, terminal_id: str) -> ProcessState:
        """Get the current state of the process in a terminal.

        Args:
            terminal_id: Window name (e.g., 'issue-123', 'review-456')

        Returns:
            ProcessState indicating whether process is running, exited, signaled, or unknown.
        """
        pane = self._get_pane_by_terminal_id(terminal_id)
        if pane is None:
            return ProcessState.UNKNOWN

        state = TmuxPaneState(pane)
        if not state.is_dead:
            return ProcessState.RUNNING

        # Process is dead - determine how it exited
        if state.signal:
            return ProcessState.SIGNALED
        return ProcessState.EXITED

    def get_exit_info(self, terminal_id: str) -> ProcessExitInfo | None:
        """Get exit information for a terminated process.

        Args:
            terminal_id: Window name (e.g., 'issue-123', 'review-456')

        Returns:
            ProcessExitInfo with exit code/signal details, or None if unavailable.
        """
        pane = self._get_pane_by_terminal_id(terminal_id)
        if pane is None:
            return None

        state = TmuxPaneState(pane)
        if not state.is_dead:
            return None  # Still running, no exit info yet

        return ProcessExitInfo(
            exit_code=state.exit_code,
            signal=state.signal,
            exit_time=datetime.now(),  # Best approximation - actual exit time not available
        )

    def is_process_alive(self, terminal_id: str) -> bool:
        """Quick check if the process is still running.

        Args:
            terminal_id: Window name (e.g., 'issue-123', 'review-456')

        Returns:
            True if process is definitely running, False otherwise.
        """
        return self.get_process_state(terminal_id) == ProcessState.RUNNING

    def capture_full_output(self, terminal_id: str) -> str | None:
        """Capture full scrollback output from a terminal.

        Args:
            terminal_id: Window name (e.g., 'issue-123', 'review-456')

        Returns:
            Full terminal output as string, or None if not available.
        """
        pane = self._get_pane_by_terminal_id(terminal_id)
        if pane is None:
            return None

        try:
            # start="-" means from the beginning of scrollback
            output = pane.capture_pane(start="-")
            return "\n".join(output) if output else ""
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Factory and Global Manager (transitional pattern)
# ---------------------------------------------------------------------------

# Global manager instance (legacy - prefer factory for new code)
_manager: Optional[TmuxManager] = None


def get_manager() -> TmuxManager:
    """Get the global TmuxManager instance.

    Legacy function - prefer create_tmux_manager() for new code.
    """
    global _manager
    if _manager is None:
        _manager = TmuxManager()
    return _manager


def create_tmux_manager(
    session_name: str = SESSION_NAME,
    server: Optional[libtmux.Server] = None,
) -> TmuxManager:
    """Factory function to create a TmuxManager.

    Use this instead of the global get_manager() for better testability
    and session isolation.

    Args:
        session_name: Name for the tmux session.
        server: Optional libtmux Server instance (useful for test isolation).

    Returns:
        New TmuxManager instance.
    """
    return TmuxManager(server=server, session_name=session_name)


def reset_global_manager() -> None:
    """Reset the global manager instance.

    Useful for testing to ensure clean state between tests.
    """
    global _manager
    _manager = None


# Backward-compatible functions (for existing code)

def create_session(session_name: str, command: str, working_dir: Path, title: str | None = None) -> None:
    """Create a window for an issue (backward-compatible wrapper).

    Note: session_name is expected to be "issue-{number}"
    """
    if not session_name.startswith("issue-"):
        raise ValueError(f"Expected session name like 'issue-42', got '{session_name}'")

    issue_number = int(session_name.replace("issue-", ""))
    manager = get_manager()
    manager.create_issue_window(issue_number, command, working_dir, title=title)


def session_exists(session_name: str) -> bool:
    """Check if a window exists for the session name (backward-compatible wrapper).

    Handles issue-N, review-N, rework-N, and triage-N session names.
    """
    import re
    match = re.match(r"(issue|review|rework|triage)-(\d+)", session_name)
    if not match:
        return False

    manager = get_manager()
    # Use the new by_name lookup that handles all session types
    return manager.window_exists_by_name(session_name)


def kill_session(session_name: str) -> None:
    """Kill a window (backward-compatible wrapper)."""
    import re
    match = re.match(r"(issue|review|rework|triage)-(\d+)", session_name)
    if not match:
        return
    manager = get_manager()
    manager.kill_window_by_name(session_name)


def list_sessions() -> list[str]:
    """List all issue session names (backward-compatible wrapper)."""
    manager = get_manager()
    return [f"issue-{num}" for num in manager.list_issue_windows()]


def attach_session(session_name: str) -> None:
    """Attach to a session (replaces current process)."""
    # This still needs to use os.execvp for true attachment
    os.execvp("tmux", ["tmux", "attach-session", "-t", SESSION_NAME])


def send_keys(session_name: str, keys: str, enter: bool = True) -> bool:
    """Send keys to a session's pane (backward-compatible wrapper).

    Handles issue-N, review-N, rework-N, and triage-N session names.

    Returns:
        True if keys were sent, False if session not found.
    """
    import re
    match = re.match(r"(issue|review|rework|triage)-(\d+)", session_name)
    if not match:
        return False
    manager = get_manager()
    return manager.send_keys_by_name(session_name, keys, enter)
