"""Declarative projection spec — single source of truth for how an event
appears in the timeline.

Every `EventName` has exactly one `EventSpec` entry mapping it to its
display fields:

    phase   — high-level stage shown in UI ("in_progress", "reviewing", etc.)
    step    — the specific action within the phase ("started", "approved")
    status  — outcome marker ("started", "completed", "failed", "active")
    level   — visibility tier ("phase" = top-level, "detail" = nested,
              "info" = e2e)

Why a separate `events/spec.py` next to `events/catalog.py`?

  - `catalog.py` (`EventName`) is a stable string contract. It rarely changes.
  - `spec.py` is a *behavior* contract — how each event renders. It evolves
    independently as the timeline UX is refined.

Keeping them separate prevents UX iteration from churning the wire-protocol
enum and lets the spec own its own test suite.

This file is purely declarative. The projection helpers in `timeline.py`
will be cut over to consult `EVENT_SPEC` in a follow-up PR; today this
module is additive and exists alongside the existing `_phase_for_event` /
`_step_for_event` / `_status_for_event` / `_level_for_event` helpers.
Parity is enforced by `tests/unit/test_event_spec.py`.

Entries are grouped to mirror the `EventName` declaration order so that
related events stay together — making anomalies (e.g. one review event
with the wrong status) visible at a glance.
"""

from __future__ import annotations

from dataclasses import dataclass

from .catalog import EventName


@dataclass(frozen=True)
class EventSpec:
    """How a single event projects into a `TimelineEvent` shape."""

    phase: str
    step: str
    status: str
    level: str


# ---------------------------------------------------------------------------
# EVENT_SPEC — every EventName maps to exactly one EventSpec.
#
# Order mirrors `events/catalog.py` so reviewers can scan adjacent events
# (e.g. all review.* together) and spot row-level anomalies.
# ---------------------------------------------------------------------------

EVENT_SPEC: dict[EventName, EventSpec] = {
    # ----- Orchestrator lifecycle -----
    EventName.ORCHESTRATOR_STARTED: EventSpec(phase='system', step='orchestrator.started', status='started', level='detail'),
    EventName.ORCHESTRATOR_READY: EventSpec(phase='system', step='orchestrator.ready', status='completed', level='detail'),
    EventName.ORCHESTRATOR_IDLE: EventSpec(phase='system', step='orchestrator.idle', status='completed', level='detail'),
    EventName.ORCHESTRATOR_PAUSED: EventSpec(phase='system', step='orchestrator.paused', status='completed', level='detail'),
    EventName.ORCHESTRATOR_RESUMED: EventSpec(phase='system', step='orchestrator.resumed', status='completed', level='detail'),
    EventName.ORCHESTRATOR_SHUTDOWN_REQUESTED: EventSpec(phase='system', step='orchestrator.shutdown_requested', status='completed', level='detail'),
    EventName.ORCHESTRATOR_SHUTDOWN_STARTED: EventSpec(phase='system', step='orchestrator.shutdown_started', status='completed', level='detail'),
    EventName.ORCHESTRATOR_SHUTDOWN_COMPLETED: EventSpec(phase='system', step='orchestrator.shutdown_completed', status='completed', level='detail'),
    EventName.ORCHESTRATOR_HEARTBEAT: EventSpec(phase='system', step='orchestrator.heartbeat', status='completed', level='detail'),

    # ----- Tick boundaries -----
    EventName.TICK_STARTED: EventSpec(phase='system', step='tick.started', status='started', level='detail'),
    EventName.TICK_COMPLETED: EventSpec(phase='system', step='tick.completed', status='completed', level='detail'),

    # ----- Facts gathering -----
    EventName.FACTS_GATHERED: EventSpec(phase='system', step='facts.gathered', status='completed', level='detail'),
    EventName.ISSUES_FETCHED: EventSpec(phase='system', step='issues.fetched', status='completed', level='detail'),

    # ----- Planning -----
    EventName.PLAN_COMPUTED: EventSpec(phase='system', step='plan.computed', status='completed', level='detail'),
    EventName.PLAN_NOOP: EventSpec(phase='system', step='plan.noop', status='completed', level='detail'),

    # ----- Apply phase -----
    EventName.APPLY_STARTED: EventSpec(phase='system', step='apply.started', status='started', level='detail'),
    EventName.APPLY_STEP_APPLIED: EventSpec(phase='system', step='apply.step_applied', status='completed', level='detail'),
    EventName.APPLY_COMPLETED: EventSpec(phase='system', step='apply.completed', status='completed', level='detail'),
    EventName.APPLY_FAILED: EventSpec(phase='system', step='apply.failed', status='completed', level='detail'),

    # ----- Action execution -----
    EventName.ACTION_START: EventSpec(phase='system', step='action.start', status='completed', level='detail'),
    EventName.ACTION_END: EventSpec(phase='system', step='action.end', status='completed', level='detail'),

    # ----- Session lifecycle -----
    EventName.SESSION_STARTED: EventSpec(phase='in_progress', step='started', status='started', level='detail'),
    EventName.SESSION_OBSERVED: EventSpec(phase='in_progress', step='observed', status='completed', level='detail'),
    EventName.SESSION_COMPLETED: EventSpec(phase='in_progress', step='completed', status='completed', level='detail'),
    EventName.SESSION_FAILED: EventSpec(phase='in_progress', step='failed', status='failed', level='detail'),
    EventName.SESSION_NAME_PARSE_ERROR: EventSpec(phase='in_progress', step='name_parse_error', status='completed', level='detail'),
    EventName.SESSION_NO_COMPLETION_RECORD: EventSpec(phase='in_progress', step='no_completion_record', status='completed', level='detail'),
    EventName.SESSION_TIMEOUT_RECOVERED: EventSpec(phase='in_progress', step='timeout_recovered', status='completed', level='detail'),
    EventName.SESSION_PROCESSING_COMPLETED: EventSpec(phase='in_progress', step='processing_completed', status='completed', level='detail'),
    EventName.SESSION_START_FAILED: EventSpec(phase='in_progress', step='start_failed', status='completed', level='detail'),
    EventName.SESSION_STOPPED: EventSpec(phase='in_progress', step='stopped', status='completed', level='detail'),
    EventName.SESSION_CLEANUP: EventSpec(phase='in_progress', step='cleanup', status='completed', level='detail'),
    EventName.SESSION_LAUNCHED: EventSpec(phase='in_progress', step='launched', status='completed', level='detail'),
    EventName.SESSION_RESTORED: EventSpec(phase='in_progress', step='restored', status='completed', level='detail'),
    EventName.SESSION_SLOW: EventSpec(phase='in_progress', step='slow', status='completed', level='detail'),
    EventName.SESSION_TIMEOUT: EventSpec(phase='in_progress', step='timeout', status='failed', level='detail'),
    EventName.SESSION_BLOCKED: EventSpec(phase='in_progress', step='blocked', status='failed', level='detail'),
    EventName.SESSION_RESUMED: EventSpec(phase='in_progress', step='resumed', status='completed', level='detail'),
    EventName.SESSION_NO_OUTPUT: EventSpec(phase='in_progress', step='no_output', status='completed', level='detail'),
    EventName.SESSION_VALIDATION_PASSED: EventSpec(phase='orchestrator', step='validation_passed', status='completed', level='detail'),
    EventName.SESSION_VALIDATION_FAILED: EventSpec(phase='orchestrator', step='validation_failed', status='failed', level='detail'),
    EventName.SESSION_VALIDATION_RETRY_NEEDED: EventSpec(phase='orchestrator', step='validation_retry_needed', status='failed', level='detail'),
    EventName.SESSION_ARTIFACT_LOOKUP: EventSpec(phase='in_progress', step='artifact_lookup', status='completed', level='detail'),
    EventName.GH_RATE_LIMIT: EventSpec(phase='system', step='gh.rate_limit', status='completed', level='detail'),
    EventName.GH_RATE_LIMIT_WARNING: EventSpec(phase='system', step='gh.rate_limit_warning', status='completed', level='detail'),
    EventName.GH_SEARCH_ITEM_MALFORMED: EventSpec(phase='system', step='gh.search_item_malformed', status='completed', level='detail'),

    # ----- Agent lifecycle projections -----
    EventName.AGENT_CODING_COMPLETED: EventSpec(phase='system', step='agent.coding_completed', status='completed', level='detail'),

    # ----- Worktree -----
    EventName.WORKTREE_RESET: EventSpec(phase='system', step='worktree.reset', status='completed', level='detail'),

    # ----- Completion lookup -----
    EventName.COMPLETION_LOOKUP: EventSpec(phase='in_progress', step='lookup', status='completed', level='detail'),

    # ----- Issue state -----
    EventName.ISSUE_BLOCKED: EventSpec(phase='blocked', step='blocked', status='failed', level='phase'),
    EventName.ISSUE_NEEDS_HUMAN: EventSpec(phase='needs_human', step='needs_human', status='failed', level='phase'),
    EventName.ISSUE_DEPENDENCY_BLOCKED: EventSpec(phase='in_progress', step='dependency_blocked', status='failed', level='phase'),
    EventName.ISSUE_CLAIMED: EventSpec(phase='in_progress', step='claimed', status='started', level='phase'),
    EventName.ISSUE_STARTED: EventSpec(phase='in_progress', step='started', status='started', level='phase'),
    EventName.ISSUE_UNBLOCKED: EventSpec(phase='in_progress', step='unblocked', status='completed', level='phase'),
    EventName.ISSUE_PR_CREATED: EventSpec(phase='orchestrator', step='pr_created', status='completed', level='phase'),
    EventName.ISSUE_PR_REJECTED: EventSpec(phase='in_progress', step='pr_rejected', status='failed', level='phase'),
    EventName.ISSUE_COMPLETED: EventSpec(phase='completed', step='completed', status='completed', level='phase'),
    EventName.ISSUE_RELEASED: EventSpec(phase='in_progress', step='released', status='completed', level='phase'),

    # ----- Dependencies -----
    EventName.DEPENDENCY_BLOCKED: EventSpec(phase='system', step='dependency.blocked', status='failed', level='detail'),
    EventName.DEPENDENCY_UNBLOCKED: EventSpec(phase='system', step='dependency.unblocked', status='completed', level='detail'),

    # ----- Code review workflow -----
    EventName.REVIEW_STARTED: EventSpec(phase='reviewing', step='started', status='started', level='phase'),
    EventName.REVIEW_QUEUED: EventSpec(phase='orchestrator', step='queued', status='started', level='phase'),
    EventName.REVIEW_ESCALATED: EventSpec(phase='reviewing', step='escalated', status='failed', level='phase'),
    EventName.REVIEW_SKIPPED: EventSpec(phase='reviewing', step='skipped', status='completed', level='phase'),
    EventName.REVIEW_LAUNCHING: EventSpec(phase='reviewing', step='launching', status='started', level='phase'),
    EventName.REVIEW_APPROVED: EventSpec(phase='reviewing', step='approved', status='completed', level='phase'),
    EventName.REVIEW_CHANGES_REQUESTED: EventSpec(phase='reviewing', step='changes_requested', status='failed', level='phase'),
    EventName.REVIEW_REWORK_STARTED: EventSpec(phase='reviewing', step='rework_started', status='started', level='phase'),
    EventName.REVIEW_REWORK_COMPLETED: EventSpec(phase='reviewing', step='rework_completed', status='completed', level='phase'),
    EventName.REVIEW_TRIAGE_STARTED: EventSpec(phase='reviewing', step='triage_started', status='started', level='phase'),
    EventName.REVIEW_TRIAGE_APPROVED: EventSpec(phase='reviewing', step='triage_approved', status='completed', level='phase'),
    EventName.REVIEW_MERGED: EventSpec(phase='reviewing', step='merged', status='completed', level='phase'),
    EventName.REVIEW_CLOSED: EventSpec(phase='reviewing', step='closed', status='failed', level='phase'),
    EventName.REVIEW_COMMENT_ADDED: EventSpec(phase='reviewing', step='comment_added', status='completed', level='phase'),
    EventName.REVIEW_EXCHANGE_STARTED: EventSpec(phase='reviewing', step='started', status='started', level='phase'),
    EventName.REVIEW_EXCHANGE_ROUND_STARTED: EventSpec(phase='reviewing', step='round_started', status='completed', level='phase'),
    EventName.REVIEW_EXCHANGE_ROUND_COMPLETED: EventSpec(phase='reviewing', step='round_completed', status='completed', level='phase'),
    EventName.REVIEW_EXCHANGE_COMPLETED: EventSpec(phase='reviewing', step='completed', status='completed', level='phase'),
    EventName.REVIEW_EXCHANGE_FAILED: EventSpec(phase='reviewing', step='failed', status='completed', level='phase'),
    EventName.REVIEW_EXCHANGE_ROLE_PROMPTED: EventSpec(phase='reviewing', step='role_prompted', status='completed', level='phase'),
    EventName.REVIEW_EXCHANGE_ROLE_FEEDBACK: EventSpec(phase='reviewing', step='role_feedback', status='completed', level='phase'),
    EventName.REVIEW_EXCHANGE_ROLE_TIMEOUT: EventSpec(phase='reviewing', step='role_timeout', status='failed', level='phase'),

    # ----- Rework cycle -----
    EventName.REWORK_STARTED: EventSpec(phase='rework', step='started', status='started', level='detail'),
    EventName.REWORK_SKIPPED: EventSpec(phase='rework', step='skipped', status='completed', level='detail'),
    EventName.REWORK_LAUNCHING: EventSpec(phase='rework', step='launching', status='started', level='detail'),
    EventName.REWORK_ESCALATING: EventSpec(phase='rework', step='escalating', status='failed', level='detail'),

    # ----- Triage -----
    EventName.TRIAGE_ISSUE_CREATED: EventSpec(phase='system', step='triage.issue_created', status='started', level='detail'),
    EventName.TRIAGE_SKIPPED: EventSpec(phase='system', step='triage.skipped', status='completed', level='detail'),
    EventName.TRIAGE_LAUNCHING: EventSpec(phase='system', step='triage.launching', status='started', level='detail'),
    EventName.TRIAGE_BATCH_TRIGGERED: EventSpec(phase='system', step='triage.batch_triggered', status='started', level='detail'),

    # ----- Cleanup -----
    EventName.CLEANUP_COMPLETED: EventSpec(phase='system', step='cleanup.completed', status='completed', level='detail'),

    # ----- Validation -----
    EventName.VALIDATION_STARTED: EventSpec(phase='orchestrator', step='validation.started', status='started', level='detail'),
    EventName.VALIDATION_COMPLETED: EventSpec(phase='orchestrator', step='validation.completed', status='completed', level='detail'),

    # ----- Provider resilience -----
    EventName.PROVIDER_TRANSIENT_ERROR: EventSpec(phase='system', step='provider.transient_error', status='started', level='detail'),
    EventName.PROVIDER_OUTAGE_ENTERED: EventSpec(phase='system', step='provider.outage_entered', status='started', level='detail'),
    EventName.PROVIDER_RETRY_SCHEDULED: EventSpec(phase='system', step='provider.retry_scheduled', status='started', level='detail'),
    EventName.PROVIDER_RETRY_ATTEMPTED: EventSpec(phase='system', step='provider.retry_attempted', status='started', level='detail'),
    EventName.PROVIDER_OUTAGE_EXITED: EventSpec(phase='system', step='provider.outage_exited', status='started', level='detail'),

    # ----- Configuration -----
    EventName.CONFIG_MERGED: EventSpec(phase='system', step='config.merged', status='completed', level='detail'),

    # ----- Goal Pilot -----
    EventName.GOAL_PILOT_CREATED: EventSpec(phase='system', step='goal_pilot.created', status='completed', level='detail'),
    EventName.GOAL_PILOT_UPDATED: EventSpec(phase='system', step='goal_pilot.updated', status='completed', level='detail'),
    EventName.GOAL_PILOT_ACTION_PROPOSED: EventSpec(phase='system', step='goal_pilot.action_proposed', status='completed', level='detail'),
    EventName.GOAL_PILOT_ACTION_EXECUTED: EventSpec(phase='system', step='goal_pilot.action_executed', status='completed', level='detail'),
    EventName.GOAL_PILOT_ACTION_FAILED: EventSpec(phase='system', step='goal_pilot.action_failed', status='completed', level='detail'),
    EventName.GOAL_PILOT_COMPLETED: EventSpec(phase='system', step='goal_pilot.completed', status='completed', level='detail'),

    # ----- Reconciliation -----
    EventName.RECONCILIATION_CHECKED: EventSpec(phase='system', step='reconciliation.checked', status='completed', level='detail'),
    EventName.RECONCILIATION_WARNING: EventSpec(phase='system', step='reconciliation.warning', status='completed', level='detail'),
    EventName.RECONCILIATION_REQUIRED: EventSpec(phase='system', step='reconciliation.required', status='completed', level='detail'),
    EventName.ISSUE_PAUSED_RECONCILE: EventSpec(phase='in_progress', step='paused_reconcile', status='completed', level='phase'),
    EventName.HISTORY_RECONCILED: EventSpec(phase='system', step='history.reconciled', status='completed', level='detail'),

    # ----- Queue projection -----
    EventName.QUEUE_CHANGED: EventSpec(phase='system', step='queue.changed', status='completed', level='detail'),

    # ----- Labels -----
    EventName.LABELS_SYNCED: EventSpec(phase='system', step='labels.synced', status='completed', level='detail'),
    EventName.LABEL_MUTATION_SUMMARY: EventSpec(phase='system', step='labels.mutation_summary', status='completed', level='detail'),
    EventName.ISSUE_LABELS_CHANGED: EventSpec(phase='in_progress', step='labels_changed', status='completed', level='phase'),
    EventName.PR_VIEW_CHANGED: EventSpec(phase='system', step='pr.view_changed', status='completed', level='detail'),

    # ----- Transitions -----
    EventName.TRANSITION_APPLIED: EventSpec(phase='system', step='transition.applied', status='completed', level='detail'),
    EventName.TRANSITION_REJECTED: EventSpec(phase='system', step='transition.rejected', status='completed', level='detail'),

    # ----- PR scanner -----
    EventName.SCANNER_REVIEWS_FOUND: EventSpec(phase='system', step='scanner.reviews_found', status='completed', level='detail'),
    EventName.SCANNER_REWORKS_FOUND: EventSpec(phase='system', step='scanner.reworks_found', status='completed', level='detail'),

    # ----- Dependencies evaluation -----
    EventName.DEPENDENCIES_EVALUATED: EventSpec(phase='system', step='dependencies.evaluated', status='completed', level='detail'),

    # ----- Stale detection / retry -----
    EventName.STALE_IN_PROGRESS_DETECTED: EventSpec(phase='system', step='stale.in_progress_detected', status='completed', level='detail'),
    EventName.STALE_IN_PROGRESS_CLEARED: EventSpec(phase='system', step='stale.in_progress_cleared', status='completed', level='detail'),
    EventName.RETRY_TRIGGERED: EventSpec(phase='system', step='issue.retry_triggered', status='completed', level='detail'),
    EventName.RETRY_NOOP_SESSION_RUNNING: EventSpec(phase='system', step='issue.retry_noop', status='completed', level='detail'),
    EventName.STARTUP_STALE_CLEARED: EventSpec(phase='system', step='orchestrator.startup_stale_cleared', status='completed', level='detail'),
    EventName.PERSISTENT_STALE_DETECTED: EventSpec(phase='system', step='stale.persistent_detected', status='completed', level='detail'),

    # ----- Observation -----
    EventName.OBSERVATION_COMPLETION_DETECTED: EventSpec(phase='in_progress', step='completion_detected', status='completed', level='detail'),
    EventName.OBSERVATION_RESULT: EventSpec(phase='in_progress', step='result', status='completed', level='detail'),

    # ----- Publish jobs -----
    EventName.PUBLISH_JOB_QUEUED: EventSpec(phase='system', step='publish_job.queued', status='completed', level='detail'),
    EventName.PUBLISH_JOB_STARTED: EventSpec(phase='system', step='publish_job.started', status='started', level='detail'),
    EventName.PUBLISH_JOB_PUSH_STARTED: EventSpec(phase='system', step='publish_job.push_started', status='completed', level='detail'),
    EventName.PUBLISH_JOB_PUSH_COMPLETED: EventSpec(phase='system', step='publish_job.push_completed', status='completed', level='detail'),
    EventName.PUBLISH_JOB_PR_CREATED: EventSpec(phase='system', step='publish_job.pr_created', status='completed', level='detail'),
    EventName.PUBLISH_JOB_VALIDATION_STARTED: EventSpec(phase='system', step='publish_job.validation_started', status='completed', level='detail'),
    EventName.PUBLISH_JOB_VALIDATION_COMPLETED: EventSpec(phase='system', step='publish_job.validation_completed', status='completed', level='detail'),
    EventName.PUBLISH_JOB_SUCCEEDED: EventSpec(phase='system', step='publish_job.succeeded', status='completed', level='detail'),
    EventName.PUBLISH_JOB_FAILED: EventSpec(phase='system', step='publish_job.failed', status='completed', level='detail'),
    EventName.PUBLISH_FAILED: EventSpec(phase='orchestrator', step='publish.failed', status='failed', level='detail'),

    # ----- Resolver -----
    EventName.RESOLVER_DUPLICATE_EXTERNAL_ID: EventSpec(phase='system', step='resolver.duplicate_external_id', status='completed', level='detail'),

    # ----- E2E test runner -----
    EventName.E2E_AUTO_TRIGGERED: EventSpec(phase='teardown', step='auto_triggered', status='active', level='info'),
    EventName.E2E_STARTED: EventSpec(phase='teardown', step='started', status='active', level='info'),
    EventName.E2E_PROGRESS: EventSpec(phase='teardown', step='progress', status='active', level='info'),
    EventName.E2E_COMPLETED: EventSpec(phase='teardown', step='completed', status='active', level='info'),
    EventName.E2E_FAILED: EventSpec(phase='teardown', step='failed', status='active', level='info'),
    EventName.E2E_STOPPED: EventSpec(phase='teardown', step='stopped', status='active', level='info'),

    # ----- Claim / lease lifecycle -----
    EventName.CLAIM_ATTEMPTED: EventSpec(phase='system', step='claim.attempted', status='completed', level='detail'),
    EventName.CLAIM_ACQUIRED: EventSpec(phase='system', step='claim.acquired', status='completed', level='detail'),
    EventName.CLAIM_CONTESTED: EventSpec(phase='system', step='claim.contested', status='completed', level='detail'),
    EventName.CLAIM_CONVERGED: EventSpec(phase='system', step='claim.converged', status='completed', level='detail'),
    EventName.CLAIM_LOST: EventSpec(phase='system', step='claim.lost', status='completed', level='detail'),
    EventName.CLAIM_LOST_BEFORE_WRITE: EventSpec(phase='system', step='claim.lost_before_write', status='completed', level='detail'),
    EventName.CLAIM_EXPIRED: EventSpec(phase='system', step='claim.expired', status='completed', level='detail'),
    EventName.CLAIM_RENEWED: EventSpec(phase='system', step='claim.renewed', status='completed', level='detail'),
    EventName.CLAIM_RELEASED: EventSpec(phase='system', step='claim.released', status='completed', level='detail'),
    EventName.CLAIM_STALE_DETECTED: EventSpec(phase='system', step='claim.stale_detected', status='completed', level='detail'),
}


def spec_for(event_name: str | EventName) -> EventSpec | None:
    """Return the `EventSpec` for an event name, or None if not registered.

    Accepts either an `EventName` enum or its string value. Returns None
    rather than raising so callers can decide whether to fall back to
    legacy projection logic during the migration window.
    """
    if isinstance(event_name, EventName):
        return EVENT_SPEC.get(event_name)
    try:
        return EVENT_SPEC[EventName(event_name)]
    except (ValueError, KeyError):
        return None
