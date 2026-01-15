"""GitHub auth checks for doctor."""

import os

from ..types import Check


def check_github_auth() -> list[Check]:
    from ....adapters.github.http_client import (
        _read_keyring_token,
        validate_github_token,
        KEYRING_SERVICE,
        KEYRING_USERNAME,
    )

    checks: list[Check] = []

    env_vars = ["ISSUE_ORCH_GITHUB_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"]
    token_sources = []
    for var in env_vars:
        value = os.environ.get(var)
        if value:
            masked = value[:4] + "..." + value[-4:] if len(value) > 12 else "***"
            token_sources.append(f"{var}: {masked}")

    keyring_token = _read_keyring_token()
    if keyring_token:
        masked = keyring_token[:4] + "..." + keyring_token[-4:] if len(keyring_token) > 12 else "***"
        token_sources.append(f"Keyring ({KEYRING_SERVICE}/{KEYRING_USERNAME}): {masked}")

    if token_sources:
        checks.append(Check(
            name="Token Sources",
            status="ok",
            detail=", ".join(token_sources),
        ))
    else:
        checks.append(Check(
            name="Token Sources",
            status="error",
            detail="No GitHub token found",
        ))

    token_result = validate_github_token()
    if token_result.valid:
        checks.append(Check(
            name="GitHub Auth",
            status="ok",
            detail=f"Authenticated as: {token_result.username}",
        ))
    else:
        checks.append(Check(
            name="GitHub Auth",
            status="error",
            detail=token_result.error or "Unknown error",
        ))

    return checks
