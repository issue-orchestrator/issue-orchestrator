"""Admission-only preservation: every validated head gets custody before teardown."""

from __future__ import annotations

from dataclasses import dataclass

from ..domain.completion_intake import CompletionIntakeError
from ..domain.prepared_completion import PreparedCompletionEvidence
from ..domain.validated_work import ValidatedWorkFailure
from ..domain.validated_work_commands import AutomaticCaptureCommand, ValidatedWorkDispositionBatch
from ..domain.publication_remote import PublicationPrState, PublicationRemoteError
from ..domain.validated_work import RemoteBaselineStatus, ValidatedWorkState
from ..domain.validated_work_capture import (
    AutomaticCaptureDecision, ValidatedWorkRemoteFacts, ValidatedWorkRemoteRequest,
    candidate_evidence, newest_per_work,
)
from ..domain.validated_work_escrow import EscrowArtifacts
from ..ports.completion_intake import CompletionIntakeRuntime
from ..ports.validated_work_preservation import ValidatedWorkAdmissionStore
from ..ports.validated_work_capture_observer import ValidatedWorkCaptureObserver
from ..ports.working_copy import WorkingCopy
from .validated_work_capture import ValidatedWorkCustody
from .validated_work_escrow import EscrowReconciliation


class ValidatedWorkPreservationService:
    def __init__(self, *, intake: CompletionIntakeRuntime, store: ValidatedWorkAdmissionStore,
                 custody: ValidatedWorkCustody, repair: EscrowReconciliation,
                 working_copy: WorkingCopy, observer: ValidatedWorkCaptureObserver) -> None:
        self._intake = intake
        self._store = store
        self._custody = custody
        self._repair = repair
        self._working_copy = working_copy
        self._observer = observer

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
        observations: dict[
            ValidatedWorkRemoteRequest, ValidatedWorkRemoteFacts | _RemoteUnavailable
        ] = {}
        for candidate in newest_per_work(candidates, command.issue_number):
            self._capture(candidate, command, observations)
        return self._store.for_issue(command.issue_number)

    def _capture(
        self, candidate: PreparedCompletionEvidence, command: AutomaticCaptureCommand,
        observations: dict[
            ValidatedWorkRemoteRequest, ValidatedWorkRemoteFacts | _RemoteUnavailable
        ],
    ) -> None:
        branch_name = candidate.run.branch_name
        if branch_name is None:
            raise CompletionIntakeError("exact run has no recorded branch binding")
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
        bound = branch is not None and branch.branch == branch_name
        remote_status = RemoteBaselineStatus.UNOBSERVED
        expected_remote_head_sha = None
        pr_number = None
        remote_failure = None
        request = ValidatedWorkRemoteRequest(
            candidate.run.session_key.issue.scope(), command.issue_number, branch_name,
        )
        observed = observations.get(request)
        if observed is None:
            try:
                observed = self._observer.observe(request)
            except PublicationRemoteError:
                observed = _RemoteUnavailable()
            observations[request] = observed
        if isinstance(observed, ValidatedWorkRemoteFacts):
            remote_status = RemoteBaselineStatus.OBSERVED
            expected_remote_head_sha = observed.branch_head_sha
            pr_number, remote_failure = _capture_pr(
                observed, candidate.run.session_key.issue.scope(), branch_name,
            )
        else:
            remote_failure = ValidatedWorkFailure.REMOTE_UNREADABLE
        evidence = candidate_evidence(candidate, issue_number=command.issue_number,
            head=head, branch_verified=bound, captured_at=command.run_evidence.observed_at,
            remote_baseline_status=remote_status,
            expected_remote_head_sha=expected_remote_head_sha, pr_number=pr_number)
        failure = (ValidatedWorkFailure.WORKTREE_AHEAD_OF_VALIDATION
                   if head != candidate.validation.head_sha else
                   ValidatedWorkFailure.WORKSPACE_INTEGRITY if not bound else remote_failure)
        state = ValidatedWorkState.QUEUED if failure is None else ValidatedWorkState.PARKED
        assert candidate.entry.normalized_path is not None
        self._custody.capture_automatic(evidence,
            EscrowArtifacts(candidate.entry.normalized_path, candidate.validation.result_path, None),
            AutomaticCaptureDecision(
                state, failure,
                command.reason + ("; queued for automatic recovery" if failure is None
                                  else "; preserved pending recovery approval"),
            ))


@dataclass(frozen=True, slots=True)
class _RemoteUnavailable:
    """One failed remote read shared only within its atomic capture batch."""


def _capture_pr(
    facts: ValidatedWorkRemoteFacts, repo_slug: str, branch_name: str,
) -> tuple[int | None, ValidatedWorkFailure | None]:
    if len(facts.pull_requests) > 1:
        return None, ValidatedWorkFailure.DUPLICATE_OPEN_PR
    if not facts.pull_requests:
        return None, None
    pr = facts.pull_requests[0]
    if (
        pr.state is not PublicationPrState.OPEN
        or pr.head_repo != repo_slug
        or pr.base_repo != repo_slug
        or pr.branch != branch_name
        or facts.branch_head_sha is None
        or pr.head_sha != facts.branch_head_sha
    ):
        return None, ValidatedWorkFailure.PR_BRANCH_MISMATCH
    return pr.number, None
