"""Translate exact retained custody into the shared publication content path."""

from ..domain.completion_intake import CompletionIntakeError
from ..domain.completion_processing import ProcessingResult
from ..domain.publication_workspace import PublicationWorkspace
from ..domain.recovery_publication import PreparedRecoveryPublication
from ..domain.validated_head_publication import PublicationContent, PublishValidatedHeadCommand, RemoteHeadExpectation
from ..domain.validated_work import EvidenceRole, RemoteBaselineStatus, ReviewDisposition
from ..domain.validated_work_store import EvidenceRow
from ..ports.completion_intake import CompletionIntakeLedger
from ..ports.working_copy import WorkingCopy
from .completion_processor import CompletionProcessor


class RetainedCompletionPreparation:
    def __init__(self, *, intake: CompletionIntakeLedger, completion: CompletionProcessor,
                 working_copy: WorkingCopy, repo_slug: str) -> None:
        self._intake = intake
        self._completion = completion
        self._working_copy = working_copy
        self._repo = repo_slug

    def prepare(self, evidence: EvidenceRow, workspace: PublicationWorkspace,
                issue_title: str) -> PreparedRecoveryPublication | ProcessingResult:
        admitted = evidence.admission.evidence
        key = admitted.identity.key
        if (key.repo_slug != self._repo or workspace.key != key
                or workspace.evidence_id != admitted.evidence_id
                or evidence.role is not EvidenceRole.CURRENT or evidence.released_at):
            raise CompletionIntakeError("retained publication requires current exact repository evidence")
        # Intake proves this immutable identity, including the captured review
        # disposition; copying an approval into an envelope cannot grant it.
        completion = self._intake.prepare_evidence(admitted)
        if admitted.identity.review_disposition is not ReviewDisposition.ROUTE_TO_PR_REVIEW:
            raise CompletionIntakeError("retained publication requires authenticated PR-review routing")
        self._require_source(workspace)
        prepared = self._completion.prepare_retained_completion(completion, workspace)
        if isinstance(prepared, ProcessingResult):
            return prepared
        errors: list[str] = []
        publication = self._completion.prepare_pull_request(
            worktree=workspace.checkout, record=prepared.record, issue_number=key.issue_number,
            issue_title=issue_title, branch=key.branch_name, agent_label=prepared.agent_label,
            errors=errors, exchange_mode=prepared.actions.exchange_mode,
            exchange_result=prepared.actions.exchange_result,
        )
        if publication is None or errors:
            return ProcessingResult(False, "Retained PR preparation refused publication", errors=errors,
                                    processing_policy=prepared.processing_policy)
        self._require_source(workspace)
        observation = admitted.observations
        if observation.remote_baseline_status is not RemoteBaselineStatus.OBSERVED:
            raise CompletionIntakeError(
                "retained publication requires an observed remote branch baseline"
            )
        command = PublishValidatedHeadCommand(
            issue_number=key.issue_number, repo_slug=key.repo_slug,
            branch_name=key.branch_name, target_head_sha=key.validated_head_sha,
            expectation=RemoteHeadExpectation.ABSENT if observation.expected_remote_head_sha is None else RemoteHeadExpectation.EXACT,
            expected_remote_head_sha=observation.expected_remote_head_sha,
            source_workspace=workspace.checkout, pr_number=observation.pr_number,
            pr_base_branch=publication.base_branch,
            content=PublicationContent(publication.title, publication.body, True),
        )
        return PreparedRecoveryPublication(command, workspace, completion, prepared.processing_policy,
                                           admitted.identity.review_disposition)

    def _require_source(self, workspace: PublicationWorkspace) -> None:
        if (self._working_copy.get_head_sha(workspace.checkout) != workspace.key.validated_head_sha
                or self._working_copy.has_uncommitted_changes(workspace.checkout)):
            raise CompletionIntakeError("retained publication source differs from its validated head")
