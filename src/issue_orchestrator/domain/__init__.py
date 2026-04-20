# pyright: reportUnsupportedDunderAll=false
# Lazy __getattr__ exports are runtime-resolved; pyright cannot see them in __all__.
"""Domain models and events for the issue orchestrator."""

from importlib import import_module
from typing import Any

_EXPORTS = {
    "Event": (".events", "Event"),
    "EventBus": (".events", "EventBus"),
    "IssueEvent": (".events", "IssueEvent"),
    "LabelEvent": (".events", "LabelEvent"),
    "ReviewEvent": (".events", "ReviewEvent"),
    "SessionEvent": (".events", "SessionEvent"),
    "IssueState": (".state_machines", "IssueState"),
    "IssueStateMachine": (".state_machines", "IssueStateMachine"),
    "ReviewState": (".state_machines", "ReviewState"),
    "ReviewStateMachine": (".state_machines", "ReviewStateMachine"),
    "SessionState": (".state_machines", "SessionState"),
    "SessionStateMachine": (".state_machines", "SessionStateMachine"),
    "StableIssueId": (".issue_key", "StableIssueId"),
    "IssueKey": (".issue_key", "IssueKey"),
    "GitHubIssueKey": (".issue_key", "GitHubIssueKey"),
    "FakeIssueKey": (".issue_key", "FakeIssueKey"),
    "IssueHandle": (".issue_key", "IssueHandle"),
    "ParsedTitle": (".issue_key", "ParsedTitle"),
    "parse_external_id": (".issue_key", "parse_external_id"),
    "TaskKind": (".session_key", "TaskKind"),
    "SessionKey": (".session_key", "SessionKey"),
    "TimelineKey": (".timeline_key", "TimelineKey"),
    "ProcessState": (".process_state", "ProcessState"),
    "ProcessExitInfo": (".process_state", "ProcessExitInfo"),
}

__all__ = (
    "Event",
    "EventBus",
    "IssueEvent",
    "LabelEvent",
    "ReviewEvent",
    "SessionEvent",
    "IssueState",
    "IssueStateMachine",
    "ReviewState",
    "ReviewStateMachine",
    "SessionState",
    "SessionStateMachine",
    "StableIssueId",
    "IssueKey",
    "GitHubIssueKey",
    "FakeIssueKey",
    "IssueHandle",
    "ParsedTitle",
    "parse_external_id",
    "TaskKind",
    "SessionKey",
    "TimelineKey",
    "ProcessState",
    "ProcessExitInfo",
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
