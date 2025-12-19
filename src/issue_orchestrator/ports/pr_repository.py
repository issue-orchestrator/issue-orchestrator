"""Pull request repository port for PR operations.

This module defines the protocol (interface) for pull request operations and
the PRInfo data class for representing pull request data.
"""

from dataclasses import dataclass
from typing import Protocol


@dataclass
class PRInfo:
    """Information about a pull request.

    This data class represents the essential information about a pull request
    that the application needs to work with.

    Attributes:
        number: The PR number.
        title: The PR title.
        url: The URL to view the PR.
        branch: The head branch name (source branch).
        body: The PR description/body text.
        state: The PR state - "open", "closed", or "merged".
        labels: List of label names on the PR.
    """

    number: int
    title: str
    url: str
    branch: str
    body: str
    state: str  # "open", "closed", "merged"
    labels: list[str]


class PRRepository(Protocol):
    """Protocol for pull request repository operations.

    This protocol defines the interface for creating, retrieving, and managing
    pull requests. Implementations can use different backends (GitHub API,
    GitLab API, etc.) while maintaining the same interface.
    """

    def get_prs_for_branch(self, branch: str, state: str = "open") -> list[PRInfo]:
        """Get all pull requests for a specific branch.

        Args:
            branch: The head branch name to search for.
            state: Filter by PR state. Can be "open", "closed", "merged", or "all".
                  Defaults to "open".

        Returns:
            A list of PRInfo objects for PRs with the specified head branch.
            Returns empty list if no matching PRs found.

        Raises:
            RepositoryError: If there's an error accessing the data source.
        """
        ...

    def get_prs_with_label(self, label: str, state: str = "open") -> list[PRInfo]:
        """Get all pull requests with a specific label.

        Args:
            label: The label name to filter by.
            state: Filter by PR state. Can be "open", "closed", "merged", or "all".
                  Defaults to "open".

        Returns:
            A list of PRInfo objects for PRs with the specified label.
            Returns empty list if no matching PRs found.

        Raises:
            RepositoryError: If there's an error accessing the data source.
        """
        ...

    def get_pr(self, pr_number: int) -> PRInfo | None:
        """Get a specific pull request by number.

        Args:
            pr_number: The PR number to retrieve.

        Returns:
            The PRInfo object if found, None otherwise.

        Raises:
            RepositoryError: If there's an error accessing the data source.
        """
        ...

    def create_pr(
        self, title: str, body: str, head: str, base: str = "main"
    ) -> PRInfo:
        """Create a new pull request.

        Args:
            title: The title for the new PR.
            body: The description/body text for the PR.
            head: The head branch name (source branch with changes).
            base: The base branch name (target branch). Defaults to "main".

        Returns:
            A PRInfo object representing the newly created PR.

        Raises:
            RepositoryError: If there's an error creating the PR.
            BranchNotFoundError: If the head or base branch doesn't exist.
            ValidationError: If the title or body is invalid.
        """
        ...

    def add_comment(self, issue_or_pr_number: int, body: str) -> str:
        """Add a comment to an issue or pull request.

        Args:
            issue_or_pr_number: The issue or PR number to comment on.
            body: The comment text to add.

        Returns:
            The URL of the created comment.

        Raises:
            RepositoryError: If there's an error adding the comment.
            IssueNotFoundError: If the issue/PR doesn't exist.
            ValidationError: If the comment body is empty or invalid.
        """
        ...
