"""E2E GitHub utility functions."""

import os
import shutil
import subprocess
from pathlib import Path

from issue_orchestrator.adapters.github import resolve_github_token
from tests.fixtures.live_agent_cli import (
    is_claude_authenticated,
    is_claude_available,
)

from .github_client import _github_adapter


def is_gh_authenticated() -> bool:
    """Check if GitHub auth is available."""
    try:
        resolve_github_token(configured_token=None)
        return True
    except Exception:
        return False


class GitHubRateLimitError(Exception):
    """Raised when GitHub API rate limit is exceeded."""
    pass


def check_github_rate_limit(repo: str) -> dict:
    """Check GitHub API rate limit status.

    Returns:
        Dict with 'remaining', 'limit', 'reset_at' keys

    Raises:
        GitHubRateLimitError: If rate limit is exceeded
    """
    try:
        data = _github_adapter(repo).get_rate_limit_snapshot()
        if data is None:
            return {"remaining": -1, "limit": -1, "reset_at": "unknown"}
        # Adapter already returns a dict with core/search/graphql keys
        core = data.get("core", {})
        remaining = core.get("remaining", 0)
        limit = core.get("limit", 5000)
        reset_timestamp = core.get("reset", 0)

        import datetime
        reset_at = datetime.datetime.fromtimestamp(reset_timestamp).strftime("%H:%M:%S") if reset_timestamp else "unknown"

        if remaining == 0:
            raise GitHubRateLimitError(
                f"GitHub API rate limit EXCEEDED!\n"
                f"  Limit: {limit}\n"
                f"  Remaining: {remaining}\n"
                f"  Resets at: {reset_at}\n"
                f"  \n"
                f"  Wait for rate limit to reset or use a different token."
            )

        return {"remaining": remaining, "limit": limit, "reset_at": reset_at}
    except GitHubRateLimitError:
        raise
    except Exception:
        return {"remaining": -1, "limit": -1, "reset_at": "unknown"}


def is_rate_limit_error(error_message: str) -> bool:
    """Check if an error message indicates a rate limit issue."""
    rate_limit_indicators = [
        "rate limit",
        "API rate limit",
        "rate_limit",
        "secondary rate limit",
        "abuse detection",
    ]
    error_lower = error_message.lower()
    return any(indicator.lower() in error_lower for indicator in rate_limit_indicators)


def is_github_connection_error(error_message: str) -> bool:
    """Check if an error message indicates a GitHub connectivity problem."""
    indicators = [
        "error connecting to api.github.com",
        "could not resolve host",
        "network is unreachable",
        "connection timed out",
        "connect: connection refused",
    ]
    error_lower = error_message.lower()
    return any(indicator in error_lower for indicator in indicators)


def is_github_reachable(repo: str) -> bool:
    """Check that GitHub API is reachable for live e2e tests."""
    try:
        snapshot = _github_adapter(repo).get_rate_limit_snapshot()
        return snapshot is not None
    except Exception:
        return False


def get_repo_from_git() -> str:
    """Get repo owner/name from git remote."""
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
        cwd=Path(__file__).parent.parent.parent.parent,
    )
    if result.returncode != 0:
        return "test/repo"

    url = result.stdout.strip()
    # Parse git@github.com:owner/repo.git or https://github.com/owner/repo.git
    if url.startswith("git@"):
        # git@github.com:owner/repo.git
        parts = url.split(":")[-1]
    else:
        # https://github.com/owner/repo.git
        parts = "/".join(url.split("/")[-2:])

    return parts.replace(".git", "")


def get_test_repo() -> str:
    """Get the repo to use for e2e tests.

    Order of precedence:
    1. E2E_TEST_REPO environment variable (e.g., "myuser/my-test-repo")
    2. Current repo from git remote (for local development)

    For open-source contributors:
    - Fork the repo or create your own test repo
    - Set E2E_TEST_REPO=youruser/yourrepo
    - Run e2e tests against your repo
    """
    return os.environ.get("E2E_TEST_REPO", get_repo_from_git())


def env_token_name() -> str | None:
    """Get the name of the environment variable containing the GitHub token."""
    if os.environ.get("GITHUB_TOKEN"):
        return "GITHUB_TOKEN"
    if os.environ.get("GH_TOKEN"):
        return "GH_TOKEN"
    return None
