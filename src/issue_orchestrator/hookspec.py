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


class LifecycleSpec:
    """Hook specifications for orchestrator lifecycle events.

    Lifecycle hooks enable plugins to react to state changes in the orchestrator.
    Unlike TerminalSpec (firstresult=True), these hooks broadcast to ALL plugins
    so any UI (web, CLI, desktop, Slack) can receive notifications.

    All hooks are fire-and-forget - return values are ignored.
    """

    @hookspec
    def on_issue_claimed(
        self,
        issue_number: int,
        title: str,
        agent_type: str,
    ) -> None:
        """Called when an issue is claimed by an agent.

        Args:
            issue_number: The GitHub issue number
            title: Issue title
            agent_type: The agent type handling this issue (e.g., "agent:web")
        """

    @hookspec
    def on_session_started(
        self,
        issue_number: int,
        session_id: str,
        worktree_path: str,
        branch_name: str,
    ) -> None:
        """Called when an agent session starts running.

        Args:
            issue_number: The GitHub issue number
            session_id: Unique session identifier
            worktree_path: Path to the git worktree
            branch_name: Git branch name for this work
        """

    @hookspec
    def on_session_completed(
        self,
        issue_number: int,
        session_id: str,
        pr_url: str | None,
        runtime_minutes: float | None,
    ) -> None:
        """Called when an agent session completes successfully.

        Args:
            issue_number: The GitHub issue number
            session_id: Unique session identifier
            pr_url: URL of the created PR, if any
            runtime_minutes: How long the session ran
        """

    @hookspec
    def on_session_failed(
        self,
        issue_number: int,
        session_id: str,
        error: str | None,
        runtime_minutes: float | None,
    ) -> None:
        """Called when an agent session fails or times out.

        Args:
            issue_number: The GitHub issue number
            session_id: Unique session identifier
            error: Error message or reason for failure
            runtime_minutes: How long the session ran before failing
        """

    @hookspec
    def on_issue_blocked(
        self,
        issue_number: int,
        reason: str | None,
    ) -> None:
        """Called when an issue becomes blocked.

        Args:
            issue_number: The GitHub issue number
            reason: Why the issue is blocked
        """

    @hookspec
    def on_issue_needs_human(
        self,
        issue_number: int,
        reason: str | None,
    ) -> None:
        """Called when an issue needs human intervention.

        Args:
            issue_number: The GitHub issue number
            reason: What human help is needed
        """

    @hookspec
    def on_pr_created(
        self,
        issue_number: int,
        pr_number: int,
        pr_url: str,
        title: str,
    ) -> None:
        """Called when a pull request is created.

        Args:
            issue_number: The associated issue number
            pr_number: The PR number
            pr_url: URL to the PR
            title: PR title
        """

    @hookspec
    def on_review_requested(
        self,
        pr_number: int,
        issue_number: int,
        review_type: str,
    ) -> None:
        """Called when a review is requested for a PR.

        Args:
            pr_number: The PR number
            issue_number: The associated issue number
            review_type: Type of review ("code_review", "cto_review")
        """

    @hookspec
    def on_review_completed(
        self,
        pr_number: int,
        issue_number: int,
        result: str,
        rework_count: int,
    ) -> None:
        """Called when a review is completed.

        Args:
            pr_number: The PR number
            issue_number: The associated issue number
            result: Review result ("approved", "changes_requested", "merged")
            rework_count: Number of rework cycles so far
        """

    @hookspec
    def on_review_escalated(
        self,
        pr_number: int,
        issue_number: int,
        rework_count: int,
        max_rework_cycles: int,
    ) -> None:
        """Called when a review is escalated due to exceeding rework limits.

        This is a critical event indicating that the bounded review loop has
        failed and human intervention is required. The PR cannot proceed through
        the normal automation path.

        Args:
            pr_number: The PR number
            issue_number: The associated issue number
            rework_count: Number of rework cycles attempted
            max_rework_cycles: The configured maximum allowed
        """

    @hookspec
    def on_orchestrator_state_changed(
        self,
        active_count: int,
        paused: bool,
        completed_today: int,
    ) -> None:
        """Called when orchestrator state changes significantly.

        Args:
            active_count: Number of active sessions
            paused: Whether orchestrator is paused
            completed_today: Number of completions today
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
