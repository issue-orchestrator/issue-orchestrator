"""Route proven publication, resolve it durably, then remove disposable assets."""

from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timezone
from functools import partial

from ..domain.models import OrchestratorState
from ..domain.published_work_finalization import PublishedWorkFinalizationRequest, PublishedWorkTarget, FinalizationStatus
from ..domain.recovery_attempt import RecoveryAttemptPending, target_from_verification
from ..domain.recovery_completion import RecoveryCompleted, recovery_resolution_complete
from ..domain.recovery_publication import PreparedRecoveryPublication
from ..domain.retry_review_routing import RetryReviewRouting
from ..domain.validated_work import ReviewDisposition
from ..domain.validated_work_claim import ValidatedWorkClaim
from ..domain.validated_work_execution import RecordExecutionToken
from ..domain.validated_work_store import LineageResolutionRefusal
from ..ports.publication_verifier import PublicationVerifier
from ..ports.published_work_finalization import PublishedWorkFinalizer
from ..ports.validated_work_effects import ValidatedWorkEffectAuthority
from ..ports.validated_work_store import ValidatedWorkStore


from .recovery_publication_cleanup import RecoveryPublicationCleanup


class RecoveryPublicationCompletion:
    def __init__(self, *, store: ValidatedWorkStore, effects: ValidatedWorkEffectAuthority,
                 finalizer: PublishedWorkFinalizer, verifier: PublicationVerifier,
                 cleanup: RecoveryPublicationCleanup, recovery_label: str,
                 clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)) -> None:
        self._store, self._effects = store, effects
        self._finalizer, self._verifier = finalizer, verifier
        self._cleanup = cleanup
        self._recovery_label, self._clock = recovery_label, clock

    def complete(self, token: RecordExecutionToken, claim: ValidatedWorkClaim,
                 prepared: PreparedRecoveryPublication, target: PublishedWorkTarget,
                 state: OrchestratorState, issue_title: str,
                 ) -> RecoveryCompleted | RecoveryAttemptPending:
        perform = partial(self._effects.perform, token, claim)
        if target.key != prepared.workspace.key or target.review_disposition is not prepared.review_disposition:
            raise ValueError("finalization target differs from prepared publication")
        prepared.command.require_disposition_binding(claim.record_id)
        record = perform(lambda: self._store.record_for_id(claim.record_id))
        if record.current_evidence.evidence_id != prepared.workspace.evidence_id:
            raise ValueError("finalization no longer names current evidence")
        command = replace(prepared.command, pr_number=target.pr_number)
        observed = perform(lambda: self._verifier.confirm_target(command))
        confirmed = target_from_verification(prepared, observed)
        if confirmed != target:
            return RecoveryAttemptPending("Publication no longer matches the finalization target", observed.failure)
        request = PublishedWorkFinalizationRequest(
            state, token, claim, record.finalization_phase, target,
            RetryReviewRouting(target.key.branch_name, False,
                target.review_disposition is ReviewDisposition.EXCHANGE_APPROVED, False),
            issue_title, prepared.processing_policy.agent_label,
            "Validated work recovered from retained completion", self._recovery_label,
            record.current_evidence.admission.evidence.observations.observed_blocking_labels,
            str(prepared.completion.run.run.worktree_path),
        )
        finalized = self._finalizer.finalize(request)
        if finalized.status is not FinalizationStatus.FINALIZED:
            return RecoveryAttemptPending(finalized.message, finalized.failure)
        observed = perform(lambda: self._verifier.confirm_target(command))
        if target_from_verification(prepared, observed) != target:
            return RecoveryAttemptPending("Publication changed before durable resolution", observed.failure)
        if not recovery_resolution_complete(record, target):
            resolved = perform(lambda: self._store.resolve_published(
                claim, record_id=claim.record_id, published_head_sha=target.key.validated_head_sha,
                pre_push_expected=command.expected_remote_head_sha or "", finalized_at=self._clock().isoformat(),
            ))
            if isinstance(resolved, LineageResolutionRefusal):
                return RecoveryAttemptPending(f"Publication resolution refused: {resolved.value}")
        return self._cleanup.release(token, claim, target)
