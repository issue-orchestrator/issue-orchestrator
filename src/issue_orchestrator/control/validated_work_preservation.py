"""Admission-only preservation: every validated head gets custody before teardown."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace

from ..domain.completion_intake import CompletionIntakeError
from ..domain.prepared_completion import PreparedCompletionEvidence
from ..domain.validated_work import ValidatedWorkFailure
from ..domain.validated_work_commands import AutomaticCaptureCommand, ValidatedWorkDispositionBatch
from ..domain.publication_remote import PublicationRemoteError
from ..domain.validated_work import RemoteBaselineStatus, ValidatedWorkState
from ..domain.validated_work_capture import (
    AutomaticCaptureDecision, ValidatedWorkRemoteFacts, ValidatedWorkRemoteRequest,
    candidate_evidence, candidate_key, newest_per_work,
)
from ..domain.validated_work_remote_authority import classify_remote_pr
from ..domain.validated_work_escrow import EscrowArtifacts
from ..domain.validated_work_store import AncestryRelation
from ..ports.completion_intake import CompletionIntakeRuntime
from ..ports.validated_work_preservation import ValidatedWorkAdmissionStore
from ..ports.validated_work_capture_observer import ValidatedWorkCaptureObserver
from ..ports.working_copy import WorkingCopy
from .validated_work_capture import ValidatedWorkCustody
from .validated_work_escrow import EscrowReconciliation

logger = logging.getLogger(__name__)


class ValidatedWorkPreservationService:
    def __init__(self, *, intake: CompletionIntakeRuntime, store: ValidatedWorkAdmissionStore,
                 custody: ValidatedWorkCustody, repair: EscrowReconciliation,
                 working_copy: WorkingCopy, observer: ValidatedWorkCaptureObserver,
                 base_ref: Callable[[], str]) -> None:
        self._intake = intake
        self._store = store
        self._custody = custody
        self._repair = repair
        self._working_copy = working_copy
        self._observer = observer
        # The ref a head must be ahead of to be work: the base its PR targets.
        self._base_ref = base_ref

    def has_unresolved_work(self, issue_number: int) -> bool:
        return self._store.has_unresolved_work(issue_number)

    def for_issue(self, issue_number: int) -> ValidatedWorkDispositionBatch:
        return self._store.for_issue(issue_number)

    def dispose_at_termination(self, command: AutomaticCaptureCommand) -> ValidatedWorkDispositionBatch:
        candidates = tuple(
            candidate
            for candidate in self._intake.prepare_termination(command.run_evidence, command.scope)
            if self._captures(candidate)
        )
        report = self._repair.reconcile_escrow_orphans()
        if report.problems:
            raise CompletionIntakeError(f"escrow custody requires repair: {report.problems}")
        self._repair.require_issue_custody(command.issue_number)
        observations: dict[
            ValidatedWorkRemoteRequest, ValidatedWorkRemoteFacts | _RemoteUnavailable
        ] = {}
        selected = newest_per_work(candidates, command.issue_number)
        for candidate in selected:
            self._capture(candidate, command, observations)
        return replace(
            self._store.for_issue(command.issue_number),
            captured_keys=frozenset(candidate_key(c, command.issue_number) for c in selected),
        )

    def _captures(self, candidate: PreparedCompletionEvidence) -> bool:
        """Whether this completion is validated work recovery must hold (#7347).

        Two facts, both required. The run's KIND must make its completion the
        issue's deliverable (``capturable``): a tech-lead run is recorded
        against its subject issue, but its branch is its own and its
        completion already decides what that branch publishes - taking it as
        the subject's work put a ``recovery-pending`` on the subject that no
        publication ever released (#7323, #7346). And its validated head must
        have commits ahead of the base its PR targets: a head the base already
        contains is nothing to preserve, and a PR of it is refused by the host.
        """
        role = candidate.role
        if not role.kind.capabilities.capturable:
            logger.info(
                "[VALIDATED_WORK] Not capturing issue #%d run %s: a %s run's "
                "completion is not the issue's deliverable",
                role.issue_number, candidate.run.run.run_id, role.kind.value,
            )
            return False
        base = self._base_ref()
        worktree = candidate.entry.run.worktree_path
        base_sha = self._working_copy.resolve_commit(worktree, base)
        # A base that cannot be read proves nothing: the head is preserved.
        relation = None if base_sha is None else self._working_copy.compare_commits(
            worktree, left=candidate.validation.head_sha, right=base_sha,
        )
        if relation in (AncestryRelation.EQUAL, AncestryRelation.ANCESTOR):
            logger.info(
                "[VALIDATED_WORK] Not capturing issue #%d run %s: validated head "
                "%s has no commits ahead of %s",
                role.issue_number, candidate.run.run.run_id,
                candidate.validation.head_sha, base,
            )
            return False
        return True

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
            pr_number, remote_failure = classify_remote_pr(
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
