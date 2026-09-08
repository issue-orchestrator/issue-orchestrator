"""Admission-only preservation: every validated head gets custody before teardown."""

from ..domain.completion_intake import CompletionIntakeError
from ..domain.prepared_completion import PreparedCompletionEvidence
from ..domain.validated_work import ValidatedWorkFailure
from ..domain.validated_work_commands import AutomaticCaptureCommand, ValidatedWorkDispositionBatch
from ..domain.validated_work_capture import candidate_evidence, newest_per_work
from ..domain.validated_work_escrow import EscrowArtifacts
from ..ports.completion_intake import CompletionIntakeRuntime
from ..ports.validated_work_preservation import ValidatedWorkAdmissionStore
from ..ports.working_copy import WorkingCopy
from .validated_work_capture import ParkedEvidenceCustody
from .validated_work_escrow import EscrowReconciliation


class ValidatedWorkPreservationService:
    def __init__(self, *, intake: CompletionIntakeRuntime, store: ValidatedWorkAdmissionStore,
                 custody: ParkedEvidenceCustody, repair: EscrowReconciliation, working_copy: WorkingCopy) -> None:
        self._intake = intake
        self._store = store
        self._custody = custody
        self._repair = repair
        self._working_copy = working_copy

    def has_unresolved_work(self, issue_number: int) -> bool:
        return self._store.has_unresolved_work(issue_number)

    def for_issue(self, issue_number: int) -> ValidatedWorkDispositionBatch:
        return self._store.for_issue(issue_number)

    def dispose_at_termination(self, command: AutomaticCaptureCommand) -> ValidatedWorkDispositionBatch:
        candidates = self._intake.prepare_termination(command.run_evidence, command.scope)
        report = self._repair.reconcile_escrow_orphans()
        if report.problems:
            raise CompletionIntakeError(f"escrow custody requires repair: {report.problems}")
        self._repair.require_issue_custody(command.issue_number)
        for candidate in newest_per_work(candidates, command.issue_number):
            self._capture(candidate, command)
        return self._store.for_issue(command.issue_number)

    def _capture(self, candidate: PreparedCompletionEvidence, command: AutomaticCaptureCommand) -> None:
        # Both legal identity forms are checked before observing mutable workspace facts.
        # This preserves first-capture branch binding and observations after runtime release.
        for bound in (True, False):
            identity = candidate_evidence(candidate, issue_number=command.issue_number,
                head=candidate.validation.head_sha, branch_verified=bound,
                captured_at=command.run_evidence.observed_at)
            retained = self._store.evidence_for_id(identity.evidence_id)
            if retained is not None:
                return
        worktree = candidate.entry.run.worktree_path
        head = self._working_copy.get_head_sha(worktree)
        if head is None:
            raise CompletionIntakeError("candidate worktree HEAD cannot be established")
        branch = self._working_copy.get_branch_status(worktree)
        bound = branch is not None and branch.branch == candidate.run.branch_name
        evidence = candidate_evidence(candidate, issue_number=command.issue_number,
            head=head, branch_verified=bound, captured_at=command.run_evidence.observed_at)
        failure = (ValidatedWorkFailure.WORKTREE_AHEAD_OF_VALIDATION
                   if head != candidate.validation.head_sha else
                   ValidatedWorkFailure.WORKSPACE_INTEGRITY if not bound else None)
        assert candidate.entry.normalized_path is not None
        self._custody.capture(evidence,
            EscrowArtifacts(candidate.entry.normalized_path, candidate.validation.result_path, None),
            reason=command.reason + "; preserved pending recovery approval", failure=failure)
