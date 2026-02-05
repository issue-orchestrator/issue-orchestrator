"""Execution layer - adapters that talk to external systems.

This package contains adapters that execute actions against external systems.
These are the "Adapters" in the architecture.

Architecture principle:
- Components that OBSERVE are named Observers (observation/)
- Components that DECIDE are named Controllers (control/)
- Components that ACT are named Adapters (execution/)

The execution layer:
- Talks to external systems (GitHub, git, terminals)
- Executes actions requested by the control plane
- Does NOT make policy decisions
- Returns facts/results to the caller

Includes a plugin architecture using pluggy for external integrations:
- Terminal plugins (tmux, etc.)
- Platform adapters (GitHub, GitLab, etc.)
- State storage plugins (JSON, SQLite, etc.)

Usage:
    from issue_orchestrator.execution import PluginManager, GitHubAdapter

    # Plugin manager for terminal operations
    pm = PluginManager(terminal_plugin="tmux")
    pm.create_session(42, "claude ...", "/path/to/worktree", "Issue title")

    # Direct adapter usage
    adapter = GitHubAdapter("owner/repo")
    issues = adapter.list_issues(labels=["bug"])
"""

from .manager import PluginManager, create_plugin_manager, BUILTIN_PLUGINS
from .terminal_subprocess import SubprocessPlugin
from ..adapters.github.github_adapter import GitHubAdapter
from ..adapters.github.github_issue import GitHubIssue
from .git_working_copy import GitWorkingCopy
from .json_store import JsonSessionStore
from .lifecycle_sse import LifecycleSSEPlugin
from .event_sink_adapter import PluggyEventSink, CompositeEventSink, LoggingEventSink
from .session_runner_adapter import PluggySessionRunner
from ..adapters.github.issue_resolver import GitHubIssueResolver
from .command_runner import LocalCommandRunner
from .session_output_adapter import FileSystemSessionOutput
from .goal_pilot_store import SqliteGoalPilotStore
from .provider_circuit_store import SQLiteProviderCircuitStore

__all__ = [
    # Main interface (internal, used by composition root)
    "PluginManager",
    "create_plugin_manager",
    "BUILTIN_PLUGINS",
    # Port adapters (for DI into orchestrator)
    "PluggyEventSink",
    "CompositeEventSink",
    "LoggingEventSink",
    "PluggySessionRunner",
    # Built-in plugins (for direct import if needed)
    "SubprocessPlugin",
    # Lifecycle plugins
    "LifecycleSSEPlugin",
    # Platform adapters
    "GitHubAdapter",
    # Issue implementation (Issue Protocol -> GitHubIssue)
    "GitHubIssue",
    # Issue resolvers (IssueKey -> backing-store handle)
    "GitHubIssueResolver",
    # Local VCS adapters
    "GitWorkingCopy",
    # Local command runner
    "LocalCommandRunner",
    # Session stores
    "JsonSessionStore",
    # Session output
    "FileSystemSessionOutput",
    # Goal pilot store
    "SqliteGoalPilotStore",
    # Provider circuit store
    "SQLiteProviderCircuitStore",
]
