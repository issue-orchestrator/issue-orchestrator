"""Issue filtering by labels.

This module provides a shared label-based filter for issues. The filter can
exclude issues that have any of a set of exact labels or any label matching a
configured prefix.

Usage:
    filter = IssueLabelFilter.from_config(
        exclude_labels=["test-data"],
        exclude_label_prefixes=["io:e2e:"],
    )
    filtered = filter.apply(issues)
"""

from dataclasses import dataclass, field
from typing import Sequence, TYPE_CHECKING

if TYPE_CHECKING:
    from ..ports.issue import Issue


@dataclass
class IssueLabelFilter:
    """Filter issues based on label criteria.

    This filter excludes issues matching any configured exact label or label
    prefix. Can be extended or replaced with more complex filtering logic in
    the future.

    Attributes:
        exclude_labels: Issues with ANY of these labels are excluded.
            Labels are matched exactly (case-sensitive).
        exclude_label_prefixes: Issues with ANY label starting with one of these
            prefixes are excluded (case-sensitive).
    """

    exclude_labels: frozenset[str] = field(default_factory=frozenset)
    exclude_label_prefixes: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_config(
        cls,
        exclude_labels: Sequence[str] | None = None,
        exclude_label_prefixes: Sequence[str] | None = None,
    ) -> "IssueLabelFilter":
        """Create a filter from configuration values.

        Args:
            exclude_labels: List of labels to exclude (from config)
            exclude_label_prefixes: Label prefixes to exclude (from config)

        Returns:
            Configured IssueLabelFilter instance
        """
        return cls(
            exclude_labels=frozenset(exclude_labels or []),
            exclude_label_prefixes=tuple(prefix for prefix in (exclude_label_prefixes or []) if prefix),
        )

    def apply(self, issues: Sequence["Issue"]) -> list["Issue"]:
        """Filter issues, removing those matching exclusion criteria.

        Args:
            issues: List of issues to filter

        Returns:
            Filtered list with excluded issues removed
        """
        if self.is_empty():
            return list(issues)

        return [issue for issue in issues if self.exclusion_reason(issue) is None]

    def exclusion_reason(self, issue: "Issue") -> str | None:
        """Return why an issue is excluded, or None when it passes.

        Args:
            issue: Issue to check

        Returns:
            Human-readable exclusion reason when the issue is excluded.
        """
        issue_labels = tuple(issue.labels)
        for label in issue_labels:
            if label in self.exclude_labels:
                return f'has excluded label "{label}"'
        for label in issue_labels:
            for prefix in self.exclude_label_prefixes:
                if label.startswith(prefix):
                    return f'has label "{label}" matching excluded prefix "{prefix}"'
        return None

    def is_empty(self) -> bool:
        """Check if filter has no criteria (passes everything)."""
        return not self.exclude_labels and not self.exclude_label_prefixes

    def __repr__(self) -> str:
        if self.is_empty():
            return "IssueLabelFilter()"
        parts: list[str] = []
        if self.exclude_labels:
            parts.append(f"exclude={sorted(self.exclude_labels)}")
        if self.exclude_label_prefixes:
            parts.append(f"exclude_prefixes={list(self.exclude_label_prefixes)}")
        return f"IssueLabelFilter({', '.join(parts)})"
