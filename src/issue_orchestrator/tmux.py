import os
import subprocess
from pathlib import Path


def _run_tmux(args: list[str], check: bool = True) -> subprocess.CompletedProcess:
    """Run a tmux command and return the result."""
    return subprocess.run(
        ["tmux"] + args, capture_output=True, text=True, check=check
    )


def create_session(session_name: str, command: str, working_dir: Path) -> None:
    """Create a new detached tmux session running a command.

    Args:
        session_name: Name of the tmux session to create
        command: Command to run in the session
        working_dir: Working directory for the session

    Raises:
        subprocess.CalledProcessError: If the tmux command fails
    """
    _run_tmux(
        [
            "new-session",
            "-d",
            "-s",
            session_name,
            "-c",
            str(working_dir),
            command,
        ]
    )


def session_exists(session_name: str) -> bool:
    """Check if a tmux session is still running.

    Args:
        session_name: Name of the session to check

    Returns:
        True if the session exists, False otherwise
    """
    result = _run_tmux(["has-session", "-t", session_name], check=False)
    return result.returncode == 0


def kill_session(session_name: str) -> None:
    """Kill a tmux session.

    Args:
        session_name: Name of the session to kill

    Raises:
        subprocess.CalledProcessError: If the tmux command fails
    """
    _run_tmux(["kill-session", "-t", session_name])


def list_sessions() -> list[str]:
    """List all tmux session names.

    Returns:
        List of session names currently running
    """
    result = _run_tmux(["list-sessions", "-F", "#{session_name}"], check=False)
    if result.returncode != 0:
        # No sessions exist
        return []
    return [line.strip() for line in result.stdout.strip().split("\n") if line.strip()]


def attach_session(session_name: str) -> None:
    """Attach to a tmux session, replacing the current process.

    Args:
        session_name: Name of the session to attach to

    Note:
        This function replaces the current process and does not return
    """
    os.execvp("tmux", ["tmux", "attach-session", "-t", session_name])
