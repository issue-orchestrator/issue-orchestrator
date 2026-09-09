"""Disposition-only publication; the outer service retains lease and attempt."""

from typing import Protocol

from ..domain.validated_head_publication import (
    PublishValidatedHeadCommand,
    PublishValidatedHeadOutcome,
)
from ..domain.validated_work_claim import ValidatedWorkClaim
from ..domain.validated_work_execution import RecordExecutionToken


class ValidatedWorkPublisher(Protocol):
    def publish(
        self,
        token: RecordExecutionToken,
        claim: ValidatedWorkClaim,
        command: PublishValidatedHeadCommand,
    ) -> PublishValidatedHeadOutcome:
        """Fence each step; unknown authority raises and dispatches no next step."""
        ...
