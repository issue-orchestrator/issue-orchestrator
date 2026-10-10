"""Composition wiring for the tech_lead act-level executors (#6764, #6778).

Production boundary: the reset executor's ``run_reset`` reuses
``reset_and_retry_issue`` — the exact per-issue pipeline behind the
dashboard's ``/api/reset-retry`` endpoint — with ``from_scratch=True``
(ADR-0031's action vocabulary defines ``reset_retry`` as reset-and-retry
FROM SCRATCH). Nothing about the reset boundary (runtime termination, PR
superseding, branch deletion, label/history/timeline clearing,
pending-label relaunch marking, queue re-insertion) is reimplemented here.

The kill executor's ``run_kill`` (#6778) uses the generation-bound lifecycle
owner: it verifies and stops the exact typed terminal/run pair observed at
tech-lead launch, then tears down the issue's hidden exchange/publish owners,
WITHOUT the reset that follows it.

Lives outside ``bootstrap`` so the composition root stays wiring-only; the
closures read live orchestrator state at EXECUTION time, which is why these
executors can only be wired after the orchestrator is constructed.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

from ..control.published_review_release import ReviewReleaseWrites, published_review_release_for
from ..control.issue_work_claims import claims_on_issue
from ..control.session_history import SessionHistoryOwner
from ..infra.repo_scope import require_repo
from ..control.tech_lead_review_release import TechLeadReviewReleaseExecutor
from ..control.tech_lead_decision_steps import DecisionStepsOwner
from ..control.tech_lead_operator_decision import OperatorDecisionExecutor
from ..control.tech_lead_block_resolution import TechLeadBlockResolutionExecutor
from ..control.blocked_item_triage import agent_questions_in
from ..domain.block_resolution import RESOLUTION_MARKER_PREFIX
from ..control.queue_cache import QueueCache
from ..control.tech_lead_kill_session import (
    KillSessionRunOutcome,
    TechLeadKillSessionExecutor,
)
from ..control.tech_lead_reset_retry import (
    ResetRetryRunOutcome,
    TechLeadResetRetryExecutor,
)
from ..control.tech_lead_validated_work_recovery import (
    TechLeadValidatedWorkRecoveryExecutor,
)

if TYPE_CHECKING:
    from ..domain.tech_lead_session import TechLeadSessionGeneration
    from ..infra.orchestrator import Orchestrator
    from ..ports import RepositoryHost
    from ..ports.issue import Issue

# Event provenance for ISSUE_UNBLOCKED emitted by an agent-authorized reset,
# distinguishing it from the operator-clicked "web.reset-retry" source.
TECH_LEAD_RESET_RETRY_EVENT_SOURCE = "tech_lead.reset_retry"


def build_tech_lead_validated_work_recovery_executor(
    orchestrator: "Orchestrator",
) -> TechLeadValidatedWorkRecoveryExecutor:
    """Bind the tech-lead command to the process-shared recovery owner."""
    deps = orchestrator.deps
    return TechLeadValidatedWorkRecoveryExecutor(
        events=deps.events,
        preflight=deps.validated_work_recovery.preflight,
        recover=lambda command: deps.validated_work_recovery.recover(
            command, orchestrator.state
        ),
    )


def build_tech_lead_review_release_executor(
    orchestrator: "Orchestrator", host: "RepositoryHost"
) -> TechLeadReviewReleaseExecutor:
    """Bind ``release_withheld_review`` (#7399) to the owner of each precondition.

    Every read is the live owner's own: the runtime lifecycle's probe and
    published-review custody, the durable claim ledger, the session history,
    the review scanner's branch map, fresh GitHub reads, and the release writes
    the stuck sweep uses, applied through the guarded applier.
    """
    deps = orchestrator.deps
    labels = deps.label_manager
    history = SessionHistoryOwner(lambda: orchestrator.state.session_history)
    return TechLeadReviewReleaseExecutor(
        events=deps.events,
        config=orchestrator.config,
        labels=labels,
        gates=deps.human_gates,
        read_issue=host.get_issue,
        list_open_prs=host.list_open_prs_complete,
        read_pr=host.get_pr,
        issue_branches=deps.pr_scanner.load_issue_branches,
        review_admission=deps.pr_scanner.review_admission,
        read_checks=host.read_pr_status_check_rollup,
        runtime_activity=deps.runtime_lifecycle.probe,
        claims_on_issue=lambda number: claims_on_issue(deps.pending_work_claims, number),
        failures_not_before=history.failures_not_before,
        custody=deps.runtime_lifecycle.published_review,
        reviews_discoverable=lambda: deps.pr_scanner.reviews_discoverable,
        writes=ReviewReleaseWrites(
            labels=labels, apply=deps.action_applier.apply,
            review_label=orchestrator.config.code_review_label or "",
        ),
        repo_slug=require_repo(orchestrator.config),
    )


def build_tech_lead_operator_decision_executor(
    orchestrator: "Orchestrator", host: "RepositoryHost"
) -> OperatorDecisionExecutor:
    """Bind an approved ``propose_decision`` (#7593) to the operator's own retry.

    Approving a decision is the operator saying "go ahead", so the item is
    retried through the very command the dashboard's Retry runs, under the
    facade's state lock; the follow-ups and the decision comment are
    create-once writes on the repository host.
    """
    deps = orchestrator.deps
    return OperatorDecisionExecutor(
        events=deps.events,
        labels=deps.label_manager,
        read_issue=host.get_issue,
        retry_issue=orchestrator.operator_issue_commands.retry,
        unsettleable_holders=lambda number: tuple(
            cause.value for cause in deps.needs_human_block.unsettleable_holders(number)
        ),
        find_issue_by_marker=host.find_issue_by_marker,
        create_issue=host.create_issue,
        comment_marker_present=host.issue_comment_marker_present,
        apply_action=deps.action_applier.apply,
        require_authority=deps.action_applier.require_mutation_authority,
        retries=deps.tech_lead_authority,
        rulings=deps.standing_rulings,
        steps=build_decision_steps_owner(orchestrator, host),
    )


def build_decision_steps_owner(orchestrator: "Orchestrator", host: "RepositoryHost") -> DecisionStepsOwner:
    """Bind a decision's steps beyond its item (#8691) to the owner of each write:
    the guarded applier (comments, closes, PR rework), the standing-rulings
    owner (body blocks), and fresh repository reads and writes behind the
    applier's mutation-authority check."""
    deps = orchestrator.deps
    return DecisionStepsOwner(
        read_issue=host.get_issue,
        read_pr=host.get_pr,
        list_milestones=host.list_milestones,
        set_milestone=host.update_issue_milestone,
        write_body=host.update_issue_body,
        comment_marker_present=host.issue_comment_marker_present,
        apply_action=deps.action_applier.apply,
        require_authority=deps.action_applier.require_mutation_authority,
        rulings=deps.standing_rulings,
        block=deps.needs_human_block,
        repo_slug=require_repo(orchestrator.config),
    )


def build_tech_lead_block_resolution_executor(
    orchestrator: "Orchestrator", host: "RepositoryHost"
) -> TechLeadBlockResolutionExecutor:
    """Bind ``resolve_block`` (#7658) to the owner of each precondition and write.

    The shared block's owner discharges the causes; the runtime probe, claim
    ledger and session history answer whether anything runs or failed since;
    the item's comments carry the durable resolution markers; the timeline
    holds the agent's question the human-only screen reads; and the operator
    commands' requeue makes the item eligible again without a label sweep.
    """
    deps = orchestrator.deps
    history = SessionHistoryOwner(lambda: orchestrator.state.session_history)
    return TechLeadBlockResolutionExecutor(
        events=deps.events,
        labels=deps.label_manager,
        block=deps.needs_human_block,
        gates=deps.human_gates,
        read_issue=host.get_issue,
        read_comment_bodies=lambda number: host.issue_comment_bodies_containing(
            number, RESOLUTION_MARKER_PREFIX
        ),
        # The WHOLE timeline: a person's task asked once is never screened out
        # by later events (the triage agenda's bounded read is for display).
        agent_questions=lambda number: agent_questions_in(deps.timeline_store.read(number)),
        runtime_activity=deps.runtime_lifecycle.probe,
        claims_on_issue=lambda number: claims_on_issue(deps.pending_work_claims, number),
        # The tech lead's own run on a focus issue proposed the decision; it
        # never raises the block the decision is about.
        sessions_not_before=lambda number, instant: history.sessions_not_before(
            number, instant, excluding_agent=orchestrator.config.tech_lead_review_agent
        ),
        published_review=deps.runtime_lifecycle.published_review,
        find_issue_by_marker=host.find_issue_by_marker,
        create_issue=host.create_issue,
        apply_action=deps.action_applier.apply,
        require_authority=deps.action_applier.require_mutation_authority,
        requeue=orchestrator.operator_issue_commands.requeue_resolved,
        discharges=deps.tech_lead_authority,
        index_proposals=deps.tech_lead_authority.proposal_index.index_proposals,
        rulings=deps.standing_rulings,
        steps=build_decision_steps_owner(orchestrator, host),
    )


def build_tech_lead_kill_session_executor(
    orchestrator: "Orchestrator",
) -> TechLeadKillSessionExecutor:
    """Build the production kill_hung_session executor (#6778)."""

    def _run_kill(
        target: "TechLeadSessionGeneration", reason: str
    ) -> KillSessionRunOutcome:
        try:
            result = orchestrator.terminate_issue_session_generation(
                target, reason=reason
            )
        except Exception as e:  # loud failure -> ActionResult.fail upstream
            return KillSessionRunOutcome(success=False, error=str(e))
        if result.stale_reason is not None:
            return KillSessionRunOutcome(
                success=False, stale_reason=result.stale_reason
            )
        assert result.termination is not None
        termination = result.termination
        return KillSessionRunOutcome(
            success=True,
            details={
                "stopped_session_ids": list(termination.stopped_session_ids),
                "cleared_active_session_ids": list(
                    termination.cleared_active_session_ids
                ),
                "cancelled_job_ids": list(termination.cancelled_job_ids),
            },
        )

    return TechLeadKillSessionExecutor(
        events=orchestrator.deps.events,
        run_kill=_run_kill,
        read_generation_stale_reason=orchestrator.issue_session_generation_stale_reason,
    )


def build_tech_lead_reset_retry_executor(
    orchestrator: "Orchestrator",
) -> TechLeadResetRetryExecutor:
    """Build the production executor over the live orchestrator."""
    # Lazy: web_retry_history_routes pulls in the FastAPI routing stack,
    # which composition should not load at module-import time.
    from ..control.maintenance import reset_issue
    from .web_retry_history_routes import (
        reset_and_retry_issue,
    )

    deps = orchestrator.deps
    label_manager = deps.label_manager

    def _run_reset(
        issue_number: int, current_labels: Sequence[str]
    ) -> ResetRetryRunOutcome:
        queue_cache = QueueCache(
            orchestrator.config, orchestrator.state, deps.queue_cache_store
        )
        success_payload, failure_payload = reset_and_retry_issue(
            issue_number=issue_number,
            from_scratch=True,
            pending_label=label_manager.reset_retry_pending,
            scratch_pending_label=label_manager.reset_retry_scratch_pending,
            repository_host=orchestrator.repository_host,
            queue_cache=queue_cache,
            state=orchestrator.state,
            deps=deps,
            config=orchestrator.config,
            reset_issue_fn=reset_issue,
            current_labels=list(current_labels),
            run_locked=orchestrator.run_locked,
            source=TECH_LEAD_RESET_RETRY_EVENT_SOURCE,
        )
        if success_payload is not None:
            return ResetRetryRunOutcome(success=True, details=success_payload)
        error = (failure_payload or {}).get("error") or "unknown reset failure"
        return ResetRetryRunOutcome(
            success=False, error=str(error), details=failure_payload or {},
            stale_reason=(failure_payload or {}).get("stale_reason")
        )

    def _read_issue(issue_number: int) -> "Issue | None":
        return deps.repository_host.get_issue(issue_number)

    return TechLeadResetRetryExecutor(
        events=deps.events,
        label_manager=label_manager,
        read_issue=_read_issue,
        runtime_snapshot=deps.runtime_lifecycle.reset_snapshot,
        run_reset=_run_reset,
        release_review=lambda issue_number: published_review_release_for(
            deps.action_applier, orchestrator.config.code_review_label or ""
        ).release(issue_number),
    )
