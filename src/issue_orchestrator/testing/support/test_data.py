"""Test data management for development and testing.

This module provides functions for creating and managing test data on GitHub.
All GitHub access is routed through GitHubAdapter for consistent auditing,
caching, and rate-limit handling.
"""

import logging
import random
import time
from typing import Optional

from ...infra import gh_audit
from ...infra.config import Config
from ...adapters.github import GitHubAdapter
from ...adapters.github.errors import GitHubHttpError
from ...adapters.github.ref_claim_adapter import CLAIM_REF_PREFIX

logger = logging.getLogger(__name__)

_adapter_cache: dict[str, GitHubAdapter] = {}


def _adapter_for(repo: str) -> GitHubAdapter:
    """Get or create a GitHubAdapter for the given repo.

    Uses a cache to reuse adapters across calls within the same process.
    """
    adapter = _adapter_cache.get(repo)
    if adapter is None:
        adapter = GitHubAdapter(repo=repo)
        _adapter_cache[repo] = adapter
    return adapter


def _ensure_label(repo: str, label: str) -> None:
    """Create label if it doesn't exist."""
    with gh_audit.context(reason=gh_audit.AuditReason.TEST_DATA_LABEL, scope=gh_audit.AuditScope.TEST):
        _adapter_for(repo).create_label(label, force=True)


def _wait_for_issue_visible(
    repo: str,
    issue_number: int,
    labels: list[str] | None = None,
    timeout: int | None = None,
) -> None:
    """Wait until issue is visible via GitHub API and labels are applied.

    GitHub has eventual consistency - an issue may not be immediately
    visible in list/view queries after creation.
    """
    cfg = Config()
    effective_timeout = timeout if timeout is not None else cfg.gh_write_verify_timeout_seconds
    deadline = time.time() + effective_timeout
    delay_s = cfg.gh_write_verify_initial_delay_ms / 1000.0
    max_delay_s = cfg.gh_write_verify_max_delay_ms / 1000.0
    backoff = cfg.gh_write_verify_backoff
    jitter_s = cfg.gh_write_verify_jitter_ms / 1000.0
    adapter = _adapter_for(repo)
    while time.time() < deadline:
        with gh_audit.context(reason=gh_audit.AuditReason.GH_READ, scope=gh_audit.AuditScope.TEST):
            issue = adapter.get_issue(issue_number)
        if issue is not None:
            if labels:
                # GitHubIssue.labels is a tuple of label names
                if all(label in issue.labels for label in labels):
                    return
            else:
                return
        if delay_s > 0:
            time.sleep(delay_s + (random.random() * jitter_s))
        delay_s = min(delay_s * backoff, max_delay_s)

    raise TimeoutError(f"Issue #{issue_number} not visible after {effective_timeout}s")


def create_issue(
    repo: str,
    title: str,
    labels: list[str],
    body: str = "E2E test issue.\n\nExpected: Agent completes.",
    wait_visible: bool = True,
    timeout: int | None = None,
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
    adapter = _adapter_for(repo)

    # Ensure all labels exist
    for label in labels:
        _ensure_label(repo, label)

    with gh_audit.context(reason=gh_audit.AuditReason.TEST_DATA_CREATE, scope=gh_audit.AuditScope.TEST):
        result = adapter.create_issue(title=title, body=body, labels=labels)

    if result is None:
        raise RuntimeError("Failed to create issue")

    issue_number = result.get("number")
    if issue_number is None:
        raise RuntimeError("Issue created but no number returned")

    # Wait for GitHub eventual consistency
    if wait_visible:
        _wait_for_issue_visible(repo, issue_number, labels, timeout)

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
    adapter = _adapter_for(repo)

    if add_labels:
        for label in add_labels:
            _ensure_label(repo, label)
        with gh_audit.context(reason=gh_audit.AuditReason.TEST_DATA_UPDATE, scope=gh_audit.AuditScope.TEST):
            for label in add_labels:
                adapter.add_label(issue_number, label)

    if remove_labels:
        with gh_audit.context(reason=gh_audit.AuditReason.TEST_DATA_UPDATE, scope=gh_audit.AuditScope.TEST):
            for label in remove_labels:
                adapter.remove_label(issue_number, label)


def close_issue(repo: str, issue_number: int, comment: str | None = None) -> None:
    """Close an issue.

    Args:
        repo: GitHub repo in owner/repo format
        issue_number: The issue number to close
        comment: Optional comment to add when closing
    """
    adapter = _adapter_for(repo)
    with gh_audit.context(reason=gh_audit.AuditReason.TEST_DATA_CLOSE, scope=gh_audit.AuditScope.TEST):
        if comment:
            adapter.add_comment(issue_number, comment)
        adapter.update_issue_state(issue_number, "closed")


def _delete_claim_ref_if_present(repo: str, issue_number: int) -> None:
    """Remove test claim refs for issues being cleaned up."""
    try:
        _adapter_for(repo).http_client.delete_git_ref(
            f"{CLAIM_REF_PREFIX}/issue-{issue_number}"
        )
    except GitHubHttpError as exc:
        if exc.status_code != 404:
            logger.warning("Failed to delete claim ref for issue #%d: %s", issue_number, exc)
    except Exception as exc:
        logger.warning("Failed to delete claim ref for issue #%d: %s", issue_number, exc)


def cleanup_issues_by_label(repo: str, label: str) -> int:
    """Close all issues with a specific label.

    Used for per-test cleanup before creating fresh issues.

    Args:
        repo: GitHub repo in owner/repo format
        label: The label to filter by

    Returns:
        Number of issues closed
    """
    adapter = _adapter_for(repo)
    with gh_audit.context(reason=gh_audit.AuditReason.TEST_DATA_LIST, scope=gh_audit.AuditScope.TEST):
        issues = adapter.list_issues(labels=[label], state="open", limit=100)

    gh_audit.update_last_call(items_returned=len(issues))
    for issue in issues:
        close_issue(repo, issue.number, f"Cleaned up by test: {label}")
        _delete_claim_ref_if_present(repo, issue.number)

    return len(issues)


def cleanup_test_issues(repo: str) -> int:
    """Close all e2e test issues.

    Closes issues with 'test-data' label OR 'agent:e2e-test' label
    to ensure all test artifacts are cleaned up.

    Returns:
        Number of issues closed (includes issues where close succeeded even if comment failed)
    """
    adapter = _adapter_for(repo)
    count = 0
    errors = 0
    seen: set[int] = set()

    for label in ["test-data", "agent:e2e-test"]:
        try:
            with gh_audit.context(reason=gh_audit.AuditReason.TEST_DATA_LIST, scope=gh_audit.AuditScope.TEST):
                issues = adapter.list_issues(labels=[label], state="open", limit=100)
            gh_audit.update_last_call(items_returned=len(issues))
        except Exception as exc:
            logger.warning("Failed to list issues with label '%s': %s", label, exc)
            continue

        for issue in issues:
            num = issue.number
            if num not in seen:
                seen.add(num)
                try:
                    with gh_audit.context(reason=gh_audit.AuditReason.TEST_DATA_CLOSE, scope=gh_audit.AuditScope.TEST):
                        # Try to add comment, but don't fail if it times out
                        try:
                            adapter.add_comment(num, "Closed by test cleanup.")
                        except Exception as exc:
                            logger.warning("Failed to add comment to issue #%d: %s", num, exc)
                        # Always try to close even if comment failed
                        adapter.update_issue_state(num, "closed")
                        _delete_claim_ref_if_present(repo, num)
                    count += 1
                except Exception as exc:
                    logger.warning("Failed to close issue #%d: %s", num, exc)
                    errors += 1

    if errors:
        logger.warning("Cleanup completed with %d errors (closed %d issues)", errors, count)
    return count


def get_issue_labels(repo: str, issue_number: int) -> list[str]:
    """Get current labels for an issue.

    Args:
        repo: GitHub repo in owner/repo format
        issue_number: The issue number to get labels for

    Returns:
        List of label names currently on the issue

    Raises:
        RuntimeError: If issue not found
    """
    adapter = _adapter_for(repo)
    with gh_audit.context(reason=gh_audit.AuditReason.GH_READ, scope=gh_audit.AuditScope.TEST):
        issue = adapter.get_issue(issue_number)

    if issue is None:
        raise RuntimeError(f"Issue #{issue_number} not found")

    return list(issue.labels)


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
