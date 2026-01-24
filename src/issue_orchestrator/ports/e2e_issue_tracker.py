"""E2E issue tracker port for managing composite issues from E2E test failures.

This module defines the protocol (interface) for creating and managing GitHub issues
from E2E test failures. It supports:
- Parent issues for E2E runs
- Sub-issues for individual test failures
- Auto-closing when tests pass
"""

from dataclasses import dataclass
from typing import Protocol

from ..infra.e2e_db import E2ERun, E2ETestResult


@dataclass(frozen=True)
class E2EParentIssueInfo:
    """Information about a created parent issue for an E2E run."""

    issue_number: int
    html_url: str
    node_id: str  # GraphQL node ID for sub-issue linking


@dataclass(frozen=True)
class E2ESubIssueInfo:
    """Information about a created sub-issue for a test failure."""

    issue_number: int
    html_url: str
    parent_issue_number: int
    nodeid: str


class E2EIssueTracker(Protocol):
    """Protocol for creating and managing E2E test failure issues.

    This protocol defines the interface for:
    - Creating parent issues for E2E runs with failures
    - Creating sub-issues for individual test failures
    - Linking sub-issues to parent issues
    - Auto-closing issues when tests pass
    """

    def create_run_issue(
        self,
        run: E2ERun,
        failed_count: int,
        labels: list[str] | None = None,
    ) -> E2EParentIssueInfo | None:
        """Create a parent issue for an E2E run with failures.

        Args:
            run: The E2E run that failed
            failed_count: Number of failed tests
            labels: Labels to add (e.g., ["e2e:run"])

        Returns:
            E2EParentIssueInfo with issue details, or None on failure
        """
        ...

    def create_test_failure_issue(
        self,
        parent_issue: E2EParentIssueInfo,
        test_result: E2ETestResult,
        first_failing_sha: str,
        last_passing_sha: str | None,
        labels: list[str] | None = None,
    ) -> E2ESubIssueInfo | None:
        """Create a sub-issue for an individual test failure.

        Args:
            parent_issue: The parent issue to link to
            test_result: The failing test result
            first_failing_sha: Commit SHA where test first failed
            last_passing_sha: Last commit SHA where test passed (if known)
            labels: Labels to add (e.g., ["e2e:test-failure", "agent:developer"])

        Returns:
            E2ESubIssueInfo with issue details, or None on failure
        """
        ...

    def close_issue_with_comment(
        self,
        issue_number: int,
        comment: str,
    ) -> bool:
        """Close an issue with a comment explaining the resolution.

        Args:
            issue_number: Issue to close
            comment: Explanation (e.g., "Test now passing as of run #N")

        Returns:
            True if closed successfully, False otherwise
        """
        ...

    def add_sub_issue(
        self,
        parent_node_id: str,
        child_issue_number: int,
    ) -> bool:
        """Link an issue as a sub-issue of a parent.

        Uses GitHub's sub-issues API (GraphQL).

        Args:
            parent_node_id: GraphQL node ID of the parent issue
            child_issue_number: Issue number of the child to link

        Returns:
            True if linked successfully, False otherwise
        """
        ...

    def get_issue_node_id(self, issue_number: int) -> str | None:
        """Get the GraphQL node ID for an issue.

        Args:
            issue_number: Issue number

        Returns:
            Node ID string or None if not found
        """
        ...
