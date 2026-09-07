"""Synchronous effect authority: one owner of token/cache/store authentication."""

from collections.abc import Callable
from typing import Protocol, TypeVar

from ..domain.validated_work_claim import ValidatedWorkClaim
from ..domain.validated_work_execution import RecordExecutionToken

T = TypeVar("T")


class ValidatedWorkEffectAuthority(Protocol):
    def perform(
        self,
        token: RecordExecutionToken,
        claim: ValidatedWorkClaim,
        effect: Callable[[], T],
    ) -> T:
        """Authenticate immediately, then run one joined effect under the caller's lease.

        Raise ValidatedWorkClaimLost before dispatch if authority is absent, or
        ValidatedWorkAuthorityUnavailable if the store check cannot complete.
        Never retain callbacks or release the caller's execution lease.
        """
        ...
