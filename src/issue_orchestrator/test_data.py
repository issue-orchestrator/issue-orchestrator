"""Test data management for development and testing."""

import json
import subprocess
import time
from typing import Optional


def _run_gh(args: list[str], check: bool = False) -> subprocess.CompletedProcess:
    """Run gh CLI command with consistent settings."""
    return subprocess.run(
        ["gh"] + args,
        capture_output=True,
        text=True,
        check=check,
    )


def _ensure_label(repo: str, label: str) -> None:
    """Create label if it doesn't exist."""
    _run_gh(["label", "create", label, "--repo", repo, "--force"])


def _wait_for_issue_visible(repo: str, issue_number: int, timeout: int = 30) -> None:
    """Wait until issue is visible via GitHub API.

    GitHub has eventual consistency - an issue may not be immediately
    visible in list/view queries after creation.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = _run_gh(["issue", "view", str(issue_number), "--repo", repo, "--json", "number"])
        if result.returncode == 0:
            return
        time.sleep(1)

    raise TimeoutError(f"Issue #{issue_number} not visible after {timeout}s")


def create_issue(
    repo: str,
    title: str,
    labels: list[str],
    body: str = "E2E test issue.\n\nExpected: Agent completes.",
    wait_visible: bool = True,
    timeout: int = 30,
) -> int:
    """Create a single GitHub issue with all constraints honored.

    This is the canonical function for creating test issues. It:
    - Ensures all labels exist before creating the issue
    - Creates the issue with specified title, body, and labels
    - Waits for GitHub's eventual consistency (issue visible in API)

    Args:
        repo: GitHub repo in owner/repo format
        title: Issue title
        labels: List of labels to apply (will be created if missing)
        body: Issue body text
        wait_visible: If True, wait until issue is visible in API queries
        timeout: Seconds to wait for visibility

    Returns:
        Issue number

    Raises:
        RuntimeError: If issue creation fails
        TimeoutError: If issue not visible within timeout
    """
    # Ensure all labels exist
    for label in labels:
        _ensure_label(repo, label)

    # Build and execute create command
    cmd = [
        "issue", "create",
        "--repo", repo,
        "--title", title,
        "--body", body,
    ]
    for label in labels:
        cmd.extend(["--label", label])

    result = _run_gh(cmd)

    if result.returncode != 0:
        raise RuntimeError(f"Failed to create issue: {result.stderr}")

    # Parse issue number from URL (e.g., "https://github.com/owner/repo/issues/123")
    issue_url = result.stdout.strip()
    issue_number = int(issue_url.split("/")[-1])

    # Wait for GitHub eventual consistency
    if wait_visible:
        _wait_for_issue_visible(repo, issue_number, timeout)

    return issue_number


def update_issue(
    repo: str,
    issue_number: int,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
) -> None:
    """Update an issue's labels.

    Args:
        repo: GitHub repo in owner/repo format
        issue_number: The issue number to update
        add_labels: Labels to add
        remove_labels: Labels to remove
    """
    if add_labels:
        for label in add_labels:
            _ensure_label(repo, label)
        _run_gh(["issue", "edit", str(issue_number), "--repo", repo,
                 "--add-label", ",".join(add_labels)])

    if remove_labels:
        _run_gh(["issue", "edit", str(issue_number), "--repo", repo,
                 "--remove-label", ",".join(remove_labels)])


def close_issue(repo: str, issue_number: int, comment: str | None = None) -> None:
    """Close an issue.

    Args:
        repo: GitHub repo in owner/repo format
        issue_number: The issue number to close
        comment: Optional comment to add when closing
    """
    cmd = ["issue", "close", str(issue_number), "--repo", repo]
    if comment:
        cmd.extend(["--comment", comment])
    _run_gh(cmd)


def cleanup_issues_by_label(repo: str, label: str) -> int:
    """Close all issues with a specific label.

    Used for per-test cleanup before creating fresh issues.

    Args:
        repo: GitHub repo in owner/repo format
        label: The label to filter by

    Returns:
        Number of issues closed
    """
    result = _run_gh(["issue", "list", "--repo", repo, "--label", label,
                      "--state", "open", "--json", "number"])

    if result.returncode != 0:
        return 0

    issues = json.loads(result.stdout)
    for issue in issues:
        close_issue(repo, issue["number"], f"Cleaned up by test: {label}")

    return len(issues)


def cleanup_test_issues(repo: str) -> int:
    """Close all e2e test issues.

    Closes issues with 'test-data' label OR 'agent:e2e-test' label
    to ensure all test artifacts are cleaned up.

    Returns:
        Number of issues closed
    """
    count = 0
    seen = set()

    for label in ["test-data", "agent:e2e-test"]:
        result = _run_gh(["issue", "list", "--repo", repo, "--label", label,
                          "--state", "open", "--json", "number"])

        if result.returncode == 0:
            issues = json.loads(result.stdout)
            for issue in issues:
                num = issue["number"]
                if num not in seen:
                    seen.add(num)
                    _run_gh(["issue", "close", str(num), "--repo", repo,
                             "--comment", "Closed by test cleanup."])
                    count += 1

    return count


def create_test_issues(repo: str, agent_labels: Optional[list[str]] = None) -> list[int]:
    """Create multiple test issues for batch testing.

    Args:
        repo: GitHub repo in owner/repo format
        agent_labels: List of agent labels to use (e.g., ["agent:backend", "agent:frontend"])
                     Defaults to ["agent:backend", "agent:frontend", "agent:mobile"]

    Returns:
        List of created issue numbers
    """
    if agent_labels is None:
        agent_labels = ["agent:backend", "agent:frontend", "agent:mobile"]

    # Define test issues: (title, agent_label, optional_priority_label)
    test_issues = [
        ("[TEST] Simple backend task", agent_labels[0] if agent_labels else "agent:backend", "priority:high"),
        ("[TEST] Frontend feature", agent_labels[1] if len(agent_labels) > 1 else agent_labels[0], "priority:medium"),
        ("[TEST] Mobile bug fix", agent_labels[2] if len(agent_labels) > 2 else agent_labels[0], "priority:low"),
        ("[TEST] Task that will block", agent_labels[0] if agent_labels else "agent:backend", None),
        ("[TEST] Task with dependency", agent_labels[0] if agent_labels else "agent:backend", None),
    ]

    created_numbers = []

    for title, agent_label, priority_label in test_issues:
        labels = ["test-data", agent_label]
        if priority_label:
            labels.append(priority_label)

        issue_number = create_issue(
            repo=repo,
            title=title,
            labels=labels,
            body="Test issue for orchestrator.\n\nExpected: Agent completes.",
        )
        created_numbers.append(issue_number)

    return created_numbers
