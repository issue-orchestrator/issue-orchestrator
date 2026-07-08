"""GitHub adapter package - implements GitHub API integration.

This package provides the concrete adapter implementations for GitHub:
- GitHubAdapter: Main adapter implementing RepositoryHost
- GitHubHttpClient: Low-level HTTP client with ETag caching
- GitHubCache: In-memory cache for API responses
- GitHubIssue: Issue data model
"""

from .github_adapter import GitHubAdapter
from .errors import GitHubAuthError, GitHubHttpError
from .auth import GitHubAuth, build_github_auth
from .http_client import (
    GitHubHttpClient,
    GitHubHttpConfig,
)
from .tokens import resolve_github_token
from .cache import GitHubCache
from .github_issue import GitHubIssue
from .issue_resolver import GitHubIssueResolver
from .repo import get_repo_from_git, GitRepoError

__all__ = [
    "GitHubAdapter",
    "GitHubAuth",
    "GitHubHttpClient",
    "GitHubHttpConfig",
    "GitHubHttpError",
    "GitHubAuthError",
    "GitHubCache",
    "GitHubIssue",
    "GitHubIssueResolver",
    "build_github_auth",
    "resolve_github_token",
    "get_repo_from_git",
    "GitRepoError",
]
