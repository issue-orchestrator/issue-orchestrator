"""Domain models and events for the issue orchestrator."""

from .events import Event, EventBus, IssueEvent, LabelEvent, ReviewEvent, SessionEvent
from .state_machines import (
    IssueState,
    IssueStateMachine,
    ReviewState,
    ReviewStateMachine,
    SessionState,
    SessionStateMachine,
)
from .issue_key import (
    IssueKey,
    GitHubIssueKey,
    FakeIssueKey,
    IssueHandle,
    ParsedTitle,
    parse_external_id,
)

__all__ = [
    # Events
    "Event",
    "EventBus",
    "IssueEvent",
    "SessionEvent",
    "ReviewEvent",
    "LabelEvent",
    # State machines
    "IssueState",
    "IssueStateMachine",
    "SessionState",
    "SessionStateMachine",
    "ReviewState",
    "ReviewStateMachine",
    # Issue identity
    "IssueKey",
    "GitHubIssueKey",
    "FakeIssueKey",
    "IssueHandle",
    "ParsedTitle",
    "parse_external_id",
]
