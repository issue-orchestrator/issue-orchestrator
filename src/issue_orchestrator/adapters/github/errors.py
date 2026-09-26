"""GitHub adapter exception types."""

from typing import Any

from ...ports.repository_host import (
    HostRateLimit,
    RepositoryHostError,
    RepositoryHostErrorKind,
    RepositoryHostRateLimitedError,
    RepositoryScanIncompleteError,
)


class GitHubHttpError(RepositoryHostError):
    """Raised when a GitHub HTTP request fails."""

    host = "github"
    kind: RepositoryHostErrorKind = "http"

    def __init__(
        self,
        message: str,
        *,
        method: str | None = None,
        url: str | None = None,
        status_code: int | None = None,
        response_text: str | None = None,
        failure_type: "Any | None" = None,  # FailureType enum, imported lazily
        issue_number: int | None = None,
    ) -> None:
        super().__init__(message)
        self.method = method
        self.url = url
        self.status_code = status_code
        self.response_text = response_text
        self.failure_type = failure_type  # FailureType enum
        self.issue_number = issue_number  # Affected issue number if issue_local

    def is_systemic(self) -> bool:
        """Check if this is a systemic failure."""
        from ...ports.verification import FailureType

        return self.failure_type == FailureType.SYSTEMIC

    def is_issue_local(self) -> bool:
        """Check if this is an issue-local failure."""
        from ...ports.verification import FailureType

        return self.failure_type == FailureType.ISSUE_LOCAL


class GitHubTransportError(RepositoryHostError):
    """Raised when a GitHub request fails before an HTTP response."""

    host = "github"
    kind: RepositoryHostErrorKind = "transport"

    def __init__(
        self,
        message: str,
        *,
        method: str | None = None,
        url: str | None = None,
        original: Exception | None = None,
    ) -> None:
        super().__init__(message)
        self.method = method
        self.url = url
        self.original = original


class GitHubAuthError(GitHubHttpError):
    """Raised when GitHub auth cannot be resolved."""


class GitHubScanIncompleteError(GitHubHttpError, RepositoryScanIncompleteError):
    """An exhaustive GitHub scan could not prove completeness.

    Subclasses ``GitHubHttpError`` so existing adapter-level handlers keep
    catching it, and the port-level marker so control policy can refuse to
    treat it as a skippable outage.
    """


class GitHubRateLimitedError(GitHubHttpError, RepositoryHostRateLimitedError):
    """GitHub refused the request on a rate limit; ``rate_limit`` says until when.

    Subclasses ``GitHubHttpError`` so every existing handler keeps catching it,
    and the port-level marker so launch policy can defer until the reset
    instead of spending a retry on a request GitHub has already said it will
    refuse (#7297).
    """

    def __init__(
        self, message: str, *, rate_limit: HostRateLimit, **kwargs: Any
    ) -> None:
        super().__init__(message, **kwargs)
        self.rate_limit = rate_limit


class GitHubRateLimitedScanIncompleteError(
    GitHubRateLimitedError, GitHubScanIncompleteError
):
    """An exhaustive scan stopped on a rate-limited page.

    Both at once: the scan is still incomplete (callers must never treat it as
    a skippable outage) and the reason is a rate limit with a known reset.
    """


__all__ = [
    "GitHubAuthError",
    "GitHubHttpError",
    "GitHubRateLimitedError",
    "GitHubRateLimitedScanIncompleteError",
    "GitHubScanIncompleteError",
    "GitHubTransportError",
]
