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
        ["osascript", "-e", 'tell application "System Events" to (name of processes) contains "iTerm"'],
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
    tell application "iTerm"
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
    tell application "iTerm"
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
    tell application "iTerm"
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
    tell application "iTerm"
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
    tell application "iTerm"
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
    tell application "iTerm"
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
    tell application "iTerm"
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


class ITermSessionManager:
    """Manages agent sessions as native iTerm2 tabs."""

    def __init__(self):
        self._sessions: dict[int, dict] = {}  # issue_number -> session info

    def create_session(
        self,
        issue_number: int,
        command: str,
        working_dir: str,
        title: str | None = None,
    ) -> bool:
        """Create a new iTerm2 tab for an issue.

        Args:
            issue_number: GitHub issue number
            command: Command to run
            working_dir: Working directory
            title: Optional title for the tab

        Returns:
            True if tab was created successfully
        """
        tab_name = f"#{issue_number}"
        if title:
            short_title = title[:20].replace('"', "'")
            tab_name = f"#{issue_number} {short_title}"

        # Escape the command for AppleScript
        escaped_cmd = command.replace('\\', '\\\\').replace('"', '\\"')
        escaped_dir = working_dir.replace('\\', '\\\\').replace('"', '\\"')

        script = f'''tell application "iTerm"
tell current window
set newTab to (create tab with default profile)
tell current session of newTab
set name to "{tab_name}"
write text "cd \\"{escaped_dir}\\" && {escaped_cmd}"
end tell
end tell
end tell'''

        success, output = run_applescript(script)
        if success:
            logger.info("Created iTerm2 tab for issue #%d", issue_number)
            self._sessions[issue_number] = {
                "tab_name": tab_name,
                "created_at": subprocess.run(["date", "+%s"], capture_output=True, text=True).stdout.strip(),
            }
            return True
        else:
            logger.error("Failed to create iTerm2 tab for issue #%d: %s", issue_number, output)
            return False

    def session_exists(self, issue_number: int) -> bool:
        """Check if a session exists for the issue."""
        if issue_number not in self._sessions:
            return False

        # Verify the tab still exists by checking for it
        tab_name = self._sessions[issue_number]["tab_name"]
        script = f'''
        tell application "iTerm"
            tell current window
                repeat with t in tabs
                    tell current session of t
                        if name contains "#{issue_number}" then
                            return true
                        end if
                    end tell
                end repeat
            end tell
        end tell
        return false
        '''
        success, output = run_applescript(script)
        exists = success and "true" in output.lower()

        if not exists:
            # Clean up our tracking
            del self._sessions[issue_number]

        return exists

    def kill_session(self, issue_number: int) -> bool:
        """Close the tab for an issue."""
        script = f'''
        tell application "iTerm"
            tell current window
                repeat with t in tabs
                    tell current session of t
                        if name contains "#{issue_number}" then
                            close t
                            return true
                        end if
                    end tell
                end repeat
            end tell
        end tell
        return false
        '''
        success, output = run_applescript(script)
        if issue_number in self._sessions:
            del self._sessions[issue_number]
        return success and "true" in output.lower()

    def select_session(self, issue_number: int) -> bool:
        """Switch to the tab for an issue."""
        return select_tab_by_name(f"#{issue_number}")

    def list_sessions(self) -> list[int]:
        """List all tracked issue numbers."""
        # Verify each session still exists
        valid = []
        for issue_number in list(self._sessions.keys()):
            if self.session_exists(issue_number):
                valid.append(issue_number)
        return valid

    def get_session_count(self) -> int:
        """Get count of active sessions."""
        return len(self.list_sessions())


# Global iTerm2 session manager
_iterm_manager: ITermSessionManager | None = None


def get_iterm_manager() -> ITermSessionManager:
    """Get the global ITermSessionManager instance."""
    global _iterm_manager
    if _iterm_manager is None:
        _iterm_manager = ITermSessionManager()
    return _iterm_manager
