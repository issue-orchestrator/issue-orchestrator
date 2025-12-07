"""GitHub operations wrapper for gh CLI."""

import json
import re
import subprocess
from dataclasses import dataclass
from typing import Optional

from .models import Issue


class GitHubError(Exception):
    """Raised when a GitHub operation fails."""

    pass


def _run_gh(args: list[str], repo: str | None = None) -> str:
    """Run gh CLI command and return stdout.

    Args:
        args: Arguments to pass to gh command
        repo: Optional repository in owner/repo format

    Returns:
        stdout from the gh command

    Raises:
        GitHubError: If gh command fails
    """
    cmd = ["gh"] + args
    if repo:
        cmd.extend(["--repo", repo])

    result = subprocess.run(cmd, capture_output=True, text=True)

    if result.returncode != 0:
        raise GitHubError(f"gh command failed: {result.stderr}")

    return result.stdout


def _run_gh_json(args: list[str], repo: str | None = None) -> dict | list:
    """Run gh CLI command and parse JSON output.

    Args:
        args: Arguments to pass to gh command
        repo: Optional repository in owner/repo format

    Returns:
        Parsed JSON output from the gh command

    Raises:
        GitHubError: If gh command fails or output is invalid JSON
    """
    output = _run_gh(args, repo)
    try:
        return json.loads(output)
    except json.JSONDecodeError as e:
        raise GitHubError(f"Failed to parse gh JSON output: {e}") from e


def get_repo() -> str:
    """Get the current repository from git remote.

    Returns:
        Repository in owner/repo format

    Raises:
        GitHubError: If git remote cannot be determined
    """
    try:
        result = subprocess.run(
            ["git", "config", "--get", "remote.origin.url"],
            capture_output=True,
            text=True,
            check=False,
        )

        if result.returncode != 0:
            raise GitHubError("Could not determine repository from git remote")

        remote_url = result.stdout.strip()

        # Handle both HTTPS and SSH URLs
        if remote_url.startswith("https://github.com/"):
            repo = remote_url.replace("https://github.com/", "").replace(".git", "")
        elif remote_url.startswith("git@github.com:"):
            repo = remote_url.replace("git@github.com:", "").replace(".git", "")
        else:
            raise GitHubError(f"Unrecognized GitHub remote URL: {remote_url}")

        return repo
    except Exception as e:
        if isinstance(e, GitHubError):
            raise
        raise GitHubError(f"Failed to get repository: {e}") from e


def list_issues(
    repo: str | None = None,
    labels: list[str] | None = None,
    state: str = "open",
    milestone: str | None = None,
    limit: int = 100,
) -> list[Issue]:
    """List issues with given labels.

    Args:
        repo: Optional repository in owner/repo format. If None, uses current repo.
        labels: List of labels to filter by. All labels must be present.
        state: Issue state to filter by (default: "open")
        milestone: Optional milestone name to filter by
        limit: Maximum number of issues to fetch (default: 100, gh default is 30)

    Returns:
        List of Issue objects

    Raises:
        GitHubError: If GitHub operation fails
    """
    if labels is None:
        labels = []

    args = ["issue", "list", "--state", state, "--limit", str(limit), "--json", "number,title,labels,state,body,milestone"]

    for label in labels:
        args.extend(["--label", label])

    if milestone:
        args.extend(["--milestone", milestone])

    try:
        output = _run_gh_json(args, repo)
        issues = []

        if isinstance(output, list):
            for item in output:
                milestone_obj = item.get("milestone")
                issue = Issue(
                    number=item["number"],
                    title=item["title"],
                    labels=[label["name"] for label in item.get("labels", [])],
                    state=item.get("state", "open"),
                    body=item.get("body"),
                    milestone=milestone_obj.get("title") if milestone_obj else None,
                )
                issues.append(issue)

        return issues
    except GitHubError:
        raise
    except Exception as e:
        raise GitHubError(f"Failed to list issues: {e}") from e


def add_label(
    repo: str | None = None,
    issue_number: int | None = None,
    label: str | None = None,
) -> None:
    """Add a label to an issue.

    Args:
        repo: Optional repository in owner/repo format. If None, uses current repo.
        issue_number: Issue number
        label: Label to add

    Raises:
        GitHubError: If GitHub operation fails
        ValueError: If required arguments are missing
    """
    if issue_number is None:
        raise ValueError("issue_number is required")
    if label is None:
        raise ValueError("label is required")

    args = ["issue", "edit", str(issue_number), "--add-label", label]

    try:
        _run_gh(args, repo)
    except GitHubError:
        raise
    except Exception as e:
        raise GitHubError(f"Failed to add label: {e}") from e


def remove_label(
    repo: str | None = None,
    issue_number: int | None = None,
    label: str | None = None,
) -> None:
    """Remove a label from an issue.

    Args:
        repo: Optional repository in owner/repo format. If None, uses current repo.
        issue_number: Issue number
        label: Label to remove

    Raises:
        GitHubError: If GitHub operation fails
        ValueError: If required arguments are missing
    """
    if issue_number is None:
        raise ValueError("issue_number is required")
    if label is None:
        raise ValueError("label is required")

    args = ["issue", "edit", str(issue_number), "--remove-label", label]

    try:
        _run_gh(args, repo)
    except GitHubError:
        raise
    except Exception as e:
        raise GitHubError(f"Failed to remove label: {e}") from e


def add_comment(
    repo: str | None = None,
    issue_number: int | None = None,
    body: str | None = None,
) -> None:
    """Add a comment to an issue.

    Args:
        repo: Optional repository in owner/repo format. If None, uses current repo.
        issue_number: Issue number
        body: Comment body text

    Raises:
        GitHubError: If GitHub operation fails
        ValueError: If required arguments are missing
    """
    if issue_number is None:
        raise ValueError("issue_number is required")
    if body is None:
        raise ValueError("body is required")

    args = ["issue", "comment", str(issue_number), "--body", body]

    try:
        _run_gh(args, repo)
    except GitHubError:
        raise
    except Exception as e:
        raise GitHubError(f"Failed to add comment: {e}") from e


def get_open_prs_for_branch(
    repo: str | None = None,
    branch: str | None = None,
) -> list[dict]:
    """Check if PRs exist for a given branch.

    Args:
        repo: Optional repository in owner/repo format. If None, uses current repo.
        branch: Branch name to search for

    Returns:
        List of PR objects (dicts with PR data)

    Raises:
        GitHubError: If GitHub operation fails
        ValueError: If required arguments are missing
    """
    if branch is None:
        raise ValueError("branch is required")

    args = ["pr", "list", "--head", branch, "--state", "open", "--json", "number,title,url"]

    try:
        output = _run_gh_json(args, repo)

        if isinstance(output, list):
            return output
        return []
    except GitHubError:
        raise
    except Exception as e:
        raise GitHubError(f"Failed to get open PRs: {e}") from e


def get_issue_comments(repo: str | None, issue_number: int) -> list[dict]:
    """Get all comments on an issue, ordered oldest first."""
    args = ["issue", "view", str(issue_number), "--json", "comments"]
    output = _run_gh_json(args, repo)
    if isinstance(output, dict):
        return output.get("comments", [])
    return []


@dataclass
class BlockedInfo:
    """Parsed blocked information from a comment."""

    reason: str
    blocked_by: list[int]  # issue numbers
    attempted: str
    unblock_action: str
    comment_url: str
    timestamp: str


@dataclass
class NeedsHumanInfo:
    """Parsed needs-human question from a comment."""

    question: str
    context: str
    options: list[str]
    default_action: str
    comment_url: str
    timestamp: str


def get_latest_blocked_info(repo: str | None, issue_number: int) -> BlockedInfo | None:
    """Parse the latest '## 🚧 Blocked' section from issue comments.

    Returns None if no blocked section found.

    Expected format:
    ## 🚧 Blocked

    **Reason:** <reason text>
    **Blocked by:** #123, #456 (optional)
    **Attempted:** <what was tried>
    **Unblock action:** <what needs to happen>
    """
    comments = get_issue_comments(repo, issue_number)
    for comment in reversed(comments):  # Latest first
        body = comment.get("body", "")
        if "## 🚧 Blocked" in body or "## Blocked" in body:
            # Parse the fields using regex
            reason = _extract_field(body, "Reason")
            blocked_by = _extract_issue_numbers(body, "Blocked by")
            attempted = _extract_field(body, "Attempted")
            unblock_action = _extract_field(body, "Unblock action")

            return BlockedInfo(
                reason=reason or "Unknown",
                blocked_by=blocked_by,
                attempted=attempted or "",
                unblock_action=unblock_action or "",
                comment_url=comment.get("url", ""),
                timestamp=comment.get("createdAt", ""),
            )
    return None


def get_latest_needs_human_info(
    repo: str | None, issue_number: int
) -> NeedsHumanInfo | None:
    """Parse the latest '## ❓ Needs Human' section from issue comments.

    Returns None if no needs-human section found.

    Expected format:
    ## ❓ Needs Human

    **Question:** <question text>
    **Context:** <context>
    **Options:**
    1. <option 1>
    2. <option 2>
    **Default if no response:** <default action>
    """
    comments = get_issue_comments(repo, issue_number)
    for comment in reversed(comments):
        body = comment.get("body", "")
        if "## ❓ Needs Human" in body or "## Needs Human" in body:
            question = _extract_field(body, "Question")
            context = _extract_field(body, "Context")
            options = _extract_numbered_list(body, "Options")
            default = _extract_field(body, "Default if no response")

            return NeedsHumanInfo(
                question=question or "Unknown question",
                context=context or "",
                options=options,
                default_action=default or "",
                comment_url=comment.get("url", ""),
                timestamp=comment.get("createdAt", ""),
            )
    return None


def _extract_field(body: str, field_name: str) -> str | None:
    """Extract a **Field:** value from markdown body."""
    pattern = rf"\*\*{field_name}:\*\*\s*(.+?)(?=\n\*\*|\n##|\n\n|$)"
    match = re.search(pattern, body, re.IGNORECASE | re.DOTALL)
    return match.group(1).strip() if match else None


def _extract_issue_numbers(body: str, field_name: str) -> list[int]:
    """Extract issue numbers (#123) from a field."""
    field_value = _extract_field(body, field_name)
    if not field_value:
        return []
    numbers = re.findall(r"#(\d+)", field_value)
    return [int(n) for n in numbers]


def _extract_numbered_list(body: str, field_name: str) -> list[str]:
    """Extract a numbered list following a field."""
    # Find the field and capture everything until the next ** field or ## heading
    pattern = rf"\*\*{field_name}:\*\*\s*\n((?:\d+\..+\n?)+)"
    match = re.search(pattern, body, re.IGNORECASE)
    if not match:
        return []

    list_text = match.group(1)
    items = re.findall(r"\d+\.\s*(.+)", list_text)
    return [item.strip() for item in items]
