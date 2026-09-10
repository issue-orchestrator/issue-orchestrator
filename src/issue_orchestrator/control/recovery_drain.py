"""Bounded round-robin recovery work; one stalled record cannot starve the rest."""

from collections.abc import Callable
import logging
import time

from ..domain.models import OrchestratorState
from ..domain.recovery_attempt import RecoveryAttemptPending
from ..domain.recovery_drain import RecoveryDrainItem, RecoveryDrainMode, RecoveryDrainReport
from ..domain.recovery_entry import RecoveryRecordRequest
from ..domain.validated_work import require_positive
from ..ports.validated_work_drain import (
    RecoveryDrainAdmission,
    ValidatedWorkDrainQueue,
    ValidatedWorkRecoveryOperation,
)

logger = logging.getLogger(__name__)


class RecoveryDrain:
    """Caller serializes ticks/state; the operation owns each complete worker lease."""

    def __init__(self, *, queue: ValidatedWorkDrainQueue, operation: ValidatedWorkRecoveryOperation,
                 batch_size: int, interval_seconds: int, clock: Callable[[], float] = time.monotonic) -> None:
        require_positive(batch_size, "recovery batch size")
        require_positive(interval_seconds, "recovery interval")
        self._queue, self._operation = queue, operation
        self._batch_size, self._interval, self._clock = batch_size, interval_seconds, clock
        self._after = ""
        self._next_at = float("-inf")

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
            requests = self._queue.recovery_requests(
                after_record_id=self._after,
                limit=self._batch_size,
            )
            if not requests and self._after:
                self._after = ""
                requests = self._queue.recovery_requests(
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
            return RecoveryDrainReport(tuple(items))
        finally:
            self._next_at = self._clock() + self._interval

    def _advance(self, request: RecoveryRecordRequest, state: OrchestratorState) -> RecoveryDrainItem:
        try:
            result = self._operation.run(request, state)
        except Exception as error:
            # The operation has joined children and exited its lease before this
            # boundary observes an error. Custody and unknown attempts survive.
            logger.exception("Recovery operation failed for record %s", request.record_id)
            result = RecoveryAttemptPending(f"Recovery operation failed: {error}")
        return RecoveryDrainItem(request.record_id, request.evidence_id, result)
