"""Port for attempt-scoped state persistence."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..domain.attempt import Attempt, AttemptKey
from ..domain.issue_key import IssueKey


@runtime_checkable
class AttemptStore(Protocol):
    """Persistence boundary for #6130 attempt state."""

    def for_key(self, key: AttemptKey) -> Attempt | None:
        """Return an attempt record for ``key`` if one exists."""
        ...

    def upsert(self, attempt: Attempt) -> None:
        """Create or replace the attempt record."""
        ...

    def supersede_issue(self, issue_key: IssueKey) -> int:
        """Drop all cached attempts for an issue.

        Returns the number of sidecars removed. This is used by scratch reset:
        correctness comes from new SHAs missing by construction; proactive
        cleanup keeps old attempt sidecars from accumulating.
        """
        ...
