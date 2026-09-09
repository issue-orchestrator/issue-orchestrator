"""Behavior-level fence shared by publication, finalization and future stages."""

from collections.abc import Callable
from typing import TypeVar

from ..domain.validated_work_claim import ValidatedWorkClaim
from ..domain.validated_work_execution import (
    RecordExecutionToken,
    ValidatedWorkClaimLost,
    ValidatedWorkAuthorityUnavailable,
)
from ..ports.validated_work_execution import ValidatedWorkExecutionOwner
from ..ports.validated_work_store import ValidatedWorkFence

T = TypeVar("T")


class FencedValidatedWorkEffects:
    def __init__(
        self, *, execution: ValidatedWorkExecutionOwner, fence: ValidatedWorkFence
    ) -> None:
        self._execution = execution
        self._fence = fence

    def perform(
        self,
        token: RecordExecutionToken,
        claim: ValidatedWorkClaim,
        effect: Callable[[], T],
    ) -> T:
        try:
            self._execution.require_active(token, claim.record_id)
            retained = self._execution.claim(token)
        except RuntimeError as error:
            raise ValidatedWorkClaimLost(str(error)) from error
        if retained is not claim:
            raise ValidatedWorkClaimLost("execution no longer holds this exact claim")
        try:
            held = self._fence.holds_claim(claim)
        except Exception as error:
            raise ValidatedWorkAuthorityUnavailable(
                f"claim read unavailable: {error}"
            ) from error
        if not held:
            raise ValidatedWorkClaimLost("store no longer holds this exact claim")
        return effect()
