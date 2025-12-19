"""GitHub adapter implementing repository port interfaces.

This module provides a GitHubAdapter class that implements the IssueRepository,
LabelManager, and PRRepository protocols using the GitHub CLI (gh).

The adapter wraps existing github.py functions and provides a unified interface
for all GitHub operations required by the application.
"""

import logging
import re
from typing import TYPE_CHECKING

from ..ports.issue_repository import IssueRepository
from ..ports.label_manager import LabelManager
from ..ports.pr_repository import PRInfo, PRRepository
from .. import github
from ..models import Issue

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


class GitHubAdapter:
    """Adapter for GitHub operations via gh CLI.

    This adapter implements the IssueRepository, LabelManager, and PRRepository
    protocols, providing a unified interface for GitHub operations.

    The adapter uses the existing github.py module functions and handles errors
    gracefully by returning None or empty lists on failure.

    Args:
        repo: Repository in owner/repo format (e.g., "owner/repo").
              If None, the current repository is determined from git remote.

    Example:
        >>> adapter = GitHubAdapter("myorg/myrepo")
        >>> issues = adapter.list_issues(labels=["bug"], state="open")
        >>> adapter.add_label(42, "in-progress")
        >>> pr = adapter.create_pr("Fix bug", "This fixes the bug", "feature-branch")
    """

    def __init__(self, repo: str | None = None):
        """Initialize the GitHub adapter.

        Args:
            repo: Repository in owner/repo format. If None, uses current repo.
        """
        self.repo = repo or github.get_repo()
        logger.info(f"GitHubAdapter initialized for repo: {self.repo}")

    # IssueRepository implementation

    def list_issues(
        self,
        labels: list[str] | None = None,
        milestone: str | None = None,
        state: str = "open",
        limit: int = 100,
    ) -> list[Issue]:
        """List issues matching the given criteria.

        Args:
            labels: Filter by issues that have all of these labels.
            milestone: Filter by milestone title.
            state: Filter by issue state ("open", "closed", or "all").
            limit: Maximum number of issues to return.

        Returns:
            List of Issue objects matching the criteria. Returns empty list on error.
        """
        try:
            return github.list_issues(
                repo=self.repo,
                labels=labels,
                state=state,
                milestone=milestone,
                limit=limit,
            )
        except github.GitHubError as e:
            logger.error(f"Failed to list issues: {e}")
            return []

    def get_issue(self, issue_number: int) -> Issue | None:
        """Get a specific issue by number.

        This method retrieves a single issue by listing issues with a limit of 1.
        An alternative approach would be to use 'gh issue view' with JSON output.

        Args:
            issue_number: The issue number to retrieve.

        Returns:
            The Issue object if found, None otherwise.
        """
        try:
            # Use gh issue view to get a specific issue
            args = ["issue", "view", str(issue_number), "--json", "number,title,labels,state,body,milestone"]
            output = github._run_gh_json(args, self.repo)

            if isinstance(output, dict):
                milestone_obj = output.get("milestone")
                return Issue(
                    number=output["number"],
                    title=output["title"],
                    labels=[label["name"] for label in output.get("labels", [])],
                    state=output.get("state", "open"),
                    body=output.get("body"),
                    milestone=milestone_obj.get("title") if milestone_obj else None,
                    milestone_number=milestone_obj.get("number") if milestone_obj else None,
                    milestone_due_on=milestone_obj.get("dueOn") if milestone_obj else None,
                )
            return None
        except github.GitHubError as e:
            logger.error(f"Failed to get issue {issue_number}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error getting issue {issue_number}: {e}")
            return None

    def get_issue_labels(self, issue_number: int) -> list[str]:
        """Get the labels for a specific issue.

        Args:
            issue_number: The issue number to get labels for.

        Returns:
            List of label names. Returns empty list on error or if issue not found.
        """
        try:
            return github.get_issue_labels(self.repo, issue_number)
        except Exception as e:
            logger.error(f"Failed to get labels for issue {issue_number}: {e}")
            return []

    # LabelManager implementation

    def add_label(self, issue_number: int, label: str) -> None:
        """Add a label to an issue.

        Args:
            issue_number: The issue number to add the label to.
            label: The label name to add.

        Raises:
            github.GitHubError: If the operation fails.
        """
        try:
            github.add_label(repo=self.repo, issue_number=issue_number, label=label)
            logger.debug(f"Added label '{label}' to issue {issue_number}")
        except github.GitHubError:
            logger.error(f"Failed to add label '{label}' to issue {issue_number}")
            raise

    def remove_label(self, issue_number: int, label: str) -> None:
        """Remove a label from an issue.

        Args:
            issue_number: The issue number to remove the label from.
            label: The label name to remove.

        Raises:
            github.GitHubError: If the operation fails.
        """
        try:
            github.remove_label(repo=self.repo, issue_number=issue_number, label=label)
            logger.debug(f"Removed label '{label}' from issue {issue_number}")
        except github.GitHubError:
            logger.error(f"Failed to remove label '{label}' from issue {issue_number}")
            raise

    def has_label(self, issue_number: int, label: str) -> bool:
        """Check if an issue has a specific label.

        Args:
            issue_number: The issue number to check.
            label: The label name to check for.

        Returns:
            True if the issue has the label, False otherwise.
        """
        try:
            labels = self.get_issue_labels(issue_number)
            return label in labels
        except Exception as e:
            logger.error(f"Failed to check label '{label}' on issue {issue_number}: {e}")
            return False

    # PRRepository implementation

    def get_prs_for_branch(self, branch: str, state: str = "open") -> list[PRInfo]:
        """Get all pull requests for a specific branch.

        Args:
            branch: The head branch name to search for.
            state: Filter by PR state ("open", "closed", "merged", or "all").

        Returns:
            List of PRInfo objects. Returns empty list on error.
        """
        try:
            args = ["pr", "list", "--head", branch, "--state", state,
                   "--json", "number,title,url,headRefName,body,state,labels"]
            output = github._run_gh_json(args, self.repo)

            if isinstance(output, list):
                return [
                    PRInfo(
                        number=pr["number"],
                        title=pr["title"],
                        url=pr["url"],
                        branch=pr["headRefName"],
                        body=pr.get("body", ""),
                        state=pr["state"],
                        labels=[label["name"] for label in pr.get("labels", [])],
                    )
                    for pr in output
                ]
            return []
        except github.GitHubError as e:
            logger.error(f"Failed to get PRs for branch '{branch}': {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error getting PRs for branch '{branch}': {e}")
            return []

    def get_prs_with_label(self, label: str, state: str = "open") -> list[PRInfo]:
        """Get all pull requests with a specific label.

        Args:
            label: The label name to filter by.
            state: Filter by PR state ("open", "closed", "merged", or "all").

        Returns:
            List of PRInfo objects. Returns empty list on error.
        """
        try:
            args = ["pr", "list", "--label", label, "--state", state,
                   "--json", "number,title,url,headRefName,body,state,labels"]
            output = github._run_gh_json(args, self.repo)

            if isinstance(output, list):
                return [
                    PRInfo(
                        number=pr["number"],
                        title=pr["title"],
                        url=pr["url"],
                        branch=pr["headRefName"],
                        body=pr.get("body", ""),
                        state=pr["state"],
                        labels=[label["name"] for label in pr.get("labels", [])],
                    )
                    for pr in output
                ]
            return []
        except github.GitHubError as e:
            logger.error(f"Failed to get PRs with label '{label}': {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error getting PRs with label '{label}': {e}")
            return []

    def get_pr(self, pr_number: int) -> PRInfo | None:
        """Get a specific pull request by number.

        Args:
            pr_number: The PR number to retrieve.

        Returns:
            The PRInfo object if found, None otherwise.
        """
        try:
            args = ["pr", "view", str(pr_number),
                   "--json", "number,title,url,headRefName,body,state,labels"]
            output = github._run_gh_json(args, self.repo)

            if isinstance(output, dict):
                return PRInfo(
                    number=output["number"],
                    title=output["title"],
                    url=output["url"],
                    branch=output["headRefName"],
                    body=output.get("body", ""),
                    state=output["state"],
                    labels=[label["name"] for label in output.get("labels", [])],
                )
            return None
        except github.GitHubError as e:
            logger.error(f"Failed to get PR {pr_number}: {e}")
            return None
        except Exception as e:
            logger.error(f"Unexpected error getting PR {pr_number}: {e}")
            return None

    def create_pr(
        self, title: str, body: str, head: str, base: str = "main"
    ) -> PRInfo:
        """Create a new pull request.

        Args:
            title: The title for the new PR.
            body: The description/body text for the PR.
            head: The head branch name (source branch with changes).
            base: The base branch name (target branch).

        Returns:
            A PRInfo object representing the newly created PR.

        Raises:
            github.GitHubError: If there's an error creating the PR.
        """
        try:
            args = ["pr", "create", "--title", title, "--body", body,
                   "--head", head, "--base", base,
                   "--json", "number,title,url,headRefName,body,state,labels"]
            output = github._run_gh_json(args, self.repo)

            if isinstance(output, dict):
                pr_info = PRInfo(
                    number=output["number"],
                    title=output["title"],
                    url=output["url"],
                    branch=output["headRefName"],
                    body=output.get("body", ""),
                    state=output["state"],
                    labels=[label["name"] for label in output.get("labels", [])],
                )
                logger.info(f"Created PR #{pr_info.number}: {pr_info.title}")
                return pr_info
            else:
                raise github.GitHubError("Failed to parse PR creation response")
        except github.GitHubError:
            logger.error(f"Failed to create PR: {title}")
            raise

    def add_comment(self, issue_or_pr_number: int, body: str) -> str:
        """Add a comment to an issue or pull request.

        Args:
            issue_or_pr_number: The issue or PR number to comment on.
            body: The comment text to add.

        Returns:
            The URL of the created comment.

        Raises:
            github.GitHubError: If there's an error adding the comment.
        """
        try:
            # The gh CLI doesn't return the comment URL directly from 'issue comment'
            # So we add the comment and then get the latest comment to retrieve its URL
            github.add_comment(repo=self.repo, issue_number=issue_or_pr_number, body=body)
            logger.debug(f"Added comment to issue/PR {issue_or_pr_number}")

            # Fetch the latest comment to get its URL
            # This is a workaround since gh issue comment doesn't output JSON
            args = ["issue", "view", str(issue_or_pr_number), "--json", "comments"]
            output = github._run_gh_json(args, self.repo)

            if isinstance(output, dict):
                comments = output.get("comments", [])
                if comments:
                    # Return the URL of the last comment (the one we just added)
                    return comments[-1].get("url", f"https://github.com/{self.repo}/issues/{issue_or_pr_number}")

            # Fallback to issue URL if we can't get the comment URL
            return f"https://github.com/{self.repo}/issues/{issue_or_pr_number}"
        except github.GitHubError:
            logger.error(f"Failed to add comment to issue/PR {issue_or_pr_number}")
            raise
