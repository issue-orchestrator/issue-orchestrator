"""The Plan/Apply vocabulary root: :class:`ActionType` and :class:`Action`.

Split out of ``actions`` so the tech-lead action module can extend the base
WITHOUT a circular import: ``action_base`` depends on nothing in the control
layer, ``tech_lead_actions`` imports it, and ``actions`` imports both and
re-exports everything. Importers keep using ``control.actions``.
"""

from dataclasses import dataclass, fields
from enum import Enum
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .reconciliation import ExpectedState


class ActionType(Enum):
    """Types of actions the orchestrator can take."""

    # Label operations
    ADD_LABEL = "add_label"
    REMOVE_LABEL = "remove_label"
    SYNC_LABELS = "sync_labels"
    SHED_RECOVERED_WORKFLOW_LABELS = "shed_recovered_workflow_labels"

    # Provider outage impact: owns the blocked-label transition *and* the
    # durable issue-scoped record of it (see control/provider_impact.py).
    APPLY_PROVIDER_IMPACT = "apply_provider_impact"

    # Session operations
    LAUNCH_SESSION = "launch_session"
    LAUNCH_VALIDATION_RETRY = "launch_validation_retry"
    STOP_SESSION = "stop_session"

    # GitHub operations
    CREATE_PR = "create_pr"
    ADD_COMMENT = "add_comment"
    SUPERSEDE_PR = "supersede_pr"
    CLOSE_ISSUE = "close_issue"
    SET_ISSUE_STATE = "set_issue_state"

    # Worktree operations
    CREATE_WORKTREE = "create_worktree"
    REMOVE_WORKTREE = "remove_worktree"

    # Queue operations
    QUEUE_REVIEW = "queue_review"
    QUEUE_RETROSPECTIVE_REVIEW = "queue_retrospective_review"
    QUEUE_REWORK = "queue_rework"
    QUEUE_TECH_LEAD = "queue_tech_lead"
    # Withdraw a queued tech-lead investigation whose subject stopped being
    # worth investigating (#6994 launch-time revalidation). The queue is the
    # only durable record of an investigation, so a run that can no longer
    # launch must be removed rather than left queued forever.
    DROP_TECH_LEAD = "drop_tech_lead"

    # Issue creation
    CREATE_TECH_LEAD_ISSUE = "create_tech_lead_issue"
    REPORT_BUDGETED_VALIDATION = "report_budgeted_validation"

    # Gated act-level proposal issue: create + record the stored op (#6778)
    CREATE_TECH_LEAD_PROPOSAL_ISSUE = "create_tech_lead_proposal_issue"

    # Pattern case-file issue: create + record the pattern ledger row (#6781)
    CREATE_TECH_LEAD_CASE_FILE_ISSUE = "create_tech_lead_case_file_issue"

    # Tech Lead decision proposals (event-only surfacing, ADR-0031)
    SURFACE_TECH_LEAD_PROPOSAL = "surface_tech_lead_proposal"
    REQUIRE_TECH_LEAD_INVESTIGATION = "require_tech_lead_investigation"

    # Act-level tech_lead execution: scratch reset via the reset owner (#6764)
    RESET_RETRY_ISSUE = "reset_retry_issue"

    # Release a published PR's review: pr-pending on, then the stale
    # blocked-failed block off, custody rechecked at apply time (#7293)
    RELEASE_PUBLISHED_REVIEW = "release_published_review"

    # Act-level tech_lead execution: terminate issue runtime (#6778, approved ops)
    KILL_HUNG_SESSION = "kill_hung_session"
    REQUEST_REWORK = "request_rework"
    RECOVER_VALIDATED_WORK = "recover_validated_work"
    # Release a review withheld only by the issue's own block (#7399)
    RELEASE_WITHHELD_REVIEW = "release_withheld_review"
    # Carry out a decision the operator approved: file its follow-ups, post
    # it on the item, and retry the item as the operator would (#7593)
    APPLY_OPERATOR_DECISION = "apply_operator_decision"
    # Decide a needs-human work block in the operator's stead (#7658)
    RESOLVE_BLOCK = "resolve_block"

    # Confirm-and-discard terminal gated-proposal ledger rows (#6779 R7/R10):
    # the single mutating boundary for proposal-op cleanup, applied off the
    # read-only fact path so fact gathering stays side-effect free.
    RECOVER_TECH_LEAD_PROPOSAL = "recover_tech_lead_proposal"
    DISCARD_TERMINAL_TECH_LEAD_PROPOSAL_OPS = "discard_terminal_tech_lead_proposal_ops"
    # Approval-label transitions of a tech-lead proposal (#7763): admit a
    # verified approval, reject an approval that does not count, restore the
    # waiting state. Only the approval owner writes these labels.
    SETTLE_PROPOSAL_APPROVAL = "settle_proposal_approval"

    # Repeat pattern observation: evidence comment + durable count (#6781/#6957)
    APPEND_PATTERN_OBSERVATION = "append_pattern_observation"

    # Terminal disposition of a completed failure investigation (#6971):
    # publish the wait state on the diagnosed issue + record the durable
    # tracker binding that transfers stuck-sweep ownership.
    RECORD_TECH_LEAD_DISPOSITION = "record_tech_lead_disposition"
    ESCALATE_TECH_LEAD_DISPOSITION = "escalate_tech_lead_disposition"

    # Per-action tech-lead charter decisions (#7330): persisted for explanation.
    RECORD_TECH_LEAD_CHARTER_DECISIONS = "record_tech_lead_charter_decisions"
    # An effect that must not run without its charter decision on the record.
    APPLY_CHARTER_AUDITED_ACTION = "apply_charter_audited_action"

    # Finding promotion (#6957): file a case file's diagnosis as a gated
    # runnable issue in the routed repo, report later evidence onto that one
    # issue, and settle it when it goes terminal.
    PROMOTE_TECH_LEAD_FINDING = "promote_tech_lead_finding"
    REPORT_PROMOTED_FINDING_EVIDENCE = "report_promoted_finding_evidence"
    SETTLE_TECH_LEAD_PROMOTION = "settle_tech_lead_promotion"

    # Escalation
    ESCALATE_TO_HUMAN = "escalate_to_human"

    # Merge queue (optional GitHub Merge Queue integration)
    ENQUEUE_TO_MERGE_QUEUE = "enqueue_to_merge_queue"

    # Cleanup operations
    CLEANUP_SESSION = "cleanup_session"

    # History operations
    RECONCILE_HISTORY_ENTRY = "reconcile_history_entry"

    # Terminal recovery (shed transient labels, then finalize history)
    RECOVER_TERMINAL_ISSUE = "recover_terminal_issue"


#: Fields that say why an action exists, not what it does: free-text audit
#: prose, and the charter decisions it is linked back to.
_NOT_LIVENESS_FACTS = frozenset({"reason", "charter_decisions"})


@dataclass(frozen=True)
class Action:
    """Base action class.

    All actions are immutable data objects that describe an intended change.
    The actual execution is handled by the ActionApplier.

    Mutating actions (those that write to GitHub) should have `expected` set
    to enable optimistic concurrency control. Before applying the mutation,
    the applier verifies current state satisfies `expected`. If not, it raises
    ReconciliationRequired instead of applying the mutation.
    """

    action_type: ActionType
    reason: str = ""  # Why this action is being taken (for audit)
    # Expected state constraints for reconciliation (required for mutating actions)
    expected: Optional["ExpectedState"] = None
    #: The tech-lead charter decisions (``decision_key``) this action is an
    #: effect of, when the charter let them execute directly (#7362). The
    #: completion owner links the action's real result back to each one.
    charter_decisions: tuple[str, ...] = ()

    def __post_init__(self):
        # Validate that subclasses set the correct action_type
        pass

    def liveness_resolves_subject(self) -> bool:
        """Whether this action, once it succeeds, settles every park on its subject.

        True only for an action that ends the subject's work and clears its
        needs-human block itself, as an operator's Retry or Dismiss does.
        """
        return False

    def liveness_facts(self) -> object | None:
        """The facts this action was derived from, for the action liveness owner (#7350).

        Two attempts with equal facts are the same question asked again, so
        their failures spend one bounded budget. The default is every field but
        the free-text ``reason`` (audit prose that often carries a count or a
        time). An action overrides this when a field changes every tick without
        the underlying facts changing - a sample time, a countdown - because
        such a field would give each failure a fresh budget and defeat the bound.
        The charter decisions an action is linked to are not facts either.

        ``None`` means another owner already governs this action's retries with
        facts the liveness owner cannot see, so it is neither gated nor counted.
        """
        return {
            item.name: getattr(self, item.name)
            for item in fields(self)
            if item.name not in _NOT_LIVENESS_FACTS
        }
