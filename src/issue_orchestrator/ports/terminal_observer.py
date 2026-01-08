"""Terminal observer port for process lifecycle monitoring.

This port defines the interface for observing process state within terminal sessions.
Unlike SessionRunner which manages sessions (create/kill), TerminalObserver
gathers facts about the process running within a session.

Key insight: A session can exist (window is open) while the process is dead
(agent crashed, exited, or was killed). This port detects the difference.

Following hexagonal architecture:
- This is a port (interface)
- Adapters (tmux, etc.) implement it
- Control layer uses it to observe, never to act

Usage:
    from issue_orchestrator.domain import ProcessState

    # Check if the agent process is still running
    state = observer.get_process_state(terminal_id)
    if state == ProcessState.EXITED:
        exit_info = observer.get_exit_info(terminal_id)
        if exit_info and exit_info.exit_code != 0:
            output = observer.capture_full_output(terminal_id)
            # Log failure details for debugging
"""

from typing import Protocol

from issue_orchestrator.domain import ProcessState, ProcessExitInfo


class TerminalObserver(Protocol):
    """Port for observing process state within terminal sessions.

    Implementations may use tmux's pane_dead attribute, process inspection,
    or other terminal-specific mechanisms. The observer pattern ensures
    we gather facts without taking action.
    """

    def get_process_state(self, terminal_id: str) -> ProcessState:
        """Get the current state of the process in a terminal.

        Args:
            terminal_id: Opaque terminal identifier (adapter interprets this)

        Returns:
            ProcessState indicating whether process is running, exited, signaled, or unknown.
            Returns UNKNOWN if terminal doesn't exist or state cannot be determined.
        """
        ...

    def get_exit_info(self, terminal_id: str) -> ProcessExitInfo | None:
        """Get exit information for a terminated process.

        Only meaningful if process state is EXITED or SIGNALED.

        Args:
            terminal_id: Opaque terminal identifier

        Returns:
            ProcessExitInfo with exit code/signal details, or None if:
            - Terminal doesn't exist
            - Process is still running
            - Exit info is not available
        """
        ...

    def is_process_alive(self, terminal_id: str) -> bool:
        """Quick check if the process is still running.

        Convenience method equivalent to:
            get_process_state(terminal_id) == ProcessState.RUNNING

        Args:
            terminal_id: Opaque terminal identifier

        Returns:
            True if process is definitely running, False otherwise.
        """
        ...

    def capture_full_output(self, terminal_id: str) -> str | None:
        """Capture full scrollback output from a terminal.

        Useful for debugging failures - captures all output, not just recent lines.

        Args:
            terminal_id: Opaque terminal identifier

        Returns:
            Full terminal output as string, or None if not available.
        """
        ...


class NullTerminalObserver:
    """No-op terminal observer for testing.

    Always returns UNKNOWN state - no actual observation.
    """

    def get_process_state(self, terminal_id: str) -> ProcessState:
        return ProcessState.UNKNOWN

    def get_exit_info(self, terminal_id: str) -> ProcessExitInfo | None:
        return None

    def is_process_alive(self, terminal_id: str) -> bool:
        return False

    def capture_full_output(self, terminal_id: str) -> str | None:
        return None
