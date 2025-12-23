"""Repository host port - combined interface for repository operations.

This module defines a combined protocol for all repository operations
that the orchestrator needs. This allows the orchestrator to depend on
a single port interface rather than importing concrete adapters.

Naming: "RepositoryHost" represents the external platform that hosts
issues, PRs, and labels. It combines IssueTracker, LabelSet, and
PullRequestTracker into a single interface.
"""

from typing import Protocol

from .issue_tracker import IssueTracker
from .label_set import LabelSet
from .pull_request_tracker import PullRequestTracker


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
