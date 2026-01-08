"""Tmux session management using libtmux.

Uses a session-per-orchestrator architecture:
- Each orchestrator instance owns one tmux session
- Each agent session (code, review, rework, triage) gets its own window
- Atomic cleanup: kill_session() removes ALL windows at once
- No global state - TmuxManager instances are created via factory
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
from libtmux.exc import LibTmuxException

from issue_orchestrator.domain import ProcessState, ProcessExitInfo

from ._tmux_retry import tmux_retry

logger = logging.getLogger(__name__)

# Constants
# Allow override via environment variable for e2e test isolation (legacy)
SESSION_NAME = os.environ.get("ORCHESTRATOR_TMUX_SESSION", "orchestrator")
DASHBOARD_WINDOW = "dashboard"

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
        """Get the orchestrator session if it exists."""
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
    def create_orchestrator_session(self) -> libtmux.Session:
        """Create the tmux session for this orchestrator, or reuse existing."""
        if self._session is not None:
            return self._session

        # Try to find existing session first
        existing = self.server.sessions.get(session_name=self._session_name)
        if existing:
            self._session = existing
            return self._session

        # Create new session
        self._session = self.server.new_session(
            session_name=self._session_name,
            window_name=DASHBOARD_WINDOW,
        )
        return self._session

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
    ) -> libtmux.Window:
        """Create a new window for an issue and run the command."""
        session = self.ensure_session()

        # Build window name
        if title:
            short_title = title[:20].replace(" ", "-").replace(":", "")
            window_name = f"#{issue_number}-{short_title}"
        else:
            window_name = f"issue-{issue_number}"

        # Check if window already exists
        existing = session.windows.filter(window_name=window_name)
        if existing:
            raise ValueError(f"Window {window_name} already exists")

        # Create window
        window = session.new_window(
            window_name=window_name,
            start_directory=str(working_dir),
        )

        # Enable remain-on-exit for exit code capture
        try:
            window.set_option("remain-on-exit", "on")
        except Exception:
            pass

        # Set up isolated environment and run command
        from ...control.isolation import build_isolation_prefix
        wrapper_dir = Path(__file__).parent.parent.parent / "scripts"
        isolation_prefix = build_isolation_prefix(
            worktree=working_dir,
            isolation_mode="standard",
            scrub_env=True,
            isolate_home=False,
        )
        setup_cmd = f'export PATH="{wrapper_dir}:$PATH" && {isolation_prefix}'

        pane = window.active_pane
        if pane is not None:
            # Enable logging
            try:
                log_dir = working_dir / ".issue-orchestrator"
                log_dir.mkdir(exist_ok=True)
                pane.cmd("pipe-pane", f"cat >> '{log_dir / 'session.log'}'")
            except OSError:
                pass

            pane.send_keys(setup_cmd)
            pane.send_keys(command)

        return window

    def _find_issue_window(self, issue_number: int) -> Optional[libtmux.Window]:
        """Find window for an issue by number (handles both naming conventions)."""
        if self.session is None:
            return None
        try:
            # Check new format: #{number}-{title}
            for window in self.session.windows:
                if window.name and window.name.startswith(f"#{issue_number}-"):
                    return window
            # Check old format: issue-{number}
            windows = self.session.windows.filter(window_name=f"issue-{issue_number}")
            return windows[0] if windows else None
        except LibTmuxException as e:
            logger.warning("[TMUX] _find_issue_window(%d) failed: %s", issue_number, str(e).strip())
            return None

    def _find_window_by_name(self, session_name: str) -> Optional[libtmux.Window]:
        """Find window by its full session name (issue-N, review-N, rework-N, etc)."""
        if self.session is None:
            return None
        try:
            windows = self.session.windows.filter(window_name=session_name)
            return windows[0] if windows else None
        except LibTmuxException as e:
            logger.warning("[TMUX] _find_window_by_name(%s) failed: %s", session_name, str(e).strip())
            return None

    def window_exists(self, issue_number: int) -> bool:
        """Check if a window exists for the given issue."""
        return self._find_issue_window(issue_number) is not None

    def window_exists_by_name(self, session_name: str) -> bool:
        """Check if a window exists by its full name (e.g., 'review-456')."""
        return self._find_window_by_name(session_name) is not None

    def get_window(self, issue_number: int) -> Optional[libtmux.Window]:
        """Get the window for an issue, or None if it doesn't exist."""
        return self._find_issue_window(issue_number)

    def kill_window(self, issue_number: int) -> None:
        """Kill the window for an issue."""
        window = self.get_window(issue_number)
        if window:
            self._stop_pipe(window)
            window.kill()

    def kill_window_by_name(self, session_name: str) -> None:
        """Kill the window by its full name."""
        window = self._find_window_by_name(session_name)
        if window:
            self._stop_pipe(window)
            window.kill()

    def select_window(self, issue_number: int) -> bool:
        """Switch to the window for an issue and open terminal if needed.

        On macOS, opens Terminal.app with tmux attached if no client is connected.

        Returns:
            True if window was selected, False if it doesn't exist
        """
        window = self.get_window(issue_number)
        if window:
            window.select()
            # Open terminal if no client is attached
            self._ensure_terminal_attached()
            return True
        return False

    def _ensure_terminal_attached(self) -> None:
        """Ensure a terminal is attached to the tmux session.

        On macOS, opens Terminal.app if no client is connected.
        """
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
        """List all issue numbers that have active windows."""
        if self.session is None:
            return []
        issue_numbers = []
        for window in self.session.windows:
            if not window.name:
                continue
            # New format: #{number}-{title}
            if window.name.startswith("#"):
                try:
                    num = int(window.name.split("-")[0][1:])  # Extract number after #
                    issue_numbers.append(num)
                except (ValueError, IndexError):
                    pass
            # Old format: issue-{number}
            elif window.name.startswith("issue-"):
                try:
                    num = int(window.name.replace("issue-", ""))
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

    def send_keys(self, issue_number: int, keys: str, enter: bool = True) -> None:
        """Send keys to an issue's pane.

        Args:
            issue_number: GitHub issue number
            keys: Keys/text to send
            enter: Whether to press enter after sending
        """
        window = self.get_window(issue_number)
        if window is None:
            return
        pane = window.active_pane
        if pane is None:
            return
        if enter:
            pane.send_keys(keys)
        else:
            pane.send_keys(keys, enter=False)

    def send_keys_by_name(self, session_name: str, keys: str, enter: bool = True) -> bool:
        """Send keys to a window by its full name.

        Args:
            session_name: Full window name (e.g., 'review-456')
            keys: Keys/text to send
            enter: Whether to press enter after sending

        Returns:
            True if keys were sent, False if window not found
        """
        window = self._find_window_by_name(session_name)
        if window is None:
            return False
        pane = window.active_pane
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
        """Get pane for a terminal_id (window name)."""
        window = self._find_window_by_name(terminal_id)
        if window is None:
            return None
        return window.active_pane

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
