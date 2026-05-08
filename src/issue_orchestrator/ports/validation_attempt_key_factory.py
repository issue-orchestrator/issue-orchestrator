"""Port for deriving validation attempt identity."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.attempt import AttemptKey
from ..domain.issue_key import IssueKey


@runtime_checkable
class ValidationAttemptKeyFactory(Protocol):
    """Builds an attempt-scoped cache key for validation."""

    def for_validation_attempt(
        self,
        *,
        issue_key: IssueKey,
        head_sha: str,
    ) -> AttemptKey:
        """Return the issue-at-HEAD identity for a validation attempt."""
        ...
