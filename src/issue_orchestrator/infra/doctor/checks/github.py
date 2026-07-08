"""GitHub auth checks for doctor."""

from typing import TYPE_CHECKING

from ..types import Check

if TYPE_CHECKING:
    from ...config import Config


def check_github_auth(config: "Config | None" = None) -> list[Check]:
    from ....adapters.github.tokens import (
        KEYRING_SERVICE,
        KEYRING_USERNAME,
    )
    from ....adapters.github.errors import GitHubAuthError
    from ....adapters.github.auth import build_github_auth

    checks: list[Check] = []
    auth_kwargs = config.github_auth_kwargs() if config else {}
    try:
        auth = build_github_auth(
            **auth_kwargs,
            repo=getattr(config, "repo", None) if config else None,
            api_url=getattr(config, "github_api_url", "https://api.github.com") if config else "https://api.github.com",
            timeout_seconds=float(getattr(config, "github_http_timeout_seconds", 20.0)) if config else 20.0,
        )
        token_sources = auth.describe_sources()
    except GitHubAuthError as exc:
        auth = None
        token_sources = []
        auth_error = str(exc)
    else:
        auth_error = None

    if token_sources:
        checks.append(Check(
            name="Token Sources",
            status="ok",
            detail=", ".join(token_sources),
        ))
    else:
        detail = auth_error or "No GitHub auth source found"
        if config and any((
            getattr(config, "github_token_env", None),
            getattr(config, "github_keyring_service", None),
            getattr(config, "github_keyring_username", None),
        )):
            expected_sources: list[str] = []
            if config.github_token_env:
                expected_sources.append(f"env:{config.github_token_env}")
            if config.github_keyring_service or config.github_keyring_username:
                expected_sources.append(
                    "keyring:"
                    f"{config.github_keyring_service or KEYRING_SERVICE}"
                    f"/{config.github_keyring_username or KEYRING_USERNAME}"
                )
            detail = f"No GitHub token found in repo-configured sources ({', '.join(expected_sources)})"
        checks.append(Check(
            name="Token Sources",
            status="error",
            detail=detail,
        ))

    if auth is None:
        checks.append(Check(
            name="GitHub Auth",
            status="error",
            detail=auth_error or "Unknown GitHub auth error",
        ))
        return checks

    token_result = auth.validate(
        repo=getattr(config, "repo", None) if config else None,
        timeout_seconds=float(getattr(config, "github_http_timeout_seconds", 20.0)) if config else 20.0,
    )
    if token_result.valid:
        detail = f"Authenticated as: {token_result.username}"
        if config and getattr(config, "repo", None):
            detail += f" with access to {config.repo}"
        checks.append(Check(
            name="GitHub Auth",
            status="ok",
            detail=detail,
        ))
    else:
        checks.append(Check(
            name="GitHub Auth",
            status="error",
            detail=token_result.error or "Unknown error",
        ))

    return checks
