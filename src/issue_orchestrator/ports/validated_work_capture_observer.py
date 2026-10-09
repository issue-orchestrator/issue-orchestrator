"""Fresh remote observation required before automatic retained-work publication."""

from typing import Protocol

from ..domain.validated_work_capture import (
    ValidatedWorkRemoteFacts,
    ValidatedWorkRemoteRequest,
)
from ..domain.publication_remote import PublicationPullRequest, PublicationRemoteError


class ValidatedWorkCaptureObserver(Protocol):
    def observe(self, request: ValidatedWorkRemoteRequest) -> ValidatedWorkRemoteFacts:
        """Read the complete uncached branch and open-PR facts or raise."""
        ...

    def merged_pull_requests(
        self, request: ValidatedWorkRemoteRequest
    ) -> tuple[PublicationPullRequest, ...]:
        """Every merged PR from the branch, complete and uncached, or raise.

        Each carries its head at merge, which outlives the branch.
        """
        ...

    def issue_pull_requests(
        self, request: ValidatedWorkRemoteRequest
    ) -> tuple[PublicationPullRequest, ...]:
        """Every open or merged same-repository PR of the issue, on ANY branch,
        complete and uncached, or raise (#8137).

        A PR is the issue's when it references it (``Closes #N`` / ``Refs #N``),
        as every PR the orchestrator opens does. Work republished on another
        branch - a completion whose PR collided, a slice rebuilt on a fresh
        branch - is found here, never by the record's branch name.
        """
        ...


class UnavailableValidatedWorkCaptureObserver:
    def observe(self, request: ValidatedWorkRemoteRequest) -> ValidatedWorkRemoteFacts:
        raise PublicationRemoteError("validated-work remote observation is unavailable")

    def merged_pull_requests(
        self, request: ValidatedWorkRemoteRequest
    ) -> tuple[PublicationPullRequest, ...]:
        raise PublicationRemoteError("validated-work remote observation is unavailable")

    def issue_pull_requests(
        self, request: ValidatedWorkRemoteRequest
    ) -> tuple[PublicationPullRequest, ...]:
        raise PublicationRemoteError("validated-work remote observation is unavailable")
