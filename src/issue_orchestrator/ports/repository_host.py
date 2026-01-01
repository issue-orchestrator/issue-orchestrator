"""Repository host port - combined interface for repository operations.

This module defines a combined protocol for all repository operations
that the orchestrator needs. This allows the orchestrator to depend on
a single port interface rather than importing concrete adapters.

Naming: "RepositoryHost" represents the external platform that hosts
issues, PRs, and labels. It combines IssueTracker, LabelSet, and
PullRequestTracker into a single interface.
"""

from typing import TYPE_CHECKING, Protocol

from .issue_tracker import IssueTracker
from .label_set import LabelSet
from .pull_request_tracker import PullRequestTracker

if TYPE_CHECKING:
    from ..domain.issue_key import IssueKey


class RepositoryHost(IssueTracker, LabelSet, PullRequestTracker, Protocol):
    """Combined protocol for all repository operations.

    This protocol extends IssueTracker, LabelSet, and PullRequestTracker
    to provide a unified interface for the orchestrator.

    Use this when you need access to issues, labels, and PRs from a single
    dependency. The orchestrator accepts this instead of concrete adapters.

    GitHubAdapter implements this protocol by implementing all three
    component protocols plus the get_issue_state method for dependency checking.
    """

    def get_issue_state(self, issue_number: int, repo: str | None = None) -> str | None:
        """Get the state of an issue ('open', 'closed', or None if not found).

        This method is used by DependencyEvaluator to check if blocking
        issues are closed.

        Args:
            issue_number: The issue number to check.
            repo: Optional repository in owner/repo format for cross-repo
                  dependencies. If None, uses the default repo.

        Returns:
            'open', 'closed', or None if the issue doesn't exist.
        """
        ...

    def create_issue_key(self, issue_number: int) -> "IssueKey":
        """Create an IssueKey for the given issue number.

        The adapter creates the appropriate IssueKey implementation
        for its backing store (e.g., GitHubIssueKey for GitHub).

        Args:
            issue_number: The issue number to create a key for.

        Returns:
            An IssueKey implementation appropriate for this repository host.
        """
        ...

    def create_issue(
        self,
        title: str,
        body: str,
        labels: list[str] | None = None,
    ) -> int | None:
        """Create a new issue.

        Args:
            title: Issue title
            body: Issue body
            labels: Labels to add

        Returns:
            Issue number if created, None on failure
        """
        ...

    def update_label_cache(self, issue_number: int, labels: list[str]) -> None:
        """Update cached labels for an issue.

        Adapters may implement a local cache to avoid repeated GH reads.
        """
        ...
