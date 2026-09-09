"""One shared local owner guards complete synchronous record operations.

Acquire execution before a durable claim and an issue mutation gate. Children
must complete or terminate and join before leaving the lease. Cancellation of
an async caller must not end the synchronous worker's lease.
"""

from contextlib import AbstractContextManager
from typing import Protocol

from ..domain.validated_work_claim import ValidatedWorkClaim
from ..domain.validated_work_execution import RecordExecutionBusy, RecordExecutionToken

RecordExecutionLease = AbstractContextManager[RecordExecutionToken]


class ValidatedWorkExecutionOwner(Protocol):
    def try_enter(
        self, record_id: str
    ) -> RecordExecutionLease | RecordExecutionBusy: ...
    def require_active(self, token: RecordExecutionToken, record_id: str) -> None: ...
    def claim(self, token: RecordExecutionToken) -> ValidatedWorkClaim | None: ...
    def remember_claim(
        self, token: RecordExecutionToken, claim: ValidatedWorkClaim
    ) -> None: ...
    def relinquish(self, token: RecordExecutionToken) -> bool: ...
