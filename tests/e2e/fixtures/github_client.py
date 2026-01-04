"""GitHub client helpers for e2e tests.

Provides a cached GitHubAdapter factory and common GitHub operations.
"""

from issue_orchestrator.adapters.github import GitHubAdapter


_github_adapter_cache: dict[str, GitHubAdapter] = {}


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
