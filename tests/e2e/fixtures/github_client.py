"""GitHub client helpers for e2e tests.

Provides a cached GitHubAdapter factory and common GitHub operations.
"""

import asyncio
import logging

from issue_orchestrator.adapters.github import GitHubAdapter

logger = logging.getLogger(__name__)

_github_adapter_cache: dict[str, GitHubAdapter] = {}

# Exponential backoff sequence: 1, 2, 4, 8, 16, 32, 64 seconds (total: 127s max)
POLL_BACKOFF_SECONDS = (1, 2, 4, 8, 16, 32, 64)


def _github_adapter(repo: str) -> GitHubAdapter:
    """Get or create a GitHubAdapter for the given repo.

    Uses a cache to reuse adapters across calls within the same process.
    All GitHub access in e2e tests is routed through the adapter for
    consistent auditing and rate-limit handling.
    """
    adapter = _github_adapter_cache.get(repo)
    if adapter is None:
        adapter = GitHubAdapter(repo=repo)
        _github_adapter_cache[repo] = adapter
    return adapter


def get_issue_comments(repo: str, issue_number: int) -> list[dict]:
    """Get comments on an issue."""
    return _github_adapter(repo).get_issue_comments(issue_number)


def get_issue_labels(repo: str, issue_number: int) -> list[str]:
    """Get labels on an issue (single GitHub API call)."""
    return _github_adapter(repo).get_issue_labels(issue_number)


async def poll_issue_label(
    repo: str,
    issue_number: int,
    label: str,
    backoff: tuple[int, ...] = POLL_BACKOFF_SECONDS,
) -> None:
    """Poll GitHub directly until issue has the expected label.

    Uses exponential backoff: 1, 2, 4, 8, 16, 32, 64 seconds.
    Total max wait: 127 seconds.

    This is more efficient than triggering a full orchestrator refresh
    when you only need to verify one issue's state.

    Args:
        repo: Repository in "owner/repo" format
        issue_number: The issue number to check
        label: The label that must be present
        backoff: Tuple of sleep durations between attempts

    Raises:
        TimeoutError: If label not found after all attempts
    """
    adapter = _github_adapter(repo)
    last_labels: list[str] = []

    for i, sleep_seconds in enumerate(backoff):
        # Invalidate cache to force fresh read
        adapter.invalidate_label_cache(issue_number)
        last_labels = adapter.get_issue_labels(issue_number)

        if label in last_labels:
            logger.info(
                "poll_issue_label: Found '%s' on issue #%d after %d attempt(s)",
                label, issue_number, i + 1
            )
            return

        logger.debug(
            "poll_issue_label: Attempt %d - issue #%d has labels %s, waiting %ds",
            i + 1, issue_number, last_labels, sleep_seconds
        )
        await asyncio.sleep(sleep_seconds)

    raise TimeoutError(
        f"Label '{label}' not found on issue #{issue_number} after {len(backoff)} attempts. "
        f"Last seen labels: {last_labels}"
    )
