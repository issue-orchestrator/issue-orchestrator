"""Hook specifications for the issue-orchestrator plugin system.

This module defines the interfaces (hook specifications) that plugins can implement.
Plugins register implementations using the @hookimpl decorator.

Usage:
    from issue_orchestrator.hookspec import hookimpl

    class MyTerminalPlugin:
        @hookimpl
        def create_session(self, session_id, command, working_dir, title):
            # Implementation
            ...

Entry points are registered in pyproject.toml:
    [project.entry-points."issue_orchestrator.plugins"]
    my_plugin = "my_package:MyPlugin"
"""

import pluggy

# Project name for hook markers
PROJECT_NAME = "issue_orchestrator"

# Create hook specification and implementation markers
hookspec = pluggy.HookspecMarker(PROJECT_NAME)
hookimpl = pluggy.HookimplMarker(PROJECT_NAME)


class TerminalSpec:
    """Hook specifications for terminal/session management.

    Terminal plugins manage the execution environment where AI agents run.
    Examples: tmux windows, iTerm2 tabs, Wezterm panes, Kitty windows.

    All hooks use firstresult=True, meaning the first plugin to return
    a non-None value wins. This allows plugin priority ordering.
    """

    @hookspec(firstresult=True)
    def create_session(
        self,
        session_id: int,
        command: str,
        working_dir: str,
        title: str | None,
    ) -> bool | None:
        """Create a new terminal session for an agent.

        Args:
            session_id: Numeric ID (typically issue number)
            command: Shell command to execute
            working_dir: Working directory path
            title: Optional human-readable title

        Returns:
            True if created, False if failed, None to defer to next plugin.
        """

    @hookspec(firstresult=True)
    def session_exists(self, session_id: int) -> bool | None:
        """Check if a session exists and is running.

        Args:
            session_id: Numeric ID to check

        Returns:
            True if exists, False if not, None to defer to next plugin.
        """

    @hookspec(firstresult=True)
    def kill_session(self, session_id: int) -> bool | None:
        """Kill/close a terminal session.

        Args:
            session_id: Numeric ID to kill

        Returns:
            True if killed, False if not found, None to defer to next plugin.
        """

    @hookspec(firstresult=True)
    def discover_running_sessions(self) -> list[dict] | None:
        """Discover sessions that survived an orchestrator restart.

        Returns:
            List of dicts with {issue_number, tab_name, is_review},
            or None to defer to next plugin.
        """

    @hookspec(firstresult=True)
    def cleanup_idle_sessions(self) -> int | None:
        """Clean up sessions where the agent has exited.

        Returns:
            Number of sessions cleaned up, or None to defer.
        """

    @hookspec(firstresult=True)
    def get_session_output(self, session_id: int, lines: int) -> str | None:
        """Get recent output from a session.

        Args:
            session_id: Numeric ID
            lines: Number of lines to retrieve

        Returns:
            Terminal output string, or None if not available/supported.
        """


# Future hook specs can be added here:
#
# class AISpec:
#     """Hook specifications for AI/LLM backends."""
#
#     @hookspec(firstresult=True)
#     def build_command(self, model, prompt_path, initial_prompt, permission_mode): ...
#
#
# class IssueTrackerSpec:
#     """Hook specifications for issue tracking systems."""
#
#     @hookspec(firstresult=True)
#     def list_issues(self, labels, milestone): ...
