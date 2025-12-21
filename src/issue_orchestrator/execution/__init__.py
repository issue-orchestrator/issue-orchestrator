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
- Terminal plugins (tmux, iTerm2, etc.)
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
from .terminal_tmux import TmuxPlugin
from .terminal_iterm import ITermPlugin
from .github_adapter import GitHubAdapter
from .git_working_copy import GitWorkingCopy
from .json_store import JsonSessionStore
from .lifecycle_ipc import LifecycleIPCPlugin
from .lifecycle_sse import LifecycleSSEPlugin
from .event_sink_adapter import PluggyEventSink, CompositeEventSink, LoggingEventSink
from .session_runner_adapter import PluggySessionRunner

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
    "TmuxPlugin",
    "ITermPlugin",
    # Lifecycle plugins
    "LifecycleIPCPlugin",
    "LifecycleSSEPlugin",
    # Platform adapters
    "GitHubAdapter",
    # Local VCS adapters
    "GitWorkingCopy",
    # Session stores
    "JsonSessionStore",
]
