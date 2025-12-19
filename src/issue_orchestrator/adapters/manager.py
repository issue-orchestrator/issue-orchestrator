"""Plugin manager for issue-orchestrator.

Creates and configures the pluggy plugin manager, registering plugins
based on configuration and entry points.
"""

import logging
from typing import Optional

import pluggy

from ..hookspec import PROJECT_NAME, TerminalSpec, LifecycleSpec

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
    pm.add_hookspecs(LifecycleSpec)

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

    def register_plugin(self, plugin: object, name: str | None = None) -> None:
        """Register an additional plugin.

        Args:
            plugin: Plugin instance implementing hook methods
            name: Optional name for the plugin
        """
        self._pm.register(plugin, name=name)
        logger.info("Registered plugin: %s", name or type(plugin).__name__)

    # Lifecycle hook convenience methods

    def notify_issue_claimed(
        self,
        issue_number: int,
        title: str,
        agent_type: str,
    ) -> None:
        """Notify all plugins that an issue was claimed."""
        self._pm.hook.on_issue_claimed(
            issue_number=issue_number,
            title=title,
            agent_type=agent_type,
        )

    def notify_session_started(
        self,
        issue_number: int,
        session_id: str,
        worktree_path: str,
        branch_name: str,
    ) -> None:
        """Notify all plugins that a session started."""
        self._pm.hook.on_session_started(
            issue_number=issue_number,
            session_id=session_id,
            worktree_path=worktree_path,
            branch_name=branch_name,
        )

    def notify_session_completed(
        self,
        issue_number: int,
        session_id: str,
        pr_url: str | None,
        runtime_minutes: float | None,
    ) -> None:
        """Notify all plugins that a session completed."""
        self._pm.hook.on_session_completed(
            issue_number=issue_number,
            session_id=session_id,
            pr_url=pr_url,
            runtime_minutes=runtime_minutes,
        )

    def notify_session_failed(
        self,
        issue_number: int,
        session_id: str,
        error: str | None,
        runtime_minutes: float | None,
    ) -> None:
        """Notify all plugins that a session failed."""
        self._pm.hook.on_session_failed(
            issue_number=issue_number,
            session_id=session_id,
            error=error,
            runtime_minutes=runtime_minutes,
        )

    def notify_issue_blocked(
        self,
        issue_number: int,
        reason: str | None,
    ) -> None:
        """Notify all plugins that an issue is blocked."""
        self._pm.hook.on_issue_blocked(
            issue_number=issue_number,
            reason=reason,
        )

    def notify_issue_needs_human(
        self,
        issue_number: int,
        reason: str | None,
    ) -> None:
        """Notify all plugins that an issue needs human help."""
        self._pm.hook.on_issue_needs_human(
            issue_number=issue_number,
            reason=reason,
        )

    def notify_pr_created(
        self,
        issue_number: int,
        pr_number: int,
        pr_url: str,
        title: str,
    ) -> None:
        """Notify all plugins that a PR was created."""
        self._pm.hook.on_pr_created(
            issue_number=issue_number,
            pr_number=pr_number,
            pr_url=pr_url,
            title=title,
        )

    def notify_review_requested(
        self,
        pr_number: int,
        issue_number: int,
        review_type: str,
    ) -> None:
        """Notify all plugins that a review was requested."""
        self._pm.hook.on_review_requested(
            pr_number=pr_number,
            issue_number=issue_number,
            review_type=review_type,
        )

    def notify_review_completed(
        self,
        pr_number: int,
        issue_number: int,
        result: str,
        rework_count: int,
    ) -> None:
        """Notify all plugins that a review was completed."""
        self._pm.hook.on_review_completed(
            pr_number=pr_number,
            issue_number=issue_number,
            result=result,
            rework_count=rework_count,
        )

    def notify_review_escalated(
        self,
        pr_number: int,
        issue_number: int,
        rework_count: int,
        max_rework_cycles: int,
    ) -> None:
        """Notify all plugins that a review was escalated.

        This is a critical event - the bounded review loop has failed
        and human intervention is required.
        """
        self._pm.hook.on_review_escalated(
            pr_number=pr_number,
            issue_number=issue_number,
            rework_count=rework_count,
            max_rework_cycles=max_rework_cycles,
        )

    def notify_state_changed(
        self,
        active_count: int,
        paused: bool,
        completed_today: int,
    ) -> None:
        """Notify all plugins of orchestrator state change."""
        self._pm.hook.on_orchestrator_state_changed(
            active_count=active_count,
            paused=paused,
            completed_today=completed_today,
        )
