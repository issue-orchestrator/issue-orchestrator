"""Canonical event name catalog.

This module defines all stable event names that can be consumed by UI, tests,
and external systems. Event names follow the format: {domain}.{action_past_tense}

Domains:
- orchestrator: Lifecycle (started, ready, idle, paused, resumed, shutdown_*)
- tick: Per-cycle boundaries (started, completed)
- facts: Discovery phase (gathered)
- plan: Planning phase (computed, noop)
- apply: Action execution (started, step_applied, completed, failed)
- session: Agent session lifecycle (started, observed, completed, failed)
- issue: Issue state changes (blocked, needs_human)
- dependency: Issue dependencies (blocked, unblocked)
- review: Code review workflow (started, queued, escalated)
- rework: Rework cycle (started)
- triage: Triage workflow (issue_created)
- cleanup: Cleanup operations (completed)
- validation: Local validation hooks (started, completed)
- config: Configuration events (merged)

All events emitted by the orchestrator MUST use EventName constants.
Raw strings are not accepted by TraceEvent.
"""

from enum import Enum


class EventName(str, Enum):
    """Canonical event names.

    Use these constants instead of raw strings to ensure consistency
    and enable IDE support/refactoring.
    """

    # =========================================================================
    # Orchestrator lifecycle
    # =========================================================================
    ORCHESTRATOR_STARTED = "orchestrator.started"
    ORCHESTRATOR_READY = "orchestrator.ready"
    ORCHESTRATOR_IDLE = "orchestrator.idle"
    ORCHESTRATOR_PAUSED = "orchestrator.paused"
    ORCHESTRATOR_RESUMED = "orchestrator.resumed"
    ORCHESTRATOR_SHUTDOWN_REQUESTED = "orchestrator.shutdown_requested"
    ORCHESTRATOR_SHUTDOWN_STARTED = "orchestrator.shutdown_started"
    ORCHESTRATOR_SHUTDOWN_COMPLETED = "orchestrator.shutdown_completed"
    ORCHESTRATOR_HEARTBEAT = "orchestrator.heartbeat"

    # =========================================================================
    # Tick boundaries (main orchestration cycle)
    # =========================================================================
    TICK_STARTED = "tick.started"
    TICK_COMPLETED = "tick.completed"

    # =========================================================================
    # Facts gathering
    # =========================================================================
    FACTS_GATHERED = "facts.gathered"
    ISSUES_FETCHED = "issues.fetched"

    # =========================================================================
    # Planning phase
    # =========================================================================
    PLAN_COMPUTED = "plan.computed"
    PLAN_NOOP = "plan.noop"

    # =========================================================================
    # Apply phase (action execution)
    # =========================================================================
    APPLY_STARTED = "apply.started"
    APPLY_STEP_APPLIED = "apply.step_applied"
    APPLY_COMPLETED = "apply.completed"
    APPLY_FAILED = "apply.failed"

    # =========================================================================
    # Action execution (per-action granularity)
    # =========================================================================
    ACTION_START = "action.start"
    ACTION_END = "action.end"

    # =========================================================================
    # Session lifecycle
    # =========================================================================
    SESSION_STARTED = "session.started"
    SESSION_OBSERVED = "session.observed"
    SESSION_COMPLETED = "session.completed"
    SESSION_FAILED = "session.failed"
    SESSION_NAME_PARSE_ERROR = "session.name_parse_error"
    SESSION_NO_COMPLETION_RECORD = "session.no_completion_record"
    SESSION_TIMEOUT_RECOVERED = "session.timeout_recovered"
    SESSION_PROCESSING_COMPLETED = "session.processing_completed"
    SESSION_START_FAILED = "session.start_failed"
    SESSION_STOPPED = "session.stopped"
    SESSION_CLEANUP = "session.cleanup"
    SESSION_LAUNCHED = "session.launched"
    SESSION_SLOW = "session.slow"
    SESSION_TIMEOUT = "session.timeout"
    SESSION_BLOCKED = "session.blocked"
    SESSION_RESUMED = "session.resumed"
    SESSION_NO_OUTPUT = "session.no_output"
    GH_RATE_LIMIT = "gh.rate_limit"
    GH_RATE_LIMIT_WARNING = "gh.rate_limit_warning"

    # =========================================================================
    # Completion lookup (agent-done processing)
    # =========================================================================
    COMPLETION_LOOKUP = "completion.lookup"

    # =========================================================================
    # Issue state
    # =========================================================================
    ISSUE_BLOCKED = "issue.blocked"
    ISSUE_NEEDS_HUMAN = "issue.needs_human"
    ISSUE_DEPENDENCY_BLOCKED = "issue.dependency_blocked"
    ISSUE_CLAIMED = "issue.claimed"
    ISSUE_STARTED = "issue.started"
    ISSUE_UNBLOCKED = "issue.unblocked"
    ISSUE_PR_CREATED = "issue.pr_created"
    ISSUE_PR_REJECTED = "issue.pr_rejected"
    ISSUE_COMPLETED = "issue.completed"
    ISSUE_RELEASED = "issue.released"

    # =========================================================================
    # Dependencies
    # =========================================================================
    DEPENDENCY_BLOCKED = "dependency.blocked"
    DEPENDENCY_UNBLOCKED = "dependency.unblocked"

    # =========================================================================
    # Code review workflow
    # =========================================================================
    REVIEW_STARTED = "review.started"
    REVIEW_QUEUED = "review.queued"
    REVIEW_ESCALATED = "review.escalated"
    REVIEW_SKIPPED = "review.skipped"
    REVIEW_LAUNCHING = "review.launching"
    REVIEW_APPROVED = "review.approved"
    REVIEW_CHANGES_REQUESTED = "review.changes_requested"
    REVIEW_REWORK_STARTED = "review.rework_started"
    REVIEW_REWORK_COMPLETED = "review.rework_completed"
    REVIEW_TRIAGE_STARTED = "review.triage_started"
    REVIEW_TRIAGE_APPROVED = "review.triage_approved"
    REVIEW_MERGED = "review.merged"
    REVIEW_CLOSED = "review.closed"

    # =========================================================================
    # Rework cycle
    # =========================================================================
    REWORK_STARTED = "rework.started"
    REWORK_SKIPPED = "rework.skipped"
    REWORK_LAUNCHING = "rework.launching"
    REWORK_ESCALATING = "rework.escalating"

    # =========================================================================
    # Triage workflow
    # =========================================================================
    TRIAGE_ISSUE_CREATED = "triage.issue_created"
    TRIAGE_SKIPPED = "triage.skipped"
    TRIAGE_LAUNCHING = "triage.launching"
    TRIAGE_BATCH_TRIGGERED = "triage.batch_triggered"

    # =========================================================================
    # Cleanup operations
    # =========================================================================
    CLEANUP_COMPLETED = "cleanup.completed"

    # =========================================================================
    # Validation (subprocess hooks)
    # =========================================================================
    VALIDATION_STARTED = "validation.started"
    VALIDATION_COMPLETED = "validation.completed"

    # =========================================================================
    # Configuration
    # =========================================================================
    CONFIG_MERGED = "config.merged"

    # =========================================================================
    # Reconciliation (label state verification)
    # =========================================================================
    RECONCILIATION_CHECKED = "reconciliation.checked"
    RECONCILIATION_WARNING = "reconciliation.warning"
    RECONCILIATION_REQUIRED = "reconciliation.required"  # Drift detected, action blocked
    ISSUE_PAUSED_RECONCILE = "issue.paused_reconcile"  # Issue paused due to drift

    # =========================================================================
    # Queue projection (UI-specific, consider moving to projection layer)
    # =========================================================================
    QUEUE_CHANGED = "queue.changed"

    # =========================================================================
    # Labels
    # =========================================================================
    LABELS_SYNCED = "labels.synced"
    ISSUE_LABELS_CHANGED = "issue.labels_changed"
    PR_VIEW_CHANGED = "pr.view_changed"

    # =========================================================================
    # Transitions (state machine)
    # =========================================================================
    TRANSITION_APPLIED = "transition.applied"
    TRANSITION_REJECTED = "transition.rejected"

    # =========================================================================
    # PR Scanner
    # =========================================================================
    SCANNER_REVIEWS_FOUND = "scanner.reviews_found"
    SCANNER_REWORKS_FOUND = "scanner.reworks_found"

    # =========================================================================
    # Dependencies evaluation
    # =========================================================================
    DEPENDENCIES_EVALUATED = "dependencies.evaluated"

    # =========================================================================
    # Observation (session monitoring)
    # =========================================================================
    OBSERVATION_COMPLETION_DETECTED = "observation.completion_detected"
    OBSERVATION_RESULT = "observation.result"

    # =========================================================================
    # Resolver (identity resolution)
    # =========================================================================
    RESOLVER_DUPLICATE_EXTERNAL_ID = "resolver.duplicate_external_id"

    def __str__(self) -> str:
        """Return the event name string for use in TraceEvent."""
        return self.value


# Schema version for event payload evolution
EVENT_SCHEMA_VERSION = 1
