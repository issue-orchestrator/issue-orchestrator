"""Provider functions for standalone entrypoints.

This module provides factory functions that return protocol implementations
without requiring a full orchestrator context. It's the composition root
for standalone CLI commands, wizards, and the control API.

Entrypoints should import from this module instead of importing adapters directly.
This keeps the layer boundary clean: entrypoints -> providers -> adapters.

Example:
    # Instead of:
    from ..adapters.github import GitHubAdapter
    adapter = GitHubAdapter(repo=repo)

    # Use:
    from ..execution.providers import create_repository_host
    host = create_repository_host(repo=repo)
"""

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..adapters.github.tokens import TokenValidationResult
    from ..infra.config import Config
    from ..ports import RepositoryHost


# =============================================================================
# GitHub Providers
# =============================================================================


def create_repository_host(repo: str, config: "Config | None" = None) -> "RepositoryHost":
    """Create a RepositoryHost for the given repository.

    Args:
        repo: Repository in 'owner/repo' format
        config: Optional loaded config so repo-specific auth and API settings
            flow into the GitHub adapter.

    Returns:
        A RepositoryHost implementation (GitHubAdapter)
    """
    from ..adapters.github import GitHubAdapter

    return GitHubAdapter(repo=repo, config=config)


def resolve_github_token(
    configured_token: str | None = None,
    configured_env: str | None = None,
    configured_keyring_service: str | None = None,
    configured_keyring_username: str | None = None,
    configured_app_client_id: str | None = None,
    configured_app_id: str | None = None,
    configured_app_installation_id: str | None = None,
    configured_app_private_key_path: str | None = None,
    configured_app_private_key_env: str | None = None,
    api_url: str = "https://api.github.com",
) -> str:
    """Resolve GitHub token from various sources.

    Checks in order:
    1. Explicitly configured token
    2. Environment variable (configured or GITHUB_TOKEN)
    3. GitHub CLI hosts.yml auth
    4. Keyring storage

    Args:
        configured_token: Explicitly provided token
        configured_env: Environment variable name to check
        configured_keyring_service: Keyring service name to check
        configured_keyring_username: Keyring username/account to check
        api_url: GitHub API base URL used to derive the matching hosts.yml entry

    Returns:
        GitHub token string

    Raises:
        ValueError: If no token found
    """
    from ..adapters.github.tokens import resolve_github_token as _resolve

    return _resolve(
        configured_token=configured_token,
        configured_env=configured_env,
        configured_keyring_service=configured_keyring_service,
        configured_keyring_username=configured_keyring_username,
        configured_app_client_id=configured_app_client_id,
        configured_app_id=configured_app_id,
        configured_app_installation_id=configured_app_installation_id,
        configured_app_private_key_path=configured_app_private_key_path,
        configured_app_private_key_env=configured_app_private_key_env,
        api_url=api_url,
    )


def validate_github_token(
    *,
    configured_token: str | None = None,
    configured_env: str | None = None,
    configured_keyring_service: str | None = None,
    configured_keyring_username: str | None = None,
    configured_app_client_id: str | None = None,
    configured_app_id: str | None = None,
    configured_app_installation_id: str | None = None,
    configured_app_private_key_path: str | None = None,
    configured_app_private_key_env: str | None = None,
    repo: str | None = None,
    api_url: str = "https://api.github.com",
) -> "TokenValidationResult":
    """Validate GitHub auth for standalone entrypoints.

    Args:
        configured_token: Explicitly provided token
        configured_env: Environment variable name to check
        configured_keyring_service: Keyring service name to check
        configured_keyring_username: Keyring username/account to check
        repo: Optional repository to validate access against
        api_url: GitHub API base URL

    Returns:
        Token validation result from the GitHub adapter layer
    """
    from ..adapters.github.http_client import validate_github_token as _validate

    return _validate(
        configured_token=configured_token,
        configured_env=configured_env,
        configured_keyring_service=configured_keyring_service,
        configured_keyring_username=configured_keyring_username,
        configured_app_client_id=configured_app_client_id,
        configured_app_id=configured_app_id,
        configured_app_installation_id=configured_app_installation_id,
        configured_app_private_key_path=configured_app_private_key_path,
        configured_app_private_key_env=configured_app_private_key_env,
        repo=repo,
        api_url=api_url,
    )


def store_keyring_token(token: str) -> None:
    """Store GitHub token in system keyring.

    Args:
        token: GitHub token to store
    """
    from ..adapters.github.tokens import store_keyring_token as _store

    _store(token)


def clear_keyring_token() -> None:
    """Clear GitHub token from system keyring."""
    from ..adapters.github.tokens import clear_keyring_token as _clear

    _clear()


def get_repo_from_git() -> str | None:
    """Detect GitHub repository from git remote.

    Returns:
        Repository in 'owner/repo' format, or None if not detected
    """
    from ..adapters.github.repo import get_repo_from_git as _get_repo
    from ..adapters.github.repo import GitRepoError

    try:
        return _get_repo()
    except GitRepoError:
        return None


# =============================================================================
# Worktree Providers
# =============================================================================


def get_hooks_dir() -> Path:
    """Get the hooks directory path.

    Returns:
        Path to the hooks directory
    """
    from ..adapters.worktree._worktree import HOOKS_DIR

    return HOOKS_DIR
