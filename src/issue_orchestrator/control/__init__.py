"""Control plane - authority and decision-making.

This package contains components that make decisions and control state transitions.
These are the "Controllers" in the architecture.

Architecture principle:
- Components that OBSERVE are named Observers (observation/)
- Components that DECIDE are named Controllers (control/)
- Components that ACT are named Adapters (execution/)

The control plane:
- Makes policy decisions
- Advances state machines
- Determines what actions to take based on observations
- Does NOT directly call external systems (delegates to execution/)
"""

from .scheduler import Scheduler
from .completion_processor import CompletionProcessor, ProcessingResult
from .transition_guard import TransitionGuard, TransitionResult, TransitionResultType
from .session_manager import (
    SessionManager,
    SessionRef,
    SessionType,
    SessionContext,
    issue_session_context,
    review_session_context,
    rework_session_context,
)
from .label_projection import (
    LabelProjection,
    DesiredLabels,
    LabelCategory,
    compute_label_changes,
)
from .label_sync import LabelSync, LabelSyncResult

__all__ = [
    "Scheduler",
    "CompletionProcessor",
    "ProcessingResult",
    "TransitionGuard",
    "TransitionResult",
    "TransitionResultType",
    "SessionManager",
    "SessionRef",
    "SessionType",
    "SessionContext",
    "issue_session_context",
    "review_session_context",
    "rework_session_context",
    "LabelProjection",
    "DesiredLabels",
    "LabelCategory",
    "compute_label_changes",
    "LabelSync",
    "LabelSyncResult",
]
