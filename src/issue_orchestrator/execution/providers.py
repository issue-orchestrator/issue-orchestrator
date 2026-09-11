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
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from ..adapters.github.tokens import TokenValidationResult
    from ..domain.repository_setup_auth import RepositorySetupGitHubAuthorization
    from ..infra.config import Config
    from ..ports import RepositoryHost
    from ..ports.promotion_target import PromotionTargetHost
    from ..ports.pattern_registry import PatternCaseFileRegistry
    from ..ports.repository_setup import RepositorySetupGitHubVerification
    from ..ports.fresh_issue_reader import FreshIssueReader


# =============================================================================
# GitHub Providers
# =============================================================================


def create_repository_host(
    repo: str, config: "Config | None" = None
) -> "RepositoryHost":
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


def create_fresh_issue_reader(repo: str, config: "Config") -> "FreshIssueReader":
    """Create the uncached issue-label reader used by mutation gates."""
    from ..adapters.github.fresh_issue_reader import GitHubFreshIssueReader

    return GitHubFreshIssueReader(repo=repo, config=config)


def create_shared_pattern_registry(
    repository_host: "RepositoryHost | None",
    *,
    claimant_id: str,
    lease_seconds: int,
) -> "PatternCaseFileRegistry | None":
    """Build GitHub-backed pattern authority when the host supports it.

    Adapter capability detection and access to the underlying GitHub client
    stay on the execution side of the composition boundary. Offline and fake
    hosts return ``None`` so the caller can select its explicit local mode.
    """
    from ..adapters.github.github_adapter import GitHubAdapter
    from ..adapters.github.pattern_registry import GitHubRefPatternRegistry

    if not isinstance(repository_host, GitHubAdapter):
        return None
    return GitHubRefPatternRegistry(
        repository_host.http_client,
        claimant_id=claimant_id,
        lease_seconds=lease_seconds,
    )


def create_promotion_target_host(
    repository_host: "RepositoryHost | None",
    config: "Config | None" = None,
) -> "PromotionTargetHost | None":
    """Adapt a repository host to the finding-promotion target port (#6957).

    The provider factory owns the adapter construction so composition-root
    helpers and the doctor check depend on this seam rather than importing the
    GitHub adapter package themselves. Explicit per-target credentials are
    resolved here once and shared by doctor and runtime filing. Returns None
    when the readiness owner says the lane cannot run or the host is not a real
    GitHub adapter (offline/testing). Active actions still fail loudly if their
    target is unexpectedly unwired.
    """
    from ..adapters.github import build_github_auth
    from ..adapters.github.http_client import GitHubHttpConfig
    from ..adapters.github.promotion_target import (
        build_promotion_target_host,
        supports_promotion_target_host,
    )

    if repository_host is None:
        return None
    if config is not None:
        from ..infra.tech_lead_promotion_activation import promotion_lane_readiness

        # The readiness owner defines when this lane has dependencies at all.
        # Inactive/unready lanes must not resolve target credentials during
        # bootstrap; switching promotion off is an operator escape hatch from
        # unavailable credentials, just as it is for doctor and tick reads.
        if not promotion_lane_readiness(config).ready:
            return None
    # Adapter support is an adapter-owned fact. Establish it before resolving
    # credentials so active offline/fake hosts retain their unwired semantics
    # even when an unused explicit target token is unavailable.
    if not supports_promotion_target_host(repository_host):
        return None
    target_connections = {}
    if config is not None:
        for repo in config.tech_lead.findings.target_repos():
            auth_config = config.tech_lead.findings.auth_for(repo)
            if auth_config is None:
                continue
            auth = build_github_auth(
                **auth_config.auth_kwargs(),
                repo=repo,
                api_url=auth_config.api_url,
                timeout_seconds=auth_config.http_timeout_seconds,
            )
            target_connections[repo] = GitHubHttpConfig(
                repo=repo,
                base_url=auth_config.api_url,
                timeout_seconds=auth_config.http_timeout_seconds,
                auth=auth,
            )
    return build_promotion_target_host(
        repository_host, target_connections=target_connections
    )


def create_repository_setup_host(
    repo_name: str,
    authorization: "RepositorySetupGitHubAuthorization",
) -> "RepositoryHost":
    """Create the setup host with the exact authorization that was verified."""
    from ..adapters.github import GitHubAdapter, build_github_auth
    from .repository_setup_github_authorization import (
        repository_setup_github_authorization_codec,
    )

    auth = build_github_auth(
        **repository_setup_github_authorization_codec.adapter_kwargs(authorization),
        repo=repo_name,
        api_url=authorization.api_url,
        timeout_seconds=authorization.http_timeout_seconds,
    )
    return GitHubAdapter(
        repo=repo_name,
        auth=auth,
        api_url=authorization.api_url,
        http_timeout_seconds=authorization.http_timeout_seconds,
    )


def verify_repository_setup_github_authorization(
    repo_name: str,
    authorization: "RepositorySetupGitHubAuthorization",
) -> "RepositorySetupGitHubVerification":
    """Verify repo access and return only non-secret identity/source facts."""
    from ..adapters.github import build_github_auth
    from ..adapters.github.errors import GitHubAuthError
    from ..domain.repository_setup_auth import RepositorySetupGitHubAuthorization
    from ..ports.repository_setup import RepositorySetupGitHubVerification
    from .repository_setup_github_authorization import (
        repository_setup_github_authorization_codec,
    )

    auth = build_github_auth(
        **repository_setup_github_authorization_codec.adapter_kwargs(authorization),
        repo=repo_name,
        api_url=authorization.api_url,
        timeout_seconds=authorization.http_timeout_seconds,
    )
    result = auth.validate(
        repo=repo_name,
        timeout_seconds=authorization.http_timeout_seconds,
    )
    if not result.valid or not result.username:
        raise GitHubAuthError(
            result.error or f"GitHub authorization failed for {repo_name}"
        )
    source = auth.resolved_source
    if source is None:
        raise GitHubAuthError(
            "GitHub authorization did not report its credential source"
        )

    normalized = authorization
    if authorization.kind == "detected":
        if source.kind == "environment" and source.environment_variable:
            normalized = RepositorySetupGitHubAuthorization(
                kind="personal",
                token_env=source.environment_variable,
                api_url=authorization.api_url,
                http_timeout_seconds=authorization.http_timeout_seconds,
            )
        elif (
            source.kind == "keyring"
            and source.keyring_service
            and source.keyring_username
        ):
            normalized = RepositorySetupGitHubAuthorization(
                kind="personal",
                keyring_service=source.keyring_service,
                keyring_username=source.keyring_username,
                api_url=authorization.api_url,
                http_timeout_seconds=authorization.http_timeout_seconds,
            )

    return RepositorySetupGitHubVerification(
        identity=result.username,
        repository=repo_name,
        auth_kind="github_app" if auth.auth_kind == "github_app" else "personal",
        source=source.description,
        normalized_authorization=normalized,
    )


def store_repository_setup_github_token(
    authorization: "RepositorySetupGitHubAuthorization",
    *,
    repo: str,
) -> "RepositorySetupGitHubAuthorization":
    """Store one personal token while preserving its verified transport metadata."""
    from dataclasses import replace

    from ..adapters.github.tokens import KEYRING_SERVICE, store_keyring_token_for

    if authorization.kind != "personal" or authorization.token is None:
        raise ValueError("Personal token storage requires inline personal authorization")
    username = (
        f"github-token:{_canonical_github_credential_host(authorization.api_url)}:{repo}"
    )
    store_keyring_token_for(
        authorization.token,
        service=KEYRING_SERVICE,
        username=username,
    )
    return replace(
        authorization,
        token=None,
        keyring_service=KEYRING_SERVICE,
        keyring_username=username,
    )


def _canonical_github_credential_host(api_url: str) -> str:
    """Return one stable host identity for repo-scoped credential storage."""
    parsed = urlsplit(api_url)
    if parsed.scheme not in {"http", "https"} or parsed.hostname is None:
        raise ValueError("GitHub API URL must be an absolute HTTP(S) URL")
    host = parsed.hostname.rstrip(".").lower()
    port = parsed.port
    default_port = (parsed.scheme == "http" and port == 80) or (
        parsed.scheme == "https" and port == 443
    )
    return f"{host}:{port}" if port is not None and not default_port else host


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
