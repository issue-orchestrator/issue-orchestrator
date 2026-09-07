"""Settle exact manual publication through existing completion effect capabilities."""

import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..domain.completion_processing import ProcessingResult
from ..domain.manual_publication import PreparedManualPublication
from ..domain.validated_head_publication import PublishValidatedHeadOutcome
from ..domain.validated_work import PublishValidatedHeadStatus
from ..ports.session_output import SessionOutput
from .completion_ports import LabelAdapter
from .completion_pr_labels import apply_pr_labels
from .completion_result_artifacts import build_processing_result, EmitCompletionEvent, PostIssueComment
from .completion_types import ERROR_PREFIX_CREATE_PR
from .review_publish_pipeline import PublishPipelinePlan


def settle_manual_publication(
    prepared: PreparedManualPublication, publication: PublishValidatedHeadOutcome, *,
    session_output: SessionOutput, labels: LabelAdapter,
    finalize_review_exchange: Callable[..., None],
    execute_planned_actions: Callable[..., tuple[str | None, str | None, bool]],
    emit_completion_event: EmitCompletionEvent, post_issue_comment: PostIssueComment,
    cleanup_completion_record: Callable[[Path, str | None, int], None],
) -> ProcessingResult:
    started_at = time.monotonic()
    command = prepared.command
    actions = list(prepared.actions_taken)
    errors: list[str] = []
    details: list[dict[str, Any]] = []
    completed = prepared.review_exchange_completed
    if publication.status in {
        PublishValidatedHeadStatus.PUBLISHED, PublishValidatedHeadStatus.ALREADY_AT_TARGET,
    }:
        if publication.pr_number is None or publication.pr_url is None or publication.pr_head_sha != command.target_head_sha or publication.observed_remote_head_sha != command.target_head_sha:
            raise ValueError("manual settlement requires exact validated PR identity")
        actions.append(f"Published validated head {command.target_head_sha}")
        if apply_pr_labels(pr_number=publication.pr_number, record=prepared.record, labels=labels,
                           actions_taken=actions, errors=errors):
            if prepared.exchange_mode in {"via-mcp", "via-local-loop"} and prepared.exchange_result is not None:
                finalize_review_exchange(
                    issue_number=command.issue_number, pr_number=publication.pr_number,
                    exchange_mode=prepared.exchange_mode, exchange_result=prepared.exchange_result,
                    actions_taken=actions, run_assets=prepared.exchange_result.run_assets,
                )
                completed = True
            execute_planned_actions(
                plan=PublishPipelinePlan(prepared.remaining_actions, False),
                worktree=command.source_workspace, record=prepared.record,
                issue_number=command.issue_number, issue_title=prepared.issue_title,
                label_target=prepared.label_target, branch=command.branch_name,
                session_name=prepared.run.session_name, agent_label=prepared.agent_label,
                actions_taken=actions, errors=errors, error_details=details,
                exchange_mode=prepared.exchange_mode, exchange_result=prepared.exchange_result,
                review_exchange_completed=completed,
            )
    else:
        errors.append(f"{ERROR_PREFIX_CREATE_PR}: {publication.message}")
    result = build_processing_result(
        session_output=session_output, worktree=command.source_workspace,
        record=prepared.record, session_name=prepared.run.session_name,
        issue_number=command.issue_number, issue_title=prepared.issue_title,
        branch=command.branch_name, pr_url=publication.pr_url,
        review_exchange_completed=completed, actions_taken=actions, errors=errors,
        error_details=details, total_duration=time.monotonic() - started_at, completion_path=None,
        intake_receipt=prepared.receipt,
        preserved_completion_path=str(prepared.completion_artifact.path),
        run_assets=prepared.run, emit_completion_event=emit_completion_event,
        post_issue_comment=post_issue_comment,
        cleanup_completion_record_fn=cleanup_completion_record,
    )
    result.review_exchange_halted |= prepared.review_exchange_halted
    return result

