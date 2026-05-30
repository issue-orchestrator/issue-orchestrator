"""Workflow modules for the orchestrator.

Each workflow is a tickable module that:
1. Examines current state (via ReconciliationResult or simpler context)
2. Determines what actions should be taken
3. Returns a list of Actions (or performs them directly in transitional code)

Workflows:
- ReviewWorkflow: Handles code review lifecycle
- ReworkWorkflow: Handles rework cycle after review rejection
- TriageWorkflow: Handles failure investigation and batch triage

Architecture principle: workflows contain POLICY (what should happen),
not MECHANICS (how to do it). Mechanics live in execution adapters.
"""

from .decision_base import WorkflowDecision
from .review_workflow import ReviewWorkflow, ReviewDecision
from .retrospective_review_workflow import (
    RetrospectiveReviewDecision,
    RetrospectiveReviewWorkflow,
)
from .rework_workflow import ReworkWorkflow, ReworkDecision
from .triage_workflow import TriageWorkflow, TriageDecision

__all__ = [
    "WorkflowDecision",
    "ReviewWorkflow",
    "ReviewDecision",
    "RetrospectiveReviewWorkflow",
    "RetrospectiveReviewDecision",
    "ReworkWorkflow",
    "ReworkDecision",
    "TriageWorkflow",
    "TriageDecision",
]
