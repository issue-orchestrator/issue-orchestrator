"""Issue repository port for accessing issue data.

This module defines the protocol (interface) for issue data access operations.
Implementations of this protocol can use different data sources (GitHub API,
database, mock data, etc.) while maintaining the same interface.
"""

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from issue_orchestrator.models import Issue


class IssueRepository(Protocol):
    """Protocol for issue data access operations.

    This protocol defines the interface that any issue repository implementation
    must satisfy. It provides methods for retrieving and querying issues from
    the underlying data source.
    """

    def list_issues(
        self,
        labels: list[str] | None = None,
        milestone: str | None = None,
        state: str = "open",
        limit: int = 100,
    ) -> list["Issue"]:
        """List issues matching the given criteria.

        Args:
            labels: Filter by issues that have all of these labels.
                   If None, no label filtering is applied.
            milestone: Filter by milestone title. If None, no milestone
                      filtering is applied.
            state: Filter by issue state. Can be "open", "closed", or "all".
                  Defaults to "open".
            limit: Maximum number of issues to return. Defaults to 100.

        Returns:
            A list of Issue objects matching the criteria.

        Raises:
            RepositoryError: If there's an error accessing the data source.
        """
        ...

    def get_issue(self, issue_number: int) -> "Issue | None":
        """Get a specific issue by number.

        Args:
            issue_number: The issue number to retrieve.

        Returns:
            The Issue object if found, None otherwise.

        Raises:
            RepositoryError: If there's an error accessing the data source.
        """
        ...

    def get_issue_labels(self, issue_number: int) -> list[str]:
        """Get the labels for a specific issue.

        Args:
            issue_number: The issue number to get labels for.

        Returns:
            A list of label names for the issue. Returns empty list if
            issue not found or has no labels.

        Raises:
            RepositoryError: If there's an error accessing the data source.
        """
        ...
