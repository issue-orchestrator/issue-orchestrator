"""Apply live completion policy to authenticated retained publication custody."""

from collections.abc import Callable
from typing import Protocol

from ..domain.completion_intake import CompletionIntakeError
from ..domain.completion_processing import ProcessingResult
from ..domain.completion_validation_policy import require_current_completion_validator
from ..domain.models import CompletionRecord
from ..domain.prepared_completion import PreparedCompletionEvidence
from ..domain.publication_workspace import PublicationWorkspace
from ..domain.registered_completion import CompletionProcessingPolicy
from ..domain.session_run import SessionRunAssets
from ..domain.validated_work_capture import candidate_key
from ..infra.config import Config
from .completion_pr_labels import reserved_pr_label_error
from .needs_human_block import SharedNeedsHumanBlock
from .completion_ports import GitAdapter
from .completion_preparation import PreparedActionPlan, PreparedCompletion, context_from_prepared_evidence
from .completion_record_validation import CompletionRecordValidator
from .publication_source_guards import PublicationSourceGuards
from .review_publish_pipeline import resolve_review_publish_pipeline
from .tech_lead_session_policy import resolve_tech_lead_completion_actions


class CompletionRoleCheck(Protocol):
    def __call__(self, *, record: CompletionRecord, processing_policy: CompletionProcessingPolicy,
                 issue_number: int, run_assets: SessionRunAssets) -> ProcessingResult | None: ...


def prepare_retained_completion(
    evidence: PreparedCompletionEvidence, workspace: PublicationWorkspace, *,
    config: Config | None, record_validator: CompletionRecordValidator,
    reject_role: CompletionRoleCheck, git: GitAdapter, base_branch: Callable[[], str],
    source_guards: PublicationSourceGuards, human_block: SharedNeedsHumanBlock,
) -> PreparedCompletion | ProcessingResult:
    """Keep original run authority and use shared checks without restarting review."""
    issue_number = workspace.key.issue_number
    if candidate_key(evidence, issue_number) != workspace.key:
        raise CompletionIntakeError("retained completion does not bind the publication workspace")
    if config is None:
        raise CompletionIntakeError("retained publication requires configured validation policy")
    context = context_from_prepared_evidence(evidence, evidence.entry.receipt, evidence.run.run)
    policy = record_validator.resolve_processing_policy(context, issue_number, None, None)
    record = context.record
    reserved = reserved_pr_label_error(record, human_block)
    if reserved is not None:
        return ProcessingResult(False, reserved, errors=[reserved], processing_policy=policy)
    rejection = reject_role(
        record=record, processing_policy=policy, issue_number=issue_number,
        run_assets=evidence.run.run,
    )
    if rejection is not None:
        return rejection.with_processing_policy(policy)
    if policy.is_tech_lead:
        shaping = resolve_tech_lead_completion_actions(
            worktree=workspace.checkout, record=record, git_adapter=git,
            base_branch=base_branch,
        )
        if shaping is not None:
            return shaping.with_processing_policy(policy)
    validation = config.validation.quick
    require_current_completion_validator(
        evidence.validation.validator_digest, validation.cmd, validation.timeout_seconds,
    )
    state = record_validator.validate_worktree_state(
        workspace.checkout, record, publication_branch=workspace.key.branch_name,
    )
    reason = state.reason if not state.ok else source_guards.check(workspace.checkout)
    if reason:
        return ProcessingResult(False, reason, errors=[reason], processing_policy=policy)
    # The admitted recovery disposition routes this head to PR review.
    # No exchange start/resume path runs on preserved evidence.
    mode = "via-draft-pr"
    plan = resolve_review_publish_pipeline(mode).plan(record.requested_actions)
    return PreparedCompletion(
        record, evidence.run.run.session_name, policy, workspace.key.branch_name,
        str(workspace.artifacts.completion), PreparedActionPlan(plan, mode, None, False, False),
    )
