"""Fresh complete issue identity; unreadability is never an open/absent issue."""

from typing import Protocol

from ..domain.recovery_entry import RecoveryIssue


class RecoveryIssueReadError(RuntimeError):
    pass


class RecoveryIssueReader(Protocol):
    def read(self, repo_slug: str, issue_number: int) -> RecoveryIssue:
        """Bypass caches/ETags; missing, malformed or unreadable raises RecoveryIssueReadError."""
        ...
