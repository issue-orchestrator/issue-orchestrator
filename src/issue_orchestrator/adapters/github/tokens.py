"""GitHub token resolution, validation, and keyring storage."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass

import httpx

from .errors import GitHubAuthError


# Keyring service/username constants for token storage
KEYRING_SERVICE = "issue-orchestrator"
KEYRING_USERNAME = "github-token"


def resolve_github_token(
    *,
    configured_token: str | None,
    configured_env: str | None = None,
    configured_keyring_service: str | None = None,
    configured_keyring_username: str | None = None,
) -> str:
    """Resolve GitHub token from multiple sources.

    Priority order (per ADR-0014):
    1. Explicitly configured token (from config file)
    2. Repo-specific auth sources declared in config:
       - Custom env var (repo.github.token_env)
       - Custom keyring entry (repo.github.keyring_service / keyring_username)
    3. ISSUE_ORCH_GITHUB_TOKEN env var (primary)
    4. GITHUB_TOKEN env var (fallback)
    5. GH_TOKEN env var (fallback)
    6. Default OS keychain entry created by issue-orchestrator auth store

    When a repo declares repo-specific auth sources, those sources are
    authoritative. Missing repo-scoped auth is treated as an error instead of
    silently falling back to a different token that may not have access to the
    configured repository.
    """
    if configured_token:
        return configured_token

    if any((configured_env, configured_keyring_service, configured_keyring_username)):
        return _resolve_repo_scoped_github_token(
            configured_env=configured_env,
            configured_keyring_service=configured_keyring_service,
            configured_keyring_username=configured_keyring_username,
        )
    # Primary env var per ADR-0014
    for env_name in ("ISSUE_ORCH_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        token = os.environ.get(env_name)
        if token:
            return token
    # Optional keychain via keyring library
    token = _read_keyring_token(service=KEYRING_SERVICE, username=KEYRING_USERNAME)
    if token:
        return token
    raise GitHubAuthError(
        "GitHub token not configured. Set ISSUE_ORCH_GITHUB_TOKEN or run: "
        "issue-orchestrator auth store"
    )


@dataclass
class TokenValidationResult:
    """Result of validating a GitHub token."""

    valid: bool
    username: str | None = None
    error: str | None = None


def _mask_token(token: str) -> str:
    """Mask a token for logs and diagnostics."""
    return token[:4] + "..." + token[-4:] if len(token) > 12 else "***"


def describe_github_token_sources(
    *,
    configured_env: str | None = None,
    configured_keyring_service: str | None = None,
    configured_keyring_username: str | None = None,
) -> list[str]:
    """Describe visible token sources for diagnostics.

    Repo-scoped auth sources are authoritative. When a repo declares a custom
    env var or keyring entry, diagnostics only report those sources and do not
    surface unrelated generic fallback tokens.
    """
    repo_scoped_auth = any(
        (configured_env, configured_keyring_service, configured_keyring_username)
    )
    token_sources: list[str] = []
    if configured_env:
        value = os.environ.get(configured_env)
        if value:
            token_sources.append(f"{configured_env}: {_mask_token(value)}")
    if configured_keyring_service or configured_keyring_username:
        keyring_service = configured_keyring_service or KEYRING_SERVICE
        keyring_username = configured_keyring_username or KEYRING_USERNAME
        value = _read_keyring_token(service=keyring_service, username=keyring_username)
        if value:
            token_sources.append(
                f"Keyring ({keyring_service}/{keyring_username}): {_mask_token(value)}"
            )
    if repo_scoped_auth:
        return token_sources
    for env_name in ("ISSUE_ORCH_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(env_name)
        if value:
            token_sources.append(f"{env_name}: {_mask_token(value)}")
    value = _read_keyring_token(service=KEYRING_SERVICE, username=KEYRING_USERNAME)
    if value:
        token_sources.append(
            f"Keyring ({KEYRING_SERVICE}/{KEYRING_USERNAME}): {_mask_token(value)}"
        )
    return token_sources


def _resolve_repo_scoped_github_token(
    *,
    configured_env: str | None,
    configured_keyring_service: str | None,
    configured_keyring_username: str | None,
) -> str:
    """Resolve a token from repo-configured auth sources only."""
    if configured_env:
        token = os.environ.get(configured_env)
        if token:
            return token
    if configured_keyring_service or configured_keyring_username:
        keyring_service = configured_keyring_service or KEYRING_SERVICE
        keyring_username = configured_keyring_username or KEYRING_USERNAME
        token = _read_keyring_token(service=keyring_service, username=keyring_username)
        if token:
            return token
    expected_sources: list[str] = []
    if configured_env:
        expected_sources.append(f"env:{configured_env}")
    if configured_keyring_service or configured_keyring_username:
        keyring_service = configured_keyring_service or KEYRING_SERVICE
        keyring_username = configured_keyring_username or KEYRING_USERNAME
        expected_sources.append(f"keyring:{keyring_service}/{keyring_username}")
    raise GitHubAuthError(
        "GitHub token not configured for repo-specific auth. "
        f"Checked {', '.join(expected_sources)}."
    )


def validate_github_token(
    token: str | None = None,
    *,
    configured_token: str | None = None,
    configured_env: str | None = None,
    configured_keyring_service: str | None = None,
    configured_keyring_username: str | None = None,
    repo: str | None = None,
    api_url: str = "https://api.github.com",
) -> TokenValidationResult:
    """Validate a GitHub token by calling the API.

    Args:
        token: Token to validate. If None, will resolve using standard sources.
        configured_token: Explicit repo-configured token.
        configured_env: Repo-configured token env var.
        configured_keyring_service: Repo-configured keyring service.
        configured_keyring_username: Repo-configured keyring username/account.
        repo: Optional owner/repo to verify access against.
        api_url: GitHub API base URL.

    Returns:
        TokenValidationResult with valid status, username, or error message.
    """
    try:
        if token is None:
            token = resolve_github_token(
                configured_token=configured_token,
                configured_env=configured_env,
                configured_keyring_service=configured_keyring_service,
                configured_keyring_username=configured_keyring_username,
            )
    except GitHubAuthError as e:
        return TokenValidationResult(valid=False, error=str(e))

    base_url = api_url.rstrip("/")
    try:
        resp = httpx.get(
            f"{base_url}/user",
            headers={"Authorization": f"token {token}"},
            timeout=10.0,
        )
        if resp.status_code == 200:
            user_info = resp.json()
            username = user_info.get("login")
            if repo:
                repo_resp = httpx.get(
                    f"{base_url}/repos/{repo}",
                    headers={"Authorization": f"token {token}"},
                    timeout=10.0,
                )
                if repo_resp.status_code != 200:
                    return TokenValidationResult(
                        valid=False,
                        username=username,
                        error=(
                            f"Token cannot access repo {repo} "
                            f"(HTTP {repo_resp.status_code})"
                        ),
                    )
            return TokenValidationResult(
                valid=True,
                username=username,
            )
        else:
            return TokenValidationResult(
                valid=False,
                error=f"Token invalid (HTTP {resp.status_code})",
            )
    except Exception as e:
        return TokenValidationResult(valid=False, error=str(e))


def _read_keyring_token(
    *,
    service: str = KEYRING_SERVICE,
    username: str = KEYRING_USERNAME,
) -> str | None:
    """Read GitHub token from OS keychain via keyring library.

    Uses the cross-platform keyring library which supports:
    - macOS Keychain
    - Windows Credential Locker
    - Linux Secret Service (GNOME Keyring, KWallet)

    Returns None if keyring is not available or no token is stored.
    """
    token: str | None = None
    try:
        import keyring
    except ImportError:
        keyring = None
    if keyring is not None:
        try:
            token = keyring.get_password(service, username)
            if token:
                return token
        except Exception:
            # Keyring can fail for various reasons (no backend, locked, etc.).
            # Fall through to the macOS security CLI if available.
            pass
    return _read_macos_security_keychain_token(service=service, username=username)


def _read_macos_security_keychain_token(*, service: str, username: str) -> str | None:
    """Read a generic password directly from the macOS login keychain."""
    if sys.platform != "darwin":
        return None
    if not shutil.which("security"):
        return None
    try:
        result = subprocess.run(
            [
                "security",
                "find-generic-password",
                "-s",
                service,
                "-a",
                username,
                "-w",
            ],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    token = result.stdout.strip()
    return token or None


def store_keyring_token(token: str) -> None:
    """Store GitHub token in OS keychain via keyring library.

    Args:
        token: The GitHub token to store

    Raises:
        ImportError: If keyring library is not installed
        Exception: If keyring storage fails
    """
    import keyring

    keyring.set_password(KEYRING_SERVICE, KEYRING_USERNAME, token)


def clear_keyring_token() -> bool:
    """Remove GitHub token from OS keychain.

    Returns:
        True if token was deleted, False if no token was stored
    """
    try:
        import keyring
    except ImportError:
        return False
    try:
        keyring.delete_password(KEYRING_SERVICE, KEYRING_USERNAME)
        return True
    except keyring.errors.PasswordDeleteError:  # type: ignore[attr-defined]
        return False
    except Exception:
        return False


__all__ = [
    "KEYRING_SERVICE",
    "KEYRING_USERNAME",
    "TokenValidationResult",
    "_mask_token",
    "_read_keyring_token",
    "_read_macos_security_keychain_token",
    "_resolve_repo_scoped_github_token",
    "clear_keyring_token",
    "describe_github_token_sources",
    "resolve_github_token",
    "store_keyring_token",
    "validate_github_token",
]
