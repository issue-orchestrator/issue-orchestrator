"""Reconcile resolved recovery interests before releasing disposable workspaces."""

from functools import partial

from ..domain.issue_disposition_gate import IssueDispositionGateStatus
from ..domain.published_work_finalization import PublishedWorkTarget
from ..domain.recovery_attempt import RecoveryAttemptPending
from ..domain.recovery_block import RecoveryBlockReconcileStatus
from ..domain.recovery_completion import RecoveryCompleted, recovery_resolution_complete
from ..domain.validated_work_claim import ValidatedWorkClaim
from ..domain.validated_work_execution import RecordExecutionToken
from ..ports.issue_disposition_gate import IssueDispositionMutationGate
from ..ports.publication_workspace import PublicationWorkspaces
from ..ports.validated_work_effects import ValidatedWorkEffectAuthority
from ..ports.validated_work_store import ValidatedWorkStore
from .aggregate_recovery_block import AggregateRecoveryBlocks


class RecoveryPublicationCleanup:
    """Restartable cleanup requires durable resolution, never a caller's assertion."""

    def __init__(self, *, store: ValidatedWorkStore, effects: ValidatedWorkEffectAuthority,
                 blocks: AggregateRecoveryBlocks, workspaces: PublicationWorkspaces,
                 gate: IssueDispositionMutationGate) -> None:
        self._store, self._effects = store, effects
        self._blocks, self._workspaces, self._gate = blocks, workspaces, gate

    def release(self, token: RecordExecutionToken, claim: ValidatedWorkClaim,
                target: PublishedWorkTarget) -> RecoveryCompleted | RecoveryAttemptPending:
        perform = partial(self._effects.perform, token, claim)
        record = perform(lambda: self._store.record_for_id(claim.record_id))
        if not recovery_resolution_complete(record, target):
            raise ValueError("workspace cleanup requires durable publication resolution")
        # Resolution can classify waiting siblings. Their aggregate owner alone
        # decides whether remaining interests keep recovery labels.
        projection = perform(lambda: self._blocks.reconcile_issue_block(target.key.issue_number))
        if projection.status is not RecoveryBlockReconcileStatus.RECONCILED:
            return RecoveryAttemptPending(projection.message)
        with self._gate.try_acquire(target.key.repo_slug, target.key.issue_number) as acquired:
            if acquired is IssueDispositionGateStatus.BUSY:
                return RecoveryAttemptPending("Recovered publication awaits workspace cleanup")
            perform(lambda: self._workspaces.release(record.current_evidence.admission))
        return RecoveryCompleted(target)
