"""iTerm2 integration via AppleScript for Mac GUI mode."""

import subprocess
import logging
import os

logger = logging.getLogger(__name__)


def is_iterm2_available() -> bool:
    """Check if iTerm2 is installed and we're on macOS."""
    if os.uname().sysname != "Darwin":
        return False
    result = subprocess.run(
        ["osascript", "-e", 'tell application "System Events" to (name of processes) contains "iTerm2"'],
        capture_output=True, text=True
    )
    return "true" in result.stdout.lower()


def is_running_in_iterm2() -> bool:
    """Check if we're currently running inside iTerm2."""
    return os.environ.get("TERM_PROGRAM") == "iTerm.app"


def run_applescript(script: str) -> tuple[bool, str]:
    """Run an AppleScript and return (success, output)."""
    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True, text=True
    )
    if result.returncode != 0:
        logger.error("AppleScript failed: %s", result.stderr)
        return False, result.stderr
    return True, result.stdout.strip()


def select_tab_by_name(tab_name: str) -> bool:
    """Switch to an iTerm2 tab by its name (partial match)."""
    script = f'''
    tell application "iTerm2"
        activate
        tell current window
            repeat with t in tabs
                tell current session of t
                    if name contains "{tab_name}" then
                        select t
                        return true
                    end if
                end tell
            end repeat
        end tell
    end tell
    return false
    '''
    success, output = run_applescript(script)
    return success and "true" in output.lower()


def select_tab_by_index(index: int) -> bool:
    """Switch to an iTerm2 tab by index (1-based)."""
    script = f'''
    tell application "iTerm2"
        activate
        tell current window
            if (count of tabs) >= {index} then
                select tab {index}
                return true
            end if
        end tell
    end tell
    return false
    '''
    success, output = run_applescript(script)
    return success and "true" in output.lower()


def get_tab_count() -> int:
    """Get the number of tabs in the current iTerm2 window."""
    script = '''
    tell application "iTerm2"
        tell current window
            return count of tabs
        end tell
    end tell
    '''
    success, output = run_applescript(script)
    if success:
        try:
            return int(output)
        except ValueError:
            pass
    return 0


def split_pane_vertical() -> bool:
    """Create a vertical split in the current iTerm2 session."""
    script = '''
    tell application "iTerm2"
        tell current session of current window
            split vertically with default profile
        end tell
    end tell
    '''
    success, _ = run_applescript(script)
    return success


def split_pane_horizontal() -> bool:
    """Create a horizontal split in the current iTerm2 session."""
    script = '''
    tell application "iTerm2"
        tell current session of current window
            split horizontally with default profile
        end tell
    end tell
    '''
    success, _ = run_applescript(script)
    return success


def send_text_to_session(text: str, new_line: bool = True) -> bool:
    """Send text to the current iTerm2 session."""
    # Escape double quotes in the text
    escaped = text.replace('"', '\\"')
    newline_flag = "true" if new_line else "false"
    script = f'''
    tell application "iTerm2"
        tell current session of current window
            write text "{escaped}" newline {newline_flag}
        end tell
    end tell
    '''
    success, _ = run_applescript(script)
    return success


def create_new_tab_with_command(command: str, name: str | None = None) -> bool:
    """Create a new iTerm2 tab and run a command in it."""
    escaped_cmd = command.replace('"', '\\"')
    name_script = f'set name to "{name}"' if name else ""

    script = f'''
    tell application "iTerm2"
        tell current window
            set newTab to (create tab with default profile)
            tell current session of newTab
                {name_script}
                write text "{escaped_cmd}"
            end tell
        end tell
    end tell
    '''
    success, _ = run_applescript(script)
    return success


def attach_to_tmux_cc(session_name: str = "orchestrator") -> bool:
    """Attach to a tmux session in control mode (-CC) for iTerm2 integration.

    This makes iTerm2 treat tmux windows as native tabs.
    """
    return send_text_to_session(f"tmux -CC attach -t {session_name}")


def start_orchestrator_iterm2_mode() -> None:
    """Start the orchestrator with iTerm2's tmux control mode.

    This should be called instead of the normal start when ui_mode=iterm2.
    """
    # First check if tmux session exists
    result = subprocess.run(
        ["tmux", "has-session", "-t", "orchestrator"],
        capture_output=True
    )

    if result.returncode == 0:
        # Session exists, attach with control mode
        logger.info("Attaching to existing orchestrator session with iTerm2 control mode")
        os.execvp("tmux", ["tmux", "-CC", "attach", "-t", "orchestrator"])
    else:
        # No session - start normally, the orchestrator will create it
        logger.info("No existing session, starting orchestrator normally")
        # The dashboard will create the tmux session, then we can attach
        pass
