"""Admission-only preservation: every validated head gets custody before teardown."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path
from dataclasses import dataclass, replace

from ..domain.completion_intake import CompletionIntakeError
from ..domain.prepared_completion import PreparedCompletionEvidence
from ..domain.validated_work import ValidatedWorkFailure
from ..domain.validated_work_commands import AutomaticCaptureCommand, ValidatedWorkDispositionBatch
from ..domain.publication_remote import PublicationRemoteError
from ..domain.validated_work import RemoteBaselineStatus, ValidatedWorkEvidence, ValidatedWorkState
from ..domain.validated_work_capture import (
    AutomaticCaptureDecision, ValidatedWorkRemoteFacts, ValidatedWorkRemoteRequest,
    candidate_evidence, candidate_key, newest_per_work,
)
from ..domain.validated_work_remote_authority import classify_remote_pr
from ..domain.validated_work_scope import outside_scope_reason, recovery_owns
from ..domain.validated_work_escrow import EscrowArtifacts
from ..domain.validated_work_store import AncestryRelation
from ..ports.completion_intake import CompletionIntakeRuntime
from ..ports.validated_work_preservation import ValidatedWorkAdmissionStore
from ..ports.validated_work_capture_observer import ValidatedWorkCaptureObserver
from ..ports.working_copy import WorkingCopy
from .validated_work_capture import ValidatedWorkCustody
from .validated_work_escrow import EscrowReconciliation
from .validated_work_published_head import PullRequestCarriage

logger = logging.getLogger(__name__)


class ValidatedWorkPreservationService:
    def __init__(self, *, intake: CompletionIntakeRuntime, store: ValidatedWorkAdmissionStore,
                 custody: ValidatedWorkCustody, repair: EscrowReconciliation,
                 working_copy: WorkingCopy, observer: ValidatedWorkCaptureObserver,
                 base_branch: Callable[[int, Path], str | None],
                 carriage: PullRequestCarriage) -> None:
        self._intake = intake
        self._store = store
        self._custody = custody
        self._repair = repair
        self._working_copy = working_copy
        self._observer = observer
        # The ref a head must be ahead of to be work: the base its PR targets.
        self._base_branch = base_branch
        self._carriage = carriage

    def has_unresolved_work(self, issue_number: int) -> bool:
        return self._store.has_unresolved_work(issue_number)

    def for_issue(self, issue_number: int) -> ValidatedWorkDispositionBatch:
        return self._store.for_issue(issue_number)

    def dispose_at_termination(self, command: AutomaticCaptureCommand) -> ValidatedWorkDispositionBatch:
        # One remote read per branch, shared by the capture decision and the
        # capture itself (#7347 PR 2 review r5).
        observations: dict[
            ValidatedWorkRemoteRequest, ValidatedWorkRemoteFacts | _RemoteUnavailable
        ] = {}
        candidates = tuple(
            candidate
            for candidate in self._intake.prepare_termination(command.run_evidence, command.scope)
            if self._captures(candidate, command, observations)
        )
        report = self._repair.reconcile_escrow_orphans()
        if report.problems:
            raise CompletionIntakeError(f"escrow custody requires repair: {report.problems}")
        self._repair.require_issue_custody(command.issue_number)
        selected = newest_per_work(candidates, command.issue_number)
        for candidate in selected:
            self._capture(candidate, command, observations)
        return replace(
            self._store.for_issue(command.issue_number),
            captured_keys=frozenset(candidate_key(c, command.issue_number) for c in selected),
        )

    def _captures(
        self, candidate: PreparedCompletionEvidence, command: AutomaticCaptureCommand,
        observations: dict[
            ValidatedWorkRemoteRequest, ValidatedWorkRemoteFacts | _RemoteUnavailable
        ],
    ) -> bool:
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
        if not recovery_owns(role):
            logger.info(
                "[VALIDATED_WORK] Not capturing issue #%d run %s: %s",
                role.issue_number, candidate.run.run.run_id, outside_scope_reason(role),
            )
            return False
        worktree = candidate.entry.run.worktree_path
        base = self._pull_request_base(candidate, command, observations)
        # Read fresh from the remote: a cached tracking ref of a base that has
        # since been force-pushed would drop real work. A base that cannot be
        # read or established proves nothing: the head is preserved.
        base_sha = None if base is None else self._working_copy.fetch_remote_branch_head(worktree, base)
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

    def _record_publication(
        self, candidate: PreparedCompletionEvidence, command: AutomaticCaptureCommand,
        observed: ValidatedWorkRemoteFacts, evidence: ValidatedWorkEvidence,
    ) -> None:
        """Before admission: record the head a PR already publishes.

        When the completion's own push put this validated head on its open PR
        - or a merged PR of the branch already landed it - that PR head is the
        lineage's published head. Recording it first makes admission resolve
        the capture as contained in it
        (RECOVERED, ``recovery-pending`` never asserted, the PR's work under
        published-review custody) instead of classifying a rebased rework
        against the lineage's older head and parking it divergent (porchpin
        #186). No proof, no record: the capture is admitted as before.

        An in-flight recovery publication keeps the lineage, and then recovery
        holds the capture like any other.
        """
        key = evidence.identity.key
        published = self._carriage.proof(
            ValidatedWorkRemoteRequest(key.repo_slug, command.issue_number, key.branch_name),
            validated_head_sha=key.validated_head_sha,
            repository=candidate.entry.run.worktree_path, facts=observed,
        )
        if published is None:
            return
        status = self._store.record_pr_publication(
            key, published=published, observed_at=command.run_evidence.observed_at,
        )
        logger.info(
            "[VALIDATED_WORK] Issue #%d run %s: %s; lineage publication %s",
            command.issue_number, candidate.run.run.run_id,
            published.describe(key.validated_head_sha), status.value,
        )

    def _pull_request_base(
        self, candidate: PreparedCompletionEvidence, command: AutomaticCaptureCommand,
        observations: dict[
            ValidatedWorkRemoteRequest, ValidatedWorkRemoteFacts | _RemoteUnavailable
        ],
    ) -> str | None:
        """The branch this work's PR targets, or None when it cannot be known.

        An open PR for the run's branch already names its base, and that is the
        authority: a base selected afresh (a changed stack or configuration)
        may already contain a head the existing PR still needs. With no PR, the
        issue's selected base (``PullRequestBaseBranch``) is what one would
        target. An unreadable remote, or PR facts capture would park as
        ambiguous, establish nothing.
        """
        branch_name = candidate.run.branch_name
        if branch_name is None:
            return None
        observed = self._observe(candidate, command, observations, branch_name)
        if not isinstance(observed, ValidatedWorkRemoteFacts):
            return None
        pr_number, failure = classify_remote_pr(
            observed, candidate.run.session_key.issue.scope(), branch_name,
        )
        if failure is not None:
            return None
        if pr_number is not None:
            return observed.pull_requests[0].base_branch
        return self._base_branch(command.issue_number, candidate.entry.run.worktree_path)

    def _observe(
        self, candidate: PreparedCompletionEvidence, command: AutomaticCaptureCommand,
        observations: dict[
            ValidatedWorkRemoteRequest, ValidatedWorkRemoteFacts | _RemoteUnavailable
        ],
        branch_name: str,
    ) -> ValidatedWorkRemoteFacts | _RemoteUnavailable:
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
        return observed

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
        observed = self._observe(candidate, command, observations, branch_name)
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
        if isinstance(observed, ValidatedWorkRemoteFacts):
            self._record_publication(candidate, command, observed, evidence)
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
