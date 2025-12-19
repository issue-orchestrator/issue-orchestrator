"""State machines for issue orchestrator lifecycle management.

This module provides state machines for tracking the lifecycle of:
- Issues: From available through completion
- Sessions: From launch through completion or failure
- Reviews: From PR creation through merge or closure

Each state machine:
- Uses the transitions library for robust state management
- Emits events via EventBus for decoupled communication
- Provides type-safe state enums
- Includes validation and error handling
- Supports conditional transitions and callbacks
"""

from .issue_machine import IssueState, IssueStateMachine
from .review_machine import ReviewState, ReviewStateMachine
from .session_machine import SessionState, SessionStateMachine

__all__ = [
    # Issue state machine
    "IssueState",
    "IssueStateMachine",
    # Session state machine
    "SessionState",
    "SessionStateMachine",
    # Review state machine
    "ReviewState",
    "ReviewStateMachine",
]
