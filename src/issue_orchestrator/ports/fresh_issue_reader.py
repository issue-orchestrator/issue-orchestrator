"""FreshIssueReader port for correctness-critical issue reads."""

from typing import Protocol


class FreshIssueReader(Protocol):
    """Protocol for fresh issue reads (no cache, no ETag)."""

    def read_issue_labels(self, issue_number: int) -> list[str]:
        """Read labels for an issue, bypassing caches."""
        ...
