"""Historical completion admission with configured fresh validation and no publish authority."""

from hashlib import sha256
from uuid import uuid4
from pathlib import Path

from ..domain.completion_intake import (
    CompletionIntakeError,
    CompletionIntakeEntry,
    CompletionParseStatus,
    OwnedCompletionSubmission,
    SubmissionOrigin,
    SubmitCompletionEvidence,
)
from ..domain.historical_intake import (
    HistoricalIntakeCommand,
    HistoricalIntakeOutcome,
    HistoricalIntakeParked,
    HistoricalIntakeRefusal,
    HistoricalIntakeRefused,
)
from ..domain.issue_key import GitHubIssueKey
from ..domain.issue_run_allocation import IssueRunAllocation
from ..domain.historical_intake_policy import (
    repository_refusal,
    completion_refusal,
    failed_historical_validation,
)
from ..domain.session_key import SessionKey, TaskKind
from ..ports.completion_intake import (
    CompletionEvidenceValidator,
    CompletionIntakeLedger,
)
from ..ports.historical_intake import HistoricalIntakeWorkspace
from ..ports.issue_run_allocator import IssueRunAllocator
from ..ports.working_copy import WorkingCopy
from ..ports.git import GitError


class HistoricalCompletionIntake:
    def __init__(
        self,
        *,
        repo_slug: str | None,
        repo_root: Path,
        working_copy: WorkingCopy,
        workspace: HistoricalIntakeWorkspace,
        allocator: IssueRunAllocator,
        ledger: CompletionIntakeLedger,
        validator: CompletionEvidenceValidator,
    ) -> None:
        self._repo_slug = repo_slug
        self._repo_root = repo_root
        self._working_copy = working_copy
        self._workspace = workspace
        self._allocator = allocator
        self._ledger = ledger
        self._validator = validator

    def import_historical(
        self, command: HistoricalIntakeCommand
    ) -> HistoricalIntakeOutcome:
        try:
            return self._import_selected(command)
        except (OSError, CompletionIntakeError, GitError):
            return HistoricalIntakeRefused(
                HistoricalIntakeRefusal.PREREQUISITE_UNAVAILABLE
            )

    def _import_selected(
        self, command: HistoricalIntakeCommand
    ) -> HistoricalIntakeOutcome:
        refused = repository_refusal(command, self._repo_slug)
        if refused is not None:
            return refused
        raw = self._workspace.capture_candidate(command)
        if sha256(raw).hexdigest() != command.candidate_sha256:
            return HistoricalIntakeRefused(HistoricalIntakeRefusal.CANDIDATE_CHANGED)
        if not self._working_copy.verify_historical_selection(
            self._repo_root, command.branch_name, command.target_head_sha
        ):
            return HistoricalIntakeRefused(HistoricalIntakeRefusal.INVALID_SELECTION)
        worktree = self._workspace.allocate(command)
        run = self._allocator.allocate(
            IssueRunAllocation(
                issue_number=command.issue_number,
                session_key=SessionKey(
                    GitHubIssueKey(command.repo_slug, str(command.issue_number)),
                    TaskKind.CODE,
                ),
                worktree_path=worktree,
                session_name="historical-" + uuid4().hex,
                agent_label="operator:historical",
                backend="historical_intake",
                claude_log_dir=None,
                orchestrator_log=None,
                retention_tier="long",
                retention_days=0,
                retention_pinned=True,
            )
        )
        self._ledger.submission_capability(run)
        entry = self._ledger.register_submission(
            run,
            OwnedCompletionSubmission(
                SubmitCompletionEvidence(raw, command.candidate_sha256, uuid4().hex),
                SubmissionOrigin.HISTORICAL_OPERATOR,
                command.actor,
                command,
            ),
        )
        self._ledger.close_run_intake(run)
        return self.resume(entry)

    def resume(self, entry: CompletionIntakeEntry) -> HistoricalIntakeOutcome:
        command = self._ledger.historical_command_for_receipt(entry.entry_id)
        refused = repository_refusal(command, self._repo_slug)
        if refused is not None:
            return refused
        if entry.parse_status is CompletionParseStatus.REJECTED:
            self._ledger.mark_processed(entry.entry_id)
            return HistoricalIntakeRefused(HistoricalIntakeRefusal.INVALID_COMPLETION)
        record = self._ledger.read_completion(entry.entry_id)
        refused = completion_refusal(record)
        if refused is not None:
            self._ledger.mark_processed(entry.entry_id)
            return refused
        validation = self._ledger.validation_for_receipt(entry.entry_id)
        if validation is None:
            result = self._validator.validate(entry)
            if result.head_sha != command.target_head_sha:
                self._ledger.mark_processed(entry.entry_id)
                return HistoricalIntakeRefused(
                    HistoricalIntakeRefusal.INVALID_SELECTION
                )
            self._ledger.attest_validation(entry.entry_id, result)
            validation = self._ledger.validation_for_receipt(entry.entry_id)
        assert validation is not None
        failed = failed_historical_validation(entry.entry_id, validation)
        if failed is not None:
            self._ledger.mark_processed(entry.entry_id)
            return failed
        admitted = self._workspace.admit_parked(command, entry.entry_id)
        self._ledger.mark_processed(entry.entry_id)
        return HistoricalIntakeParked(
            admitted.disposition.record_id, admitted.disposition.evidence_id
        )
