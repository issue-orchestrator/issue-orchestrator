"""GitHub token resolution, validation, and keyring storage."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from base64 import b64decode
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import yaml

from .errors import GitHubAuthError

logger = logging.getLogger(__name__)


# Keyring service/username constants for token storage
KEYRING_SERVICE = "issue-orchestrator"
KEYRING_USERNAME = "github-token"
_GO_KEYRING_B64_PREFIX = "go-keyring-base64:"


def resolve_github_token(
    *,
    configured_token: str | None = None,
    configured_env: str | None = None,
    configured_keyring_service: str | None = None,
    configured_keyring_username: str | None = None,
    api_url: str = "https://api.github.com",
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
    6. GitHub CLI hosts.yml auth
    7. Default OS keychain entry created by issue-orchestrator auth store

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
    token = _read_gh_cli_token(host=_github_host_for_api_url(api_url))
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
    api_url: str = "https://api.github.com",
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
    gh_cli_token = _read_gh_cli_token(host=_github_host_for_api_url(api_url))
    if gh_cli_token:
        token_sources.append(
            f"GitHub CLI auth ({_github_host_for_api_url(api_url)}): {_mask_token(gh_cli_token)}"
        )
    value = _read_keyring_token(service=KEYRING_SERVICE, username=KEYRING_USERNAME)
    if value:
        token_sources.append(
            f"Keyring ({KEYRING_SERVICE}/{KEYRING_USERNAME}): {_mask_token(value)}"
        )
    return token_sources


def _github_host_for_api_url(api_url: str) -> str:
    """Map an API base URL to the corresponding GitHub host entry in hosts.yml."""
    hostname = urlparse(api_url).hostname or "api.github.com"
    if hostname == "api.github.com":
        return "github.com"
    return hostname


def _gh_hosts_paths() -> list[Path]:
    """Return candidate GitHub CLI hosts.yml locations in lookup order."""
    candidates: list[Path] = []
    seen: set[Path] = set()

    def _append(path: Path | None) -> None:
        if path is None:
            return
        normalized = path.expanduser()
        if normalized in seen:
            return
        seen.add(normalized)
        candidates.append(normalized)

    gh_config_dir = os.environ.get("GH_CONFIG_DIR")
    if gh_config_dir:
        _append(Path(gh_config_dir) / "hosts.yml")
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    if xdg_config_home:
        _append(Path(xdg_config_home) / "gh" / "hosts.yml")
    _append(Path.home() / ".config" / "gh" / "hosts.yml")
    appdata = os.environ.get("APPDATA")
    if appdata:
        _append(Path(appdata) / "GitHub CLI" / "hosts.yml")
    _append(Path("/etc/gh/hosts.yml"))
    return candidates


def _read_gh_hosts_record(*, host: str) -> dict[str, object] | None:
    """Read a single host record from GitHub CLI's hosts.yml if available."""
    for hosts_path in _gh_hosts_paths():
        try:
            if not hosts_path.exists():
                continue
            payload = yaml.safe_load(hosts_path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError):
            continue
        if not isinstance(payload, dict):
            continue
        host_data = payload.get(host)
        if not isinstance(host_data, dict):
            continue
        return host_data
    return None


def _gh_cli_account_for_host(record: dict[str, object]) -> str | None:
    """Return the GitHub CLI account name for a host record."""
    user = record.get("user")
    if isinstance(user, str) and user.strip():
        return user.strip()
    users = record.get("users")
    if isinstance(users, dict):
        for account_name in users:
            if isinstance(account_name, str) and account_name.strip():
                return account_name.strip()
    return None


def _read_gh_cli_token(*, host: str) -> str | None:
    """Read GitHub CLI auth from hosts.yml or its paired keychain entry."""
    record = _read_gh_hosts_record(host=host)
    if record is None:
        return None
    token = record.get("oauth_token")
    if isinstance(token, str) and token.strip():
        return token.strip()
    account = _gh_cli_account_for_host(record)
    if account:
        return _read_keyring_token(service=f"gh:{host}", username=account)
    return None


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
                return _normalize_keyring_secret(token)
        except Exception as exc:  # noqa: BLE001
            # Keyring can fail for a variety of legitimate reasons — no
            # backend on headless Linux, a locked keyring, a remote
            # daemon that timed out. We still have to fall through to
            # the macOS ``security`` CLI fallback for those cases, so
            # catch broadly here but log at DEBUG so a misconfigured
            # keyring is not totally invisible when someone is
            # troubleshooting auth.
            logger.debug(
                "keyring.get_password failed for service=%s user=%s: %s",
                service,
                username,
                exc,
            )
    return _read_macos_security_keychain_token(service=service, username=username)


def _read_macos_security_keychain_token(*, service: str, username: str) -> str | None:
    """Read a generic password directly from the macOS login keychain.

    SECURITY NOTE: this is a command-injection boundary. ``service`` and
    ``username`` flow into the argv of the ``security`` binary, which is
    safe because we pass a fixed ``list`` to ``subprocess.run`` (no
    ``shell=True``, no string interpolation). Do NOT switch this call
    to ``shell=True`` or build the command with an f-string — that
    would let an attacker-controlled service name inject arbitrary
    shell metacharacters.
    """
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
    except (OSError, subprocess.SubprocessError) as exc:
        # Keep this catch narrow — an unknown exception here may be a
        # signal of tampering or a broken environment and should not be
        # silently swallowed without a trace. DEBUG so it only surfaces
        # under active troubleshooting.
        logger.debug(
            "macOS security find-generic-password failed for service=%s user=%s: %s",
            service,
            username,
            exc,
        )
        return None
    if result.returncode != 0:
        return None
    token = result.stdout.strip()
    normalized = _normalize_keyring_secret(token)
    return normalized or None


def _normalize_keyring_secret(secret: str) -> str:
    """Normalize secrets read from OS keychains.

    GitHub CLI stores secure-storage tokens through go-keyring using the
    ``go-keyring-base64:<base64-token>`` envelope. issue-orchestrator's own
    keyring entries are stored as raw tokens, so only unwrap when the prefix is
    present.
    """
    if not secret.startswith(_GO_KEYRING_B64_PREFIX):
        return secret
    encoded = secret.removeprefix(_GO_KEYRING_B64_PREFIX)
    try:
        decoded = b64decode(encoded.encode("ascii"), validate=True).decode("utf-8")
    except Exception:
        return secret
    return decoded or secret


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
    "_gh_cli_account_for_host",
    "_gh_hosts_paths",
    "_github_host_for_api_url",
    "_normalize_keyring_secret",
    "_read_gh_cli_token",
    "_read_gh_hosts_record",
    "_read_keyring_token",
    "_read_macos_security_keychain_token",
    "_resolve_repo_scoped_github_token",
    "clear_keyring_token",
    "describe_github_token_sources",
    "resolve_github_token",
    "store_keyring_token",
]
