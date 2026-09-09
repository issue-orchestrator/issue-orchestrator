"""Own the full synchronous recovery lease, claim, publication and finalization."""

from ..domain.models import OrchestratorState
from ..domain.recovery_attempt import RecoveryAttemptPending
from ..domain.recovery_completion import RecoveryCompleted
from ..domain.recovery_entry import RecoveryRecordRequest
from ..domain.validated_work_execution import RecordExecutionBusy, RecordExecutionToken
from ..ports.validated_work_execution import ValidatedWorkExecutionOwner
from ..ports.validated_work_store import ValidatedWorkStore
from .claimed_recovery_preparation import ClaimedRecoveryPreparation
from .recovery_publication_attempt import RecoveryPublicationAttempt
from .recovery_publication_completion import RecoveryPublicationCompletion


class RecoveryRecordOperation:
    def __init__(self, *, execution: ValidatedWorkExecutionOwner, store: ValidatedWorkStore,
                 preparation: ClaimedRecoveryPreparation, publication: RecoveryPublicationAttempt,
                 completion: RecoveryPublicationCompletion) -> None:
        self._execution, self._store = execution, store
        self._preparation, self._publication, self._completion = preparation, publication, completion

    def preflight(
        self, request: RecoveryRecordRequest
    ) -> RecoveryAttemptPending | None:
        """Read-only applicability check over the same current-record policy."""
        return request.refusal(self._store.record_for_id(request.record_id))

    def run(self, request: RecoveryRecordRequest,
            state: OrchestratorState) -> RecoveryCompleted | RecoveryAttemptPending:
        """Return only after all synchronous children finish; never transfer a token.

        A refused claim release stays private in the execution owner. Subsequent
        entries must settle that release before attempting any new recovery work.
        """
        lease = self._execution.try_enter(request.record_id)
        if isinstance(lease, RecordExecutionBusy):
            return RecoveryAttemptPending("Record recovery is already executing")
        with lease as token:
            if not self._execution.relinquish(token):
                return RecoveryAttemptPending("Record awaits its reserved stop operation")
            try:
                return self._run_owned(token, request, state)
            finally:
                self._execution.relinquish(token)

    def _run_owned(self, token: RecordExecutionToken, request: RecoveryRecordRequest,
                   state: OrchestratorState) -> RecoveryCompleted | RecoveryAttemptPending:
        record = self._store.record_for_id(request.record_id)
        refusal = request.refusal(record)
        if refusal is not None:
            return refusal
        claim = self._store.acquire_claim(request.record_id,
            expected_states=frozenset({record.disposition.state}), evidence_id=request.evidence_id)
        if claim is None:
            return RecoveryAttemptPending("Record belongs to another owner or its evidence changed")
        self._execution.remember_claim(token, claim)
        ready = self._preparation.prepare(token, claim, request)
        if isinstance(ready, RecoveryAttemptPending):
            return ready
        target = self._publication.advance(token, claim, ready.publication, approved=request.approved)
        if isinstance(target, RecoveryAttemptPending):
            return target
        return self._completion.complete(token, claim, ready.publication, target, state, ready.issue_title)
