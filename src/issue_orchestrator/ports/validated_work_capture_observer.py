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
        """Every open or merged same-repository PR of the issue on another of
        the issue's own branches (``<issue>-...``, not ``request.branch_name``),
        complete and uncached, or raise (#8137).

        A PR is the issue's when it references it (``Closes #N`` / ``Refs #N``),
        as every PR the orchestrator opens does. Work republished on another
        branch - a completion whose PR collided, a slice rebuilt on a fresh
        branch - is found here, never by the record's branch name.
        """
        ...

    def issue_pull_request(
        self, repo_slug: str, issue_number: int, number: int
    ) -> PublicationPullRequest | None:
        """PR ``number`` when it is an open or merged same-repository PR of the
        issue on any of the issue's own branches, read uncached; else None,
        or raise when the answer is incomplete (#9092).

        Association is the issue's reference timeline, as for
        :meth:`issue_pull_requests`, but no branch is excluded: a release
        names its superseding PR, which may be on the records' own branch.
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

    def issue_pull_request(
        self, repo_slug: str, issue_number: int, number: int
    ) -> PublicationPullRequest | None:
        raise PublicationRemoteError("validated-work remote observation is unavailable")
