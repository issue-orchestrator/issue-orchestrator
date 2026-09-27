"""Bounded round-robin recovery work; one stalled record cannot starve the rest."""

from collections.abc import Callable
import logging
import time

from ..domain.models import OrchestratorState
from ..domain.recovery_attempt import RecoveryAttemptPending
from ..domain.recovery_drain import RecoveryDrainItem, RecoveryDrainMode, RecoveryDrainReport
from ..domain.validated_work_remote_authority import RemoteAuthorityRefreshRequest
from ..domain.validated_work import require_positive
from ..domain.validated_work_commands import StoredEvidenceCommand
from ..domain.recovery_entry import RecoveryRecordRequest
from ..domain.recovery_completion import RecoveryCompleted
from ..ports.validated_work_drain import (
    RecoveryDrainAdmission,
    ValidatedWorkAuthorityRefreshOperation,
    ValidatedWorkDrainQueue,
    ValidatedWorkDrainRequest,
    ValidatedWorkRecoveryOperation,
    ValidatedWorkScopeSweep,
)
from ..ports.recovery_block import RecoveryBlockSweep
from ..ports.retained_claim_maintenance import RetainedClaimMaintenanceOwner
from .recovery_drain_liveness import RecoveryDrainLiveness

logger = logging.getLogger(__name__)


class RecoveryDrain:
    """Caller serializes ticks/state; the operation owns each complete worker lease."""

    def __init__(self, *, queue: ValidatedWorkDrainQueue, operation: ValidatedWorkRecoveryOperation,
                 authority_refresh: ValidatedWorkAuthorityRefreshOperation,
                 claim_maintenance: RetainedClaimMaintenanceOwner,
                 block_sweep: RecoveryBlockSweep, scope_sweep: ValidatedWorkScopeSweep,
                 batch_size: int, interval_seconds: int,
                 liveness: RecoveryDrainLiveness,
                 clock: Callable[[], float] = time.monotonic) -> None:
        require_positive(batch_size, "recovery batch size")
        require_positive(interval_seconds, "recovery interval")
        self._queue, self._operation, self._authority_refresh = queue, operation, authority_refresh
        self._block_sweep = block_sweep
        self._scope_sweep = scope_sweep
        self._claim_maintenance = claim_maintenance
        self._batch_size, self._interval, self._clock = batch_size, interval_seconds, clock
        self._liveness = liveness
        self._after = ""
        self._next_at = float("-inf")

    @staticmethod
    def _request(command: StoredEvidenceCommand) -> RecoveryRecordRequest:
        return RecoveryRecordRequest(
            record_id=command.authority.record_id,
            evidence_id=command.evidence_id,
            approved=command.authority,
            issue_number=command.issue_number,
        )

    def preflight(
        self, command: StoredEvidenceCommand
    ) -> RecoveryAttemptPending | None:
        """Read current applicability through the shared record operation."""
        return self._operation.preflight(self._request(command))

    def recover(
        self, command: StoredEvidenceCommand, state: OrchestratorState
    ) -> RecoveryCompleted | RecoveryAttemptPending:
        """Execute explicit recovery through the same per-record operation.

        An operator asked for this, so it runs whatever the liveness owner holds
        (#7350); its outcome is still recorded, and a success releases a park.
        """
        request = self._request(command)
        key = self._liveness.key(request)
        try:
            result = self._operation.run(request, state)
        except Exception as error:
            self._liveness.settle_explicit(request.record_id, key, error)
            raise
        self._liveness.settle_explicit(request.record_id, key, result)
        return result

    def tick(
        self, state: OrchestratorState, admission: RecoveryDrainAdmission
    ) -> RecoveryDrainReport:
        if admission() is RecoveryDrainMode.STOPPED:
            return RecoveryDrainReport(())
        started = self._clock()
        if started < self._next_at:
            return RecoveryDrainReport(())
        # Advance even on an unavailable queue: an outage cannot create a tight
        # retry loop. A completed worker starts the next interval at quiescence.
        self._next_at = started + self._interval
        try:
            claim_maintenance = self._claim_maintenance.reconcile(admission)
            # Before publication: a record recovery never owned resolves here
            # even in states no publication lane selects (#7323).
            scope_sweep = self._scope_sweep.tick(admission)
            block_sweep = self._block_sweep.tick(admission)
            requests = self._queue.drain_requests(
                after_record_id=self._after,
                limit=self._batch_size,
            )
            if not requests and self._after:
                self._after = ""
                requests = self._queue.drain_requests(
                    after_record_id="",
                    limit=self._batch_size,
                )
            items: list[RecoveryDrainItem] = []
            for request in requests:
                if admission() is RecoveryDrainMode.STOPPED:
                    break
                items.append(self._advance(request, state))
            if items:
                completed_batch = len(items) == len(requests)
                self._after = (
                    ""
                    if completed_batch and len(requests) < self._batch_size
                    else items[-1].record_id
                )
            return RecoveryDrainReport(tuple(items), claim_maintenance, block_sweep, scope_sweep)
        finally:
            self._next_at = self._clock() + self._interval

    def _advance(self, request: ValidatedWorkDrainRequest, state: OrchestratorState) -> RecoveryDrainItem:
        # Consult the liveness owner before selecting the record again (#7350):
        # a record parked on unchanged facts is held, never retried every pass.
        # It still yields an item, so the round-robin cursor moves past it.
        key = self._liveness.key(request)
        if key is None:
            held = RecoveryAttemptPending("Held by action liveness: a fact read for the record failed")
            return RecoveryDrainItem(request.record_id, request.evidence_id, held)
        decision = self._liveness.admit(key)
        if not decision.admitted:
            held = RecoveryAttemptPending(f"Held by action liveness: {decision.describe()}")
            return RecoveryDrainItem(request.record_id, request.evidence_id, held)
        try:
            result = (
                self._authority_refresh.run(request)
                if isinstance(request, RemoteAuthorityRefreshRequest)
                else self._operation.run(request, state)
            )
        except Exception as error:
            # The operation has joined children and exited its lease before this
            # boundary observes an error. Custody and unknown attempts survive.
            logger.exception("Recovery drain operation failed for record %s", request.record_id)
            self._liveness.settle_error(key, error)
            result = RecoveryAttemptPending(f"Recovery drain operation failed: {error}")
            return RecoveryDrainItem(request.record_id, request.evidence_id, result)
        self._liveness.settle(key, result)
        return RecoveryDrainItem(request.record_id, request.evidence_id, result)
