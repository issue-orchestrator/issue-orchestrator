"""Issue data factory helpers for e2e tests.

Provides functions to create/update/close issues during test execution.
"""

import logging

from issue_orchestrator.domain.issue_key import IssueKey, GitHubIssueKey, parse_external_id
from issue_orchestrator.testing.support.test_data import (
    create_issue,
    update_issue,
    close_issue,
)
from .inflight_tracker import trigger_refresh

logger = logging.getLogger(__name__)


def inflight_create(
    repo: str,
    title: str,
    labels: list[str],
    body: str = "Created mid-test.",
) -> tuple[IssueKey, int]:
    """Create an issue while orchestrator is running.

    Note: Caller should call trigger_refresh() after creating all issues
    to notify the orchestrator to pick them up.

    Args:
        repo: GitHub repo in owner/repo format
        title: Issue title (should include [EXTERNAL-ID] prefix)
        labels: Labels to apply
        body: Issue body

    Returns:
        Tuple of (IssueKey, issue_number) for the created issue

    Raises:
        ValueError: If title doesn't contain an external ID prefix like [M1-011]
    """
    # Extract external ID from title prefix - all test issues MUST have one
    parsed = parse_external_id(title)
    if not parsed.external_id:
        raise ValueError(
            f"Title must contain external ID prefix like [M1-011]: {title!r}"
        )

    issue_number = create_issue(repo, title, labels, body)
    logger.info("Created issue #%d with external_id=%s", issue_number, parsed.external_id)
    return GitHubIssueKey(repo=repo, external_id=parsed.external_id), issue_number


def inflight_update(
    issue: IssueKey,
    add_labels: list[str] | None = None,
    remove_labels: list[str] | None = None,
    port: int | None = None,
) -> None:
    """Update an issue while orchestrator is running.

    Args:
        issue: The issue to update
        add_labels: Labels to add
        remove_labels: Labels to remove
        port: Control API port for refresh (defaults to Config.control_api_port)
    """
    issue_number = int(issue.stable_id())
    update_issue(issue.scope(), issue_number, add_labels, remove_labels)
    trigger_refresh(port)


def inflight_close(
    issue: IssueKey,
    comment: str | None = None,
    port: int | None = None,
) -> None:
    """Close an issue while orchestrator is running.

    Args:
        issue: The issue to close
        comment: Optional comment when closing
        port: Control API port for refresh (defaults to Config.control_api_port)
    """
    issue_number = int(issue.stable_id())
    close_issue(issue.scope(), issue_number, comment)
    trigger_refresh(port)
