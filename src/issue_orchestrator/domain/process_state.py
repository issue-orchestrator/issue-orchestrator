"""ProcessState - Domain models for process lifecycle observation.

These models represent the observable state of a process within a terminal session,
independent of any specific terminal technology (tmux, iTerm2, etc).

This is a domain concept: "What state is the process in?" not "How do we detect it?"

Usage:
    # Check if a process is still alive
    state = observer.get_process_state(terminal_id)
    if state == ProcessState.RUNNING:
        # Process is still executing
        pass
    elif state in (ProcessState.EXITED, ProcessState.SIGNALED):
        # Process terminated - get details
        exit_info = observer.get_exit_info(terminal_id)
        if exit_info.exit_code != 0:
            # Non-zero exit - failure
            pass
"""

from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class ProcessState(Enum):
    """Observable state of a process.

    This is what we can detect about a process at observation time.
    """

    RUNNING = "running"       # Process is executing
    EXITED = "exited"         # Process exited normally (exit code available)
    SIGNALED = "signaled"     # Process was killed by signal
    UNKNOWN = "unknown"       # State cannot be determined


@dataclass(frozen=True)
class ProcessExitInfo:
    """Exit information for a terminated process.

    Captures details about how and when a process ended.
    This information is only available after a process has exited.

    Attributes:
        exit_code: The numeric exit code (0 = success, non-zero = failure).
                   None if the process was killed by signal or state unknown.
        signal: The signal that killed the process (e.g., "SIGTERM", "SIGKILL").
                None if the process exited normally.
        exit_time: When the exit was detected. None if not available.
    """

    exit_code: int | None = None
    signal: str | None = None
    exit_time: datetime | None = None

    @property
    def success(self) -> bool:
        """True if process exited with code 0."""
        return self.exit_code == 0

    @property
    def was_signaled(self) -> bool:
        """True if process was terminated by a signal."""
        return self.signal is not None

    def __str__(self) -> str:
        """Human-readable description of the exit."""
        if self.signal:
            return f"killed by {self.signal}"
        if self.exit_code is not None:
            return f"exit code {self.exit_code}"
        return "unknown exit"
