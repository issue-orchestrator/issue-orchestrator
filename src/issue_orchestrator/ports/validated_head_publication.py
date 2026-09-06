"""Remote publication capabilities; no workflow or admission authority."""

from typing import Protocol

from ..domain.validated_head_publication import (
    BranchWriteOutcome,
    PrEnsureOutcome,
    PublishValidatedHeadCommand,
    PublishValidatedHeadOutcome,
)


class ValidatedHeadExecutor(Protocol):
    def push_validated_head(
        self, command: PublishValidatedHeadCommand
    ) -> BranchWriteOutcome: ...

    def ensure_pull_request(
        self, command: PublishValidatedHeadCommand
    ) -> PrEnsureOutcome: ...

    def publish_or_reconcile(
        self, command: PublishValidatedHeadCommand
    ) -> PublishValidatedHeadOutcome: ...
