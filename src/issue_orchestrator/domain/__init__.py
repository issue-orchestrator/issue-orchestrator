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
    StableIssueId,
    IssueKey,
    GitHubIssueKey,
    FakeIssueKey,
    IssueHandle,
    ParsedTitle,
    parse_external_id,
)
from .session_key import (
    TaskKind,
    SessionKey,
)
from .timeline_key import TimelineKey
from .process_state import (
    ProcessState,
    ProcessExitInfo,
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
    "StableIssueId",
    "IssueKey",
    "GitHubIssueKey",
    "FakeIssueKey",
    "IssueHandle",
    "ParsedTitle",
    "parse_external_id",
    # Session identity
    "TaskKind",
    "SessionKey",
    # Timeline identity
    "TimelineKey",
    # Process observation
    "ProcessState",
    "ProcessExitInfo",
]
