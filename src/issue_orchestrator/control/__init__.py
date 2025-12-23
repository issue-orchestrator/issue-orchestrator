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
from .workflows import (
    ReviewWorkflow,
    ReviewDecision,
    ReworkWorkflow,
    ReworkDecision,
    TriageWorkflow,
    TriageDecision,
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
    StopSessionAction,
    TransitionAction,
    QueueReviewAction,
    QueueReworkAction,
    QueueTriageAction,
    EscalateToHumanAction,
    AddCommentAction,
)
from .action_applier import ActionApplier
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
from .sandbox_verify import (
    VerificationResult,
    SandboxVerificationResult,
    verify_gh_auth_unavailable,
    verify_git_push_fails,
    verify_env_vars_absent,
    verify_home_isolated,
    verify_sandbox,
    run_verification_cli,
)
from .prepush_check import (
    run_prepush_check,
    load_publish_gate_config,
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
from .planner import Planner, Plan, OrchestratorSnapshot, SkippedItem

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
    "ReviewWorkflow",
    "ReviewDecision",
    "ReworkWorkflow",
    "ReworkDecision",
    "TriageWorkflow",
    "TriageDecision",
    # Actions and applier
    "Action",
    "ActionType",
    "ActionResult",
    "ActionResultType",
    "AddLabelAction",
    "RemoveLabelAction",
    "SyncLabelsAction",
    "LaunchSessionAction",
    "StopSessionAction",
    "TransitionAction",
    "QueueReviewAction",
    "QueueReworkAction",
    "QueueTriageAction",
    "EscalateToHumanAction",
    "AddCommentAction",
    "ActionApplier",
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
    "VerificationResult",
    "SandboxVerificationResult",
    "verify_gh_auth_unavailable",
    "verify_git_push_fails",
    "verify_env_vars_absent",
    "verify_home_isolated",
    "verify_sandbox",
    "run_verification_cli",
    # Pre-push check
    "run_prepush_check",
    "load_publish_gate_config",
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
]
