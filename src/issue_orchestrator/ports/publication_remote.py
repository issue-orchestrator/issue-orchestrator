"""Uncached remote facts and a single attributable PR creation."""

from typing import Protocol

from ..domain.publication_remote import PublicationPullRequest
from ..domain.validated_head_publication import PublishValidatedHeadCommand


class PublicationRemote(Protocol):
    """Every failure raises PublicationRemoteError; None means proven absence."""

    def read_branch(self, command: PublishValidatedHeadCommand) -> str | None: ...

    def read_pr(
        self, command: PublishValidatedHeadCommand, number: int
    ) -> PublicationPullRequest | None: ...

    def list_prs(
        self, command: PublishValidatedHeadCommand
    ) -> tuple[PublicationPullRequest, ...]: ...

    def create_pr(
        self, command: PublishValidatedHeadCommand
    ) -> PublicationPullRequest: ...
