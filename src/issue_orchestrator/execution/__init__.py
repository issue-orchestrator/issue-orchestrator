# pyright: reportUnsupportedDunderAll=false
# Lazy __getattr__ exports are runtime-resolved; pyright cannot see them in __all__.
"""Execution layer adapters and storage implementations."""

from importlib import import_module
from typing import Any

_EXPORTS = {
    "PluginManager": (".manager", "PluginManager"),
    "create_plugin_manager": (".manager", "create_plugin_manager"),
    "BUILTIN_PLUGINS": (".manager", "BUILTIN_PLUGINS"),
    "PluggyEventSink": (".event_sink_adapter", "PluggyEventSink"),
    "CompositeEventSink": (".event_sink_adapter", "CompositeEventSink"),
    "LoggingEventSink": (".event_sink_adapter", "LoggingEventSink"),
    "PluggySessionRunner": (".session_runner_adapter", "PluggySessionRunner"),
    "SubprocessPlugin": (".terminal_subprocess", "SubprocessPlugin"),
    "LifecycleSSEPlugin": (".lifecycle_sse", "LifecycleSSEPlugin"),
    "GitHubAdapter": ("..adapters.github.github_adapter", "GitHubAdapter"),
    "GitHubIssue": ("..adapters.github.github_issue", "GitHubIssue"),
    "GitHubIssueResolver": ("..adapters.github.issue_resolver", "GitHubIssueResolver"),
    "GitWorkingCopy": (".git_working_copy", "GitWorkingCopy"),
    "LocalCommandRunner": (".command_runner", "LocalCommandRunner"),
    "JsonSessionStore": (".json_store", "JsonSessionStore"),
    "FileSystemSessionOutput": (".session_output_adapter", "FileSystemSessionOutput"),
    "SqliteGoalPilotStore": (".goal_pilot_store", "SqliteGoalPilotStore"),
    "SQLiteProviderCircuitStore": (".provider_circuit_store", "SQLiteProviderCircuitStore"),
    "QueueCacheStore": (".queue_cache_store", "QueueCacheStore"),
    "TimelineEventSink": (".timeline_event_sink", "TimelineEventSink"),
    "DefaultTimelineReader": (".timeline_reader", "DefaultTimelineReader"),
    "SqliteTimelineStore": (".timeline_store", "SqliteTimelineStore"),
    "TimelineStoreConfig": (".timeline_store", "TimelineStoreConfig"),
    "DefaultTimelineWriter": (".timeline_writer", "DefaultTimelineWriter"),
}

__all__ = (
    "PluginManager",
    "create_plugin_manager",
    "BUILTIN_PLUGINS",
    "PluggyEventSink",
    "CompositeEventSink",
    "LoggingEventSink",
    "PluggySessionRunner",
    "SubprocessPlugin",
    "LifecycleSSEPlugin",
    "GitHubAdapter",
    "GitHubIssue",
    "GitHubIssueResolver",
    "GitWorkingCopy",
    "LocalCommandRunner",
    "JsonSessionStore",
    "FileSystemSessionOutput",
    "SqliteGoalPilotStore",
    "SQLiteProviderCircuitStore",
    "QueueCacheStore",
    "TimelineEventSink",
    "DefaultTimelineReader",
    "SqliteTimelineStore",
    "TimelineStoreConfig",
    "DefaultTimelineWriter",
)


def __getattr__(name: str) -> Any:
    try:
        module_name, attr_name = _EXPORTS[name]
    except KeyError as exc:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}") from exc

    value = getattr(import_module(module_name, __name__), attr_name)
    globals()[name] = value
    return value


def __dir__() -> list[str]:
    return sorted([*globals(), *__all__])
