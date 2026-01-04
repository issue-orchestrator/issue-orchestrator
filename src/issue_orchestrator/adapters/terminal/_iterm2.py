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
        # Close any idle zombie tabs for this issue
        self._close_existing_tabs_for_issue(issue_number)

        # Check if there's still a RUNNING session (Claude active) - if so, don't create duplicate
        if self._has_running_tab_for_issue(issue_number):
            logger.warning("Skipping session creation for #%d - active Claude session already exists in iTerm2", issue_number)
            return False

        tab_name = f"#{issue_number}"
        if title:
            short_title = title[:20].replace('"', "'")
            tab_name = f"#{issue_number} {short_title}"

        # Escape the command for AppleScript
        escaped_cmd = command.replace('\\', '\\\\').replace('"', '\\"')
        escaped_dir = working_dir.replace('\\', '\\\\').replace('"', '\\"')

        # Use escape sequence to set tab name (set name to doesn't work reliably)
        escaped_tab_name = tab_name.replace('"', '\\"')
        # Wrap command in zsh -l -c to ensure proper PATH (iTerm may default to bash)
        # The -l flag sources login profile (~/.zshrc) so tools like claude are in PATH
        # IMPORTANT: Escape single quotes in the command before wrapping in single quotes
        # In shell, 'foo'\''bar' produces foo'bar (end quote, escaped quote, start quote)
        cmd_with_escaped_quotes = command.replace("'", "'\\''")

        # Prepend gh-wrapper directory to PATH to intercept unauthorized gh pr create
        # This blocks agents from bypassing agent-done
        from pathlib import Path
        from ...control.isolation import build_isolation_prefix
        wrapper_dir = Path(__file__).parent / "scripts"
        path_prefix = f'export PATH="{wrapper_dir}:$PATH" && '

        # Add isolation: scrub credentials, isolate HOME to worktree
        isolation_prefix = build_isolation_prefix(
            worktree=Path(working_dir),
            isolation_mode="standard",
            scrub_env=True,
            isolate_home=True,
        )

        # Add sandbox verification before running agent command
        # This confirms isolation is working (gh auth fails, git push fails, etc.)
        # Use verify-agent-sandbox if available, otherwise fall back to Python module
        sandbox_check = (
            "if command -v verify-agent-sandbox &> /dev/null; then "
            "verify-agent-sandbox || { echo 'Sandbox verification failed - aborting'; exit 1; }; "
            "elif python3 -m issue_orchestrator.execution.sandbox_verify 2>/dev/null; then :; "
            "elif [ $? -eq 1 ]; then echo 'Sandbox verification failed - aborting'; exit 1; fi && "
        )

        zsh_wrapped_cmd = f"zsh -l -c '{path_prefix}{isolation_prefix}{sandbox_check}cd \"{working_dir}\" && {cmd_with_escaped_quotes}'"
        escaped_zsh_cmd = zsh_wrapped_cmd.replace('\\', '\\\\').replace('"', '\\"')
        # AppleScript that creates a window if none exists, then creates a tab
        script = f'''tell application "iTerm"
-- Ensure iTerm2 has at least one window
if (count of windows) = 0 then
    create window with default profile
end if
tell current window
set newTab to (create tab with default profile)
tell current session of newTab
write text "printf \\"\\\\033]0;{escaped_tab_name}\\\\007\\""
write text "{escaped_zsh_cmd}"
end tell
end tell
end tell'''

        # Log the command being executed for debugging
        logger.info("Creating iTerm2 tab for issue #%d with command: %s", issue_number, command[:100] + "..." if len(command) > 100 else command)
        logger.debug("Full zsh-wrapped command: %s", zsh_wrapped_cmd)

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
        """Check if a session exists for the issue AND is still running a command.

        Returns False if the tab doesn't exist OR if it's at a shell prompt
        (meaning Claude has exited).

        NOTE: This checks iTerm directly, not just in-memory tracking.
        This is critical for detecting sessions that survived orchestrator restart.
        """
        # Always check iTerm directly - don't rely on in-memory _sessions dict
        # which is lost on orchestrator restart
        has_running = self._has_running_tab_for_issue(issue_number)

        # Update in-memory tracking to match reality
        if not has_running and issue_number in self._sessions:
            del self._sessions[issue_number]

        return has_running

    def _has_running_tab_for_issue(self, issue_number: int) -> bool:
        """Check if there's a running (Claude active) tab for this issue."""
        script = f'''
        tell application "iTerm"
            repeat with w in windows
                repeat with t in tabs of w
                    tell current session of t
                        if name starts with "#{issue_number} " or name is "#{issue_number}" then
                            if is processing is true then
                                return true
                            end if
                        end if
                    end tell
                end repeat
            end repeat
            return false
        end tell
        '''
        success, output = run_applescript(script)
        return success and "true" in output.lower()

    def _close_existing_tabs_for_issue(self, issue_number: int) -> int:
        """Close IDLE existing tabs for an issue number (prevents zombie duplicates).

        Only closes tabs where Claude has exited (is processing = false).
        Running sessions are left alone to avoid killing useful work.

        Returns:
            Number of tabs closed
        """
        # AppleScript to close only IDLE tabs with this issue number
        script = f'''
        tell application "iTerm"
            set closedCount to 0
            repeat with w in windows
                set tabsToClose to {{}}
                repeat with t in tabs of w
                    tell current session of t
                        if name starts with "#{issue_number} " or name is "#{issue_number}" then
                            -- Only close if idle (Claude has exited)
                            if is processing is false then
                                set end of tabsToClose to t
                            end if
                        end if
                    end tell
                end repeat
                -- Close tabs in reverse order to avoid index shifting issues
                repeat with i from (count of tabsToClose) to 1 by -1
                    close item i of tabsToClose
                    set closedCount to closedCount + 1
                end repeat
            end repeat
            return closedCount
        end tell
        '''
        success, output = run_applescript(script)
        if success:
            try:
                closed = int(output.strip())
                if closed > 0:
                    logger.info("Closed %d idle zombie tab(s) for issue #%d before creating new session", closed, issue_number)
                return closed
            except ValueError:
                return 0
        return 0

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

    def send_to_session(self, issue_number: int, text: str) -> bool:
        """Send text to a specific session by issue number.

        Args:
            issue_number: The issue number identifying the session
            text: Text to send (will add newline)

        Returns:
            True if text was sent successfully
        """
        escaped_text = text.replace('"', '\\"')
        # Use "tell application id" to avoid activating iTerm and stealing focus
        script = f'''
        tell application id "com.googlecode.iterm2"
            tell current window
                repeat with t in tabs
                    tell current session of t
                        if name contains "#{issue_number}" then
                            write text "{escaped_text}"
                            return true
                        end if
                    end tell
                end repeat
            end tell
        end tell
        return false
        '''
        success, output = run_applescript(script)
        if success and "true" in output.lower():
            logger.info("Sent text to session #%d", issue_number)
            return True
        return False

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


def discover_issue_tabs() -> list[int]:
    """Discover existing iTerm2 tabs with issue-related names.

    Scans all iTerm2 tabs across all windows for tabs whose names start with '#'
    followed by a number (our naming convention for issue tabs).

    Returns:
        List of issue numbers found in tab names
    """
    import re

    script = '''
    tell application "iTerm"
        set tabNames to {}
        repeat with w in windows
            repeat with t in tabs of w
                tell current session of t
                    set end of tabNames to name
                end tell
            end repeat
        end repeat
        return tabNames
    end tell
    '''
    success, output = run_applescript(script)
    if not success:
        logger.warning("Failed to enumerate iTerm2 tabs: %s", output)
        return []

    # Parse output - AppleScript returns comma-separated list
    # Look for patterns like "#123" or "#123 Some title"
    issue_numbers = []
    pattern = re.compile(r'#(\d+)')

    for name in output.split(','):
        name = name.strip()
        match = pattern.match(name)
        if match:
            issue_numbers.append(int(match.group(1)))

    logger.info("Discovered %d issue tabs in iTerm2: %s", len(issue_numbers), issue_numbers)
    return issue_numbers


def discover_running_sessions() -> list[dict]:
    """Discover iTerm2 tabs that are actively running Claude (is processing = true).

    Returns:
        List of dicts with {issue_number, tab_name, is_review} for each running session
    """
    import re

    script = '''
    tell application "iTerm"
        set results to {}
        repeat with w in windows
            repeat with t in tabs of w
                tell current session of t
                    if is processing is true then
                        set tabName to name
                        if tabName starts with "#" then
                            set end of results to tabName
                        end if
                    end if
                end tell
            end repeat
        end repeat
        return results
    end tell
    '''
    success, output = run_applescript(script)
    if not success:
        logger.warning("Failed to discover running sessions: %s", output)
        return []

    sessions = []
    pattern = re.compile(r'#(\d+)')

    for name in output.split(','):
        name = name.strip()
        match = pattern.match(name)
        if match:
            issue_num = int(match.group(1))
            is_review = "Review PR" in name or name.startswith("#") and "review" in name.lower()
            sessions.append({
                "issue_number": issue_num,
                "tab_name": name,
                "is_review": is_review,
            })

    logger.info("Discovered %d running sessions in iTerm2: %s",
                len(sessions), [s["issue_number"] for s in sessions])
    return sessions


def cleanup_idle_tabs() -> int:
    """Close all iTerm2 tabs that are at a shell prompt (idle).

    This identifies tabs whose names start with '#' (our convention) and are
    at a shell prompt (meaning Claude has exited), then closes them.

    Returns:
        Number of tabs closed
    """
    # AppleScript to find and close idle issue tabs
    # A tab is idle if is_processing is false (at shell prompt)
    script = '''
    tell application "iTerm"
        set closedCount to 0
        repeat with w in windows
            set tabsToClose to {}
            repeat with t in tabs of w
                tell current session of t
                    if name starts with "#" then
                        if is processing is false then
                            set end of tabsToClose to t
                        end if
                    end if
                end tell
            end repeat
            -- Close tabs in reverse order to avoid index shifting issues
            repeat with i from (count of tabsToClose) to 1 by -1
                close item i of tabsToClose
                set closedCount to closedCount + 1
            end repeat
        end repeat
        return closedCount
    end tell
    '''
    success, output = run_applescript(script)
    if success:
        try:
            closed = int(output.strip())
            logger.info("Closed %d idle iTerm2 tabs", closed)
            return closed
        except ValueError:
            logger.warning("Unexpected output from cleanup script: %s", output)
            return 0
    else:
        logger.warning("Failed to cleanup idle tabs: %s", output)
        return 0
