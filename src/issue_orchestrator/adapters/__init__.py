"""Pluggable adapters for external dependencies.

This package provides a plugin architecture using pluggy for external integrations:
- Terminal plugins (tmux, iTerm2, etc.)
- AI plugins (Claude, ChatGPT, etc.) [future]
- Issue tracker plugins (GitHub, GitLab, etc.) [future]
- State storage plugins (JSON, SQLite, etc.) [future]

Plugins implement hooks defined in hookspec.py using the @hookimpl decorator.

Usage:
    from issue_orchestrator.adapters import PluginManager

    # Create with explicit plugin
    pm = PluginManager(terminal_plugin="tmux")

    # Or use ui_mode for backwards compatibility
    pm = PluginManager(ui_mode="web")

    # Call hooks via convenience methods
    pm.create_session(42, "claude ...", "/path/to/worktree", "Issue title")

    # Or access hooks directly
    pm.hook.create_session(session_id=42, command="...", working_dir="...", title="...")

Third-party plugins can be registered via entry points in pyproject.toml:
    [project.entry-points."issue_orchestrator.plugins"]
    my_plugin = "my_package:MyPlugin"
"""

from .manager import PluginManager, create_plugin_manager, BUILTIN_PLUGINS
from .terminal_tmux import TmuxPlugin
from .terminal_iterm import ITermPlugin

__all__ = [
    # Main interface
    "PluginManager",
    "create_plugin_manager",
    "BUILTIN_PLUGINS",
    # Built-in plugins (for direct import if needed)
    "TmuxPlugin",
    "ITermPlugin",
]
