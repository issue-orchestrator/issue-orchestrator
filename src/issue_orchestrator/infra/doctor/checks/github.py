"""GitHub auth checks for doctor."""

from typing import TYPE_CHECKING

from ..types import Check

if TYPE_CHECKING:
    from ...config import Config


def check_github_auth(config: "Config | None" = None) -> list[Check]:
    from ....adapters.github.http_client import (
        KEYRING_SERVICE,
        KEYRING_USERNAME,
        describe_github_token_sources,
        validate_github_token,
    )

    checks: list[Check] = []
    token_sources = describe_github_token_sources(
        configured_env=getattr(config, "github_token_env", None) if config else None,
        configured_keyring_service=getattr(config, "github_keyring_service", None) if config else None,
        configured_keyring_username=getattr(config, "github_keyring_username", None) if config else None,
    )

    if token_sources:
        checks.append(Check(
            name="Token Sources",
            status="ok",
            detail=", ".join(token_sources),
        ))
    else:
        detail = "No GitHub token found"
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

    token_result = validate_github_token(
        configured_token=getattr(config, "github_token", None) if config else None,
        configured_env=getattr(config, "github_token_env", None) if config else None,
        configured_keyring_service=getattr(config, "github_keyring_service", None) if config else None,
        configured_keyring_username=getattr(config, "github_keyring_username", None) if config else None,
        repo=getattr(config, "repo", None) if config else None,
        api_url=getattr(config, "github_api_url", "https://api.github.com") if config else "https://api.github.com",
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
