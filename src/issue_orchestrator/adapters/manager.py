"""Plugin manager for issue-orchestrator.

Creates and configures the pluggy plugin manager, registering plugins
based on configuration and entry points.
"""

import logging
from typing import Optional

import pluggy

from ..hookspec import PROJECT_NAME, TerminalSpec

logger = logging.getLogger(__name__)

# Built-in plugin mapping
BUILTIN_PLUGINS = {
    "tmux": "issue_orchestrator.adapters.terminal_tmux:TmuxPlugin",
    "iterm2": "issue_orchestrator.adapters.terminal_iterm:ITermPlugin",
    "iterm": "issue_orchestrator.adapters.terminal_iterm:ITermPlugin",  # alias
}

# UI mode to plugin mapping (backwards compatibility)
UI_MODE_PLUGINS = {
    "tmux": "tmux",
    "iterm2": "iterm2",
    "iterm": "iterm2",
    "web": "iterm2",  # Web mode uses iTerm2 for terminals
}


def _load_plugin_class(class_path: str):
    """Load a plugin class by its dotted path.

    Args:
        class_path: Path like "package.module:ClassName"

    Returns:
        Instantiated plugin object.
    """
    import importlib

    if ":" in class_path:
        module_path, class_name = class_path.rsplit(":", 1)
    else:
        module_path, class_name = class_path.rsplit(".", 1)

    module = importlib.import_module(module_path)
    plugin_class = getattr(module, class_name)
    return plugin_class()


def create_plugin_manager(
    terminal_plugin: Optional[str] = None,
    ui_mode: str = "web",
    load_entry_points: bool = True,
) -> pluggy.PluginManager:
    """Create and configure a plugin manager.

    Args:
        terminal_plugin: Explicit terminal plugin to load.
            Can be: "tmux", "iterm2", or a class path like "mypackage:MyPlugin"
        ui_mode: Fallback UI mode if terminal_plugin not specified.
        load_entry_points: Whether to load plugins from entry points.

    Returns:
        Configured PluginManager with hooks ready to call.
    """
    pm = pluggy.PluginManager(PROJECT_NAME)

    # Register hook specifications
    pm.add_hookspecs(TerminalSpec)

    # Determine which terminal plugin to load
    if terminal_plugin:
        plugin_ref = terminal_plugin
    else:
        # Fall back to UI mode mapping
        plugin_ref = UI_MODE_PLUGINS.get(ui_mode, "iterm2")

    # Load the terminal plugin
    if plugin_ref in BUILTIN_PLUGINS:
        class_path = BUILTIN_PLUGINS[plugin_ref]
    else:
        class_path = plugin_ref

    try:
        plugin = _load_plugin_class(class_path)
        pm.register(plugin, name=f"terminal_{plugin_ref}")
        logger.info("Loaded terminal plugin: %s", plugin_ref)
    except Exception as e:
        logger.error("Failed to load terminal plugin %s: %s", plugin_ref, e)
        raise

    # Load additional plugins from entry points
    if load_entry_points:
        # Entry point group: "issue_orchestrator.plugins"
        pm.load_setuptools_entrypoints(f"{PROJECT_NAME}.plugins")
        logger.debug("Loaded plugins from entry points")

    return pm


class PluginManager:
    """High-level wrapper around pluggy for issue-orchestrator.

    Provides a cleaner interface for the orchestrator to call hooks.
    """

    def __init__(
        self,
        terminal_plugin: Optional[str] = None,
        ui_mode: str = "web",
    ):
        """Initialize the plugin manager.

        Args:
            terminal_plugin: Explicit terminal plugin to load.
            ui_mode: Fallback UI mode for backwards compatibility.
        """
        self._pm = create_plugin_manager(
            terminal_plugin=terminal_plugin,
            ui_mode=ui_mode,
        )

    @property
    def hook(self):
        """Access the hook caller for direct hook invocation."""
        return self._pm.hook

    # Convenience methods that wrap hook calls with sensible defaults

    def create_session(
        self,
        session_id: int,
        command: str,
        working_dir: str,
        title: str | None = None,
    ) -> bool:
        """Create a terminal session."""
        result = self._pm.hook.create_session(
            session_id=session_id,
            command=command,
            working_dir=working_dir,
            title=title,
        )
        return result if result is not None else False

    def session_exists(self, session_id: int) -> bool:
        """Check if a session exists."""
        result = self._pm.hook.session_exists(session_id=session_id)
        return result if result is not None else False

    def kill_session(self, session_id: int) -> None:
        """Kill a session."""
        self._pm.hook.kill_session(session_id=session_id)

    def discover_running_sessions(self) -> list[dict]:
        """Discover running sessions."""
        result = self._pm.hook.discover_running_sessions()
        return result if result is not None else []

    def cleanup_idle_sessions(self) -> int:
        """Clean up idle sessions."""
        result = self._pm.hook.cleanup_idle_sessions()
        return result if result is not None else 0

    def get_session_output(self, session_id: int, lines: int = 50) -> str | None:
        """Get session output."""
        return self._pm.hook.get_session_output(session_id=session_id, lines=lines)
