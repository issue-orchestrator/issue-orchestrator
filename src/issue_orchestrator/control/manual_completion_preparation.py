"""Bind a manual retry to trusted custody before shared completion preparation."""

from dataclasses import replace
from pathlib import Path

from ..domain.completion_intake import CompletionIntakeError
from ..domain.completion_processing import ProcessingResult
from ..domain.manual_publication import PreparedManualPublication
from ..domain.models import CompletionOutcome, RequestedAction
from ..domain.publish_retry import PublishRetryLocators
from ..domain.validated_head_publication import (
    PublicationContent, PublishValidatedHeadCommand, PublishValidatedHeadOutcome,
    RemoteHeadExpectation, is_protected_publication_branch,
)
from ..ports.completion_intake import CompletionIntakeRuntime
from ..ports.working_copy import WorkingCopy
from .completion_processor import CompletionProcessor


class ManualCompletionPreparation:
    def __init__(self, *, intake: CompletionIntakeRuntime, completion: CompletionProcessor,
                 working_copy: WorkingCopy, repo_slug: str) -> None:
        self._intake = intake
        self._completion = completion
        self._working_copy = working_copy
        self._repo_slug = repo_slug

    def prepare_manual_publication(
        self, locators: PublishRetryLocators, issue_title: str,
    ) -> PreparedManualPublication | ProcessingResult:
        receipt = locators.intake_receipt
        if receipt is None:
            return self._refusal("Manual publication requires the exact processed intake receipt")
        try:
            evidence = self._intake.prepare_receipt_for_issue(receipt, locators.run_assets, locators.issue_number)
        except CompletionIntakeError as exc:
            return replace(self._refusal(str(exc)), intake_receipt=receipt)
        worktree = evidence.run.run.worktree_path
        branch = evidence.run.branch_name
        if (evidence.run.session_key.stable_id() != locators.session_key
                or evidence.run.session_key.issue.scope() != self._repo_slug
                or worktree != Path(locators.worktree_path)
                or branch != locators.branch_name or branch is None
                or is_protected_publication_branch(branch)):
            return replace(self._refusal(
                "Retry locators differ from the allocated run and branch, or target a protected branch"
            ), intake_receipt=receipt)
        target = evidence.validation.head_sha
        if not self._source_matches(worktree, branch, target):
            return replace(self._refusal("Retry source must be clean and remain at the validated head and allocated branch"), intake_receipt=receipt)
        actions: list[str] = []
        errors: list[str] = []
        prepared = self._completion.prepare_completion(
            worktree, locators.issue_number, issue_title, run_assets=evidence.run.run,
            completion_path=None, agent_label=None, intake_receipt=receipt,
            actions_taken=actions, errors=errors, prepared_evidence=evidence,
        )
        if isinstance(prepared, ProcessingResult):
            return replace(prepared, intake_receipt=receipt)
        if prepared.actions.halted or errors:
            return ProcessingResult(False, "Completion preparation halted manual publication",
                processing_policy=prepared.processing_policy,
                errors=errors, review_exchange_completed=prepared.actions.review_exchange_completed,
                review_exchange_halted=True, intake_receipt=receipt)
        if (prepared.record.outcome is not CompletionOutcome.COMPLETED
                or RequestedAction.CREATE_PR not in prepared.actions.plan.ordered_actions
                or prepared.agent_label is None):
            return replace(self._refusal("Prepared completion does not authorize PR publication with an allocated agent role"), intake_receipt=receipt, processing_policy=prepared.processing_policy)
        publication = self._completion.prepare_pull_request(
            worktree=worktree, record=prepared.record, issue_number=locators.issue_number,
            issue_title=issue_title, branch=branch, agent_label=prepared.agent_label, errors=errors,
            exchange_mode=prepared.actions.exchange_mode, exchange_result=prepared.actions.exchange_result,
        )
        if publication is None or errors:
            return ProcessingResult(False, "PR preparation refused manual publication", errors=errors,
                                    processing_policy=prepared.processing_policy,
                                    intake_receipt=receipt)
        # Review/pre-push preparation may have changed the source. Its new HEAD
        # is never substituted for the receipt's immutable validated target.
        if prepared.branch != branch or not self._source_matches(worktree, branch, target):
            return replace(self._refusal("Completion preparation changed the validated source; publication refused"), intake_receipt=receipt, processing_policy=prepared.processing_policy)
        command = PublishValidatedHeadCommand(
            issue_number=locators.issue_number, repo_slug=self._repo_slug,
            branch_name=branch, target_head_sha=target,
            expectation=RemoteHeadExpectation.UNCONSTRAINED, expected_remote_head_sha=None,
            source_workspace=worktree, pr_number=locators.pr_number,
            pr_base_branch=publication.base_branch,
            content=PublicationContent(publication.title, publication.body,
                                       publication.exchange_mode not in {"via-mcp", "via-local-loop"}),
        )
        return PreparedManualPublication(
            command=command, receipt=receipt, run=evidence.run.run,
            completion_artifact=self._intake.completion_artifact(receipt, evidence.run.run),
            record=prepared.record, issue_title=issue_title, processing_policy=prepared.processing_policy,
            label_target=locators.pr_number or locators.issue_number,
            actions_taken=tuple(actions), remaining_actions=tuple(action for action in prepared.actions.plan.ordered_actions
                if action not in {RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR}),
            exchange_mode=publication.exchange_mode, exchange_result=prepared.actions.exchange_result,
            review_exchange_completed=prepared.actions.review_exchange_completed or locators.review_exchange_completed,
            review_exchange_halted=locators.review_exchange_halted,
        )

    def settle_manual_publication(
        self, prepared: PreparedManualPublication, publication: PublishValidatedHeadOutcome,
    ) -> ProcessingResult:
        return self._completion.settle_manual_publication(prepared, publication)

    def _source_matches(self, worktree: Path, branch: str, target: str) -> bool:
        return (self._working_copy.get_current_branch(worktree) == branch
                and self._working_copy.get_head_sha(worktree) == target
                and not self._working_copy.has_uncommitted_changes(worktree))

    @staticmethod
    def _refusal(message: str) -> ProcessingResult:
        return ProcessingResult.for_intake_refusal(CompletionIntakeError(message))
