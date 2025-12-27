"""GitHub adapter implementing platform port interfaces.

This module provides a GitHubAdapter class that implements the IssueTracker,
LabelSet, and PullRequestTracker protocols using the GitHub CLI (gh).

The adapter wraps existing github.py functions and provides a unified interface
for all GitHub operations required by the application.

Naming: This is an execution-layer adapter that talks to an external platform.
"""

import logging
import re
from typing import TYPE_CHECKING

from ..ports.issue_tracker import IssueTracker
from ..ports.label_set import LabelSet
from ..ports.pull_request_tracker import PRInfo, PullRequestTracker
from .. import _github_impl as github
from .github_issue import GitHubIssue

if TYPE_CHECKING:
    from ..domain.issue_key import IssueKey, GitHubIssueKey

logger = logging.getLogger(__name__)


class GitHubAdapter:
    """Adapter for GitHub operations via gh CLI.

    This adapter implements the IssueTracker, LabelSet, and PullRequestTracker
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
    ) -> list[GitHubIssue]:
        """List issues matching the given criteria.

        Args:
            labels: Filter by issues that have all of these labels.
            milestone: Filter by milestone title.
            state: Filter by issue state ("open", "closed", or "all").
            limit: Maximum number of issues to return.

        Returns:
            List of GitHubIssue objects matching the criteria. Returns empty list on error.
        """
        try:
            # Get raw issues from github module (returns old Issue type)
            raw_issues = github.list_issues(
                repo=self.repo,
                labels=labels,
                state=state,
                milestone=milestone,
                limit=limit,
            )
            # Convert to GitHubIssue (frozen, with key-based equality)
            return [
                GitHubIssue(
                    number=issue.number,
                    repo=self.repo,
                    title=issue.title,
                    labels=tuple(issue.labels),
                    state=issue.state,
                    body=issue.body,
                    milestone=issue.milestone,
                    milestone_number=issue.milestone_number,
                    milestone_due_on=issue.milestone_due_on,
                )
                for issue in raw_issues
            ]
        except github.GitHubError as e:
            logger.error(f"Failed to list issues: {e}")
            return []

    def get_issue(self, issue_number: int) -> GitHubIssue | None:
        """Get a specific issue by number.

        Args:
            issue_number: The issue number to retrieve.

        Returns:
            The GitHubIssue object if found, None otherwise.
        """
        try:
            # Use gh issue view to get a specific issue
            args = ["issue", "view", str(issue_number), "--json", "number,title,labels,state,body,milestone"]
            output = github._run_gh_json(args, self.repo)

            if isinstance(output, dict):
                milestone_obj = output.get("milestone")
                return GitHubIssue(
                    number=output["number"],
                    repo=self.repo,
                    title=output["title"],
                    labels=tuple(label["name"] for label in output.get("labels", [])),
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

    def get_issue_by_key(self, key: "IssueKey") -> GitHubIssue | None:
        """Get an issue by its IssueKey.

        This is the reverse lookup: IssueKey -> GitHubIssue.
        For GitHubIssueKey, extracts the issue number and fetches.

        Args:
            key: The IssueKey to look up.

        Returns:
            The GitHubIssue if found, None otherwise.
        """
        from ..domain.issue_key import GitHubIssueKey

        if isinstance(key, GitHubIssueKey):
            # If external_id is numeric, it's the issue number
            if key.external_id.isdigit():
                return self.get_issue(int(key.external_id))
            # Otherwise, we need to search - for now, return None
            # Future: could search by title prefix
            logger.warning(f"Cannot reverse lookup non-numeric external_id: {key}")
            return None
        else:
            logger.warning(f"Cannot lookup non-GitHub IssueKey: {key}")
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

    def get_prs_for_issue(self, issue_number: int, state: str = "open") -> list[PRInfo]:
        """Get all pull requests associated with a specific issue.

        Finds PRs where:
        - Branch starts with the issue number followed by a dash (e.g., "328-feature-name")
        - OR title contains "#issue_number" (e.g., "#328: Feature")

        Args:
            issue_number: The issue number to find PRs for.
            state: Filter by PR state ("open", "closed", "merged", or "all").

        Returns:
            List of PRInfo objects. Returns empty list on error.
        """
        try:
            # Use the existing github.get_prs_for_issue function
            prs = github.get_prs_for_issue(repo=self.repo, issue_number=issue_number)
            return [
                PRInfo(
                    number=pr["number"],
                    title=pr["title"],
                    url=pr.get("url", ""),
                    branch=pr.get("headRefName", ""),
                    body=pr.get("body", ""),
                    state=pr.get("state", "OPEN"),
                    labels=[label["name"] for label in pr.get("labels", [])],
                )
                for pr in prs
            ]
        except github.GitHubError as e:
            logger.error(f"Failed to get PRs for issue {issue_number}: {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error getting PRs for issue {issue_number}: {e}")
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

    def list_prs(self, state: str = "open", limit: int = 100) -> list[PRInfo]:
        """List pull requests.

        Args:
            state: Filter by PR state ("open", "closed", "merged", or "all").
            limit: Maximum number of PRs to return.

        Returns:
            List of PRInfo objects. Returns empty list on error.
        """
        try:
            args = ["pr", "list", "--state", state, "--limit", str(limit),
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
            logger.error(f"Failed to list PRs: {e}")
            return []
        except Exception as e:
            logger.error(f"Unexpected error listing PRs: {e}")
            return []

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
            # Create the PR (gh pr create doesn't support --json)
            args = ["pr", "create", "--title", title, "--body", body,
                   "--head", head, "--base", base]
            result = github._run_gh(args, self.repo)

            # Parse the URL from the output (gh pr create prints the PR URL)
            pr_url = result.strip()
            if not pr_url.startswith("https://"):
                raise github.GitHubError(f"Unexpected output from gh pr create: {result}")

            # Extract PR number from URL (https://github.com/owner/repo/pull/123)
            pr_number = int(pr_url.split("/")[-1])

            # Fetch full PR info using gh pr view
            view_args = ["pr", "view", str(pr_number),
                        "--json", "number,title,url,headRefName,body,state,labels"]
            output = github._run_gh_json(view_args, self.repo)

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
                raise github.GitHubError("Failed to parse PR view response")
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

    def get_issue_state(self, issue_number: int, repo: str | None = None) -> str | None:
        """Get the state of an issue ('open', 'closed', or None if not found).

        This method implements the IssueStateChecker protocol for dependency evaluation.

        Args:
            issue_number: The issue number to check.
            repo: Optional repository in owner/repo format for cross-repo dependencies.
                  If None, uses this adapter's configured repo.

        Returns:
            The issue state ('open' or 'closed'), or None if the issue cannot be found.
        """
        target_repo = repo or self.repo
        try:
            # Use gh issue view to get the issue state
            args = ["issue", "view", str(issue_number), "--json", "state"]
            output = github._run_gh_json(args, target_repo)

            if isinstance(output, dict):
                return output.get("state")
            return None
        except github.GitHubError as e:
            # 404 or permission error - issue is missing
            logger.debug(f"Issue {issue_number} in {target_repo} not found: {e}")
            return None
        except Exception as e:
            # Re-raise unexpected errors for UNKNOWN state handling
            logger.debug(f"Error checking issue {issue_number} in {target_repo}: {e}")
            raise

    def create_issue_key(self, issue_number: int) -> "GitHubIssueKey":
        """Create a GitHubIssueKey for the given issue number.

        This allows the orchestrator to get IssueKeys without knowing about
        GitHub-specific implementations.

        The method fetches the issue to extract the stable external_id from the
        title (e.g., "[M1-011] Fix login bug" -> external_id="M1-011").
        Falls back to using the issue number as external_id if the issue can't
        be fetched or has no external_id prefix in its title.

        Args:
            issue_number: The issue number to create a key for.

        Returns:
            A GitHubIssueKey with this adapter's repo and the parsed external_id.
        """
        from ..domain.issue_key import GitHubIssueKey, parse_external_id

        # Try to fetch the issue to get the stable external_id from title
        issue = self.get_issue(issue_number)
        if issue:
            parsed = parse_external_id(issue.title)
            if parsed.external_id:
                return GitHubIssueKey(repo=self.repo, external_id=parsed.external_id)

        # Fall back to issue number if no external_id found
        return GitHubIssueKey(repo=self.repo, external_id=str(issue_number))

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
        return github.create_issue(
            repo=self.repo,
            title=title,
            body=body,
            labels=labels,
        )
