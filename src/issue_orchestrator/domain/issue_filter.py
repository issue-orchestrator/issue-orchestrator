"""Issue filtering by labels.

This module provides a simple label-based filter for issues. The filter
can be configured to exclude issues that have any of a set of labels.

The current implementation is intentionally simple (exclude_labels only).
The interface is designed to allow future expansion to more complex
filtering (e.g., expressions like "(label1 or label2) and ~label3").

Usage:
    filter = IssueLabelFilter(exclude_labels=["test-data", "wip"])
    filtered = filter.apply(issues)  # Removes issues with test-data or wip labels
"""

from dataclasses import dataclass, field
from typing import Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    from ..ports.issue import Issue


@dataclass
class IssueLabelFilter:
    """Filter issues based on label criteria.

    This is a simple filter that excludes issues matching any of the
    exclude_labels. Can be extended or replaced with more complex
    filtering logic in the future.

    Attributes:
        exclude_labels: Issues with ANY of these labels are excluded.
            Labels are matched exactly (case-sensitive).
    """

    exclude_labels: frozenset[str] = field(default_factory=frozenset)

    @classmethod
    def from_config(
        cls,
        exclude_labels: Sequence[str] | None = None,
    ) -> "IssueLabelFilter":
        """Create a filter from configuration values.

        Args:
            exclude_labels: List of labels to exclude (from config)

        Returns:
            Configured IssueLabelFilter instance
        """
        return cls(
            exclude_labels=frozenset(exclude_labels or []),
        )

    def apply(self, issues: Sequence["Issue"]) -> list["Issue"]:
        """Filter issues, removing those matching exclusion criteria.

        Args:
            issues: List of issues to filter

        Returns:
            Filtered list with excluded issues removed
        """
        if not self.exclude_labels:
            return list(issues)

        return [
            issue for issue in issues
            if not self._should_exclude(issue)
        ]

    def _should_exclude(self, issue: "Issue") -> bool:
        """Check if an issue should be excluded.

        Args:
            issue: Issue to check

        Returns:
            True if issue should be excluded (has any excluded label)
        """
        issue_labels = set(issue.labels)
        return bool(issue_labels & self.exclude_labels)

    def is_empty(self) -> bool:
        """Check if filter has no criteria (passes everything)."""
        return not self.exclude_labels

    def __repr__(self) -> str:
        if self.is_empty():
            return "IssueLabelFilter()"
        return f"IssueLabelFilter(exclude={sorted(self.exclude_labels)})"
