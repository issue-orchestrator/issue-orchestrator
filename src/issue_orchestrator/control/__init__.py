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
from .session_manager import (
    SessionManager,
    SessionRef,
    SessionType,
    SessionContext,
    issue_session_context,
    review_session_context,
    rework_session_context,
)
from .label_sync import LabelSync, LabelSyncResult, DesiredLabels, compute_label_changes
from .workflows import (
    ReviewWorkflow,
    ReviewDecision,
    RetrospectiveReviewWorkflow,
    RetrospectiveReviewDecision,
    ReworkWorkflow,
    ReworkDecision,
    TechLeadWorkflow,
    TechLeadDecision,
)
from .actions import (
    Action,
    ActionType,
    ActionResult,
    ActionResultType,
    AddLabelAction,
    RemoveLabelAction,
    SyncLabelsAction,
    LaunchSessionAction,
    LaunchValidationRetryAction,
    StopSessionAction,
    QueueReviewAction,
    QueueRetrospectiveReviewAction,
    QueueReworkAction,
    QueueTechLeadAction,
    EscalateToHumanAction,
    AddCommentAction,
    ReconcileHistoryEntryAction,
    RecoverTerminalIssueAction,
)
from .action_applier import ActionApplier
from .session_history import SessionHistoryOwner
from .validation import (
    ValidationRecord,
    ValidationRecordStore,
    ValidationRunner,
    ValidationCache,
    PublishGate,
    PublishGateResult,
    AgentGate,
    AgentGateResult,
    VALIDATION_SCHEMA_VERSION,
)
from .isolation import (
    FORBIDDEN_ENV_VARS,
    GIT_SAFE_ENV,
    get_forbidden_env_vars,
    build_env_unset_commands,
    build_git_safe_commands,
    build_home_isolation_command,
    build_isolation_prefix,
    verify_env_scrubbed,
    all_env_scrubbed,
)
from .reconciliation import (
    ReconciliationRequired,
    ExternalSnapshot,
    ExpectedState,
    ReconciliationResult,
    check_reconciliation,
    require_reconciliation,
)
from .session_controller import SessionController, SessionDecision
from .goal_pilot import GoalPilot
from .planner import Planner
from .planner_types import Plan, OrchestratorSnapshot, SkippedItem
from .orchestrator_deps import OrchestratorDeps

__all__ = [
    "Scheduler",
    "CompletionProcessor",
    "ProcessingResult",
    "SessionManager",
    "SessionRef",
    "SessionType",
    "SessionContext",
    "issue_session_context",
    "review_session_context",
    "rework_session_context",
    "LabelSync",
    "LabelSyncResult",
    "DesiredLabels",
    "compute_label_changes",
    "ReviewWorkflow",
    "ReviewDecision",
    "RetrospectiveReviewWorkflow",
    "RetrospectiveReviewDecision",
    "ReworkWorkflow",
    "ReworkDecision",
    "TechLeadWorkflow",
    "TechLeadDecision",
    # Actions and applier
    "Action",
    "ActionType",
    "ActionResult",
    "ActionResultType",
    "AddLabelAction",
    "RemoveLabelAction",
    "SyncLabelsAction",
    "LaunchSessionAction",
    "LaunchValidationRetryAction",
    "StopSessionAction",
    "QueueReviewAction",
    "QueueRetrospectiveReviewAction",
    "QueueReworkAction",
    "QueueTechLeadAction",
    "EscalateToHumanAction",
    "AddCommentAction",
    "ReconcileHistoryEntryAction",
    "RecoverTerminalIssueAction",
    "ActionApplier",
    "SessionHistoryOwner",
    # Validation
    "ValidationRecord",
    "ValidationRecordStore",
    "ValidationRunner",
    "ValidationCache",
    "PublishGate",
    "PublishGateResult",
    "AgentGate",
    "AgentGateResult",
    "VALIDATION_SCHEMA_VERSION",
    # Isolation
    "FORBIDDEN_ENV_VARS",
    "GIT_SAFE_ENV",
    "get_forbidden_env_vars",
    "build_env_unset_commands",
    "build_git_safe_commands",
    "build_home_isolation_command",
    "build_isolation_prefix",
    "verify_env_scrubbed",
    "all_env_scrubbed",
    # Sandbox verification
    # Pre-push check
    # Reconciliation
    "ReconciliationRequired",
    "ExternalSnapshot",
    "ExpectedState",
    "ReconciliationResult",
    "check_reconciliation",
    "require_reconciliation",
    # Session controller
    "SessionController",
    "SessionDecision",
    # Planner
    "Planner",
    "Plan",
    "OrchestratorSnapshot",
    "SkippedItem",
    # Goal pilot
    "GoalPilot",
    # Dependencies container
    "OrchestratorDeps",
]
