"""Label set port for label operations.

This module defines the protocol (interface) for managing labels on issues.
Implementations can interact with different platforms (GitHub, GitLab, etc.)
while maintaining the same interface.

Naming: "LabelSet" is a neutral noun implying CRUD operations on a set of labels,
without policy implication. This is an execution-layer interface.
"""

from typing import Protocol


class LabelSet(Protocol):
    """Protocol for label operations.

    This protocol defines the interface for adding, removing, and checking
    labels on issues. Implementations handle the actual interaction with
    the underlying platform (e.g., GitHub API).

    Naming: "LabelSet" (not "LabelManager") because "Manager" implies policy
    decisions. This is just CRUD on a set of labels.
    """

    def add_label(self, issue_number: int, label: str) -> None:
        """Add a label to an issue.

        If the label already exists on the issue, this is a no-op.
        The label will be created in the repository if it doesn't exist.

        Args:
            issue_number: The issue number to add the label to.
            label: The label name to add.

        Raises:
            LabelError: If there's an error adding the label.
            IssueNotFoundError: If the issue doesn't exist.
        """
        ...

    def remove_label(self, issue_number: int, label: str) -> None:
        """Remove a label from an issue.

        If the label doesn't exist on the issue, this is a no-op.

        Args:
            issue_number: The issue number to remove the label from.
            label: The label name to remove.

        Raises:
            LabelError: If there's an error removing the label.
            IssueNotFoundError: If the issue doesn't exist.
        """
        ...

    def has_label(self, issue_number: int, label: str) -> bool:
        """Check if an issue has a specific label.

        Args:
            issue_number: The issue number to check.
            label: The label name to check for.

        Returns:
            True if the issue has the label, False otherwise.
            Returns False if the issue doesn't exist.

        Raises:
            LabelError: If there's an error checking the label.
        """
        ...


# Backwards compatibility alias
LabelManager = LabelSet
