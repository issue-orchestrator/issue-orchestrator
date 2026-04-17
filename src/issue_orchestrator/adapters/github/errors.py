"""GitHub adapter exception types."""

from typing import Any


class GitHubHttpError(Exception):
    """Raised when a GitHub HTTP request fails."""

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


class GitHubTransportError(Exception):
    """Raised when a GitHub request fails before an HTTP response."""

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


__all__ = [
    "GitHubAuthError",
    "GitHubHttpError",
    "GitHubTransportError",
]
