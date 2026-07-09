"""Agent-secret environment variable ownership."""

from __future__ import annotations

import os
import re
from collections.abc import Iterable

EXTRA_FORBIDDEN_ENV_VARS_ENV = "ISSUE_ORCHESTRATOR_EXTRA_FORBIDDEN_ENV_VARS"
GITHUB_APP_PRIVATE_KEY_ENV = "ISSUE_ORCH_GITHUB_APP_PRIVATE_KEY"

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def configure_extra_forbidden_env_vars(names: Iterable[str | None]) -> None:
    """Publish configured secret env names for agent env scrubbers."""
    values = normalize_env_var_names(names)
    if values:
        os.environ[EXTRA_FORBIDDEN_ENV_VARS_ENV] = ",".join(values)
    else:
        os.environ.pop(EXTRA_FORBIDDEN_ENV_VARS_ENV, None)


def github_app_private_key_env_vars(configured_private_key_env: str | None) -> list[str]:
    """Return GitHub App private-key env names that agents must not inherit."""
    return normalize_env_var_names((GITHUB_APP_PRIVATE_KEY_ENV, configured_private_key_env))


def forbidden_agent_env_vars(base: Iterable[str]) -> list[str]:
    """Combine a base scrub list with configured runtime secret env names."""
    return normalize_env_var_names(
        [
            *base,
            *github_app_private_key_env_vars(None),
            *configured_extra_forbidden_env_vars(),
        ]
    )


def configured_extra_forbidden_env_vars() -> list[str]:
    """Read configured runtime secret env names from the orchestrator process."""
    raw = os.environ.get(EXTRA_FORBIDDEN_ENV_VARS_ENV)
    if not raw:
        return []
    return normalize_env_var_names(raw.split(","))


def normalize_env_var_names(names: Iterable[str | None]) -> list[str]:
    """Normalize and validate shell-safe environment variable names."""
    normalized: list[str] = []
    seen: set[str] = set()
    for name in names:
        if name is None:
            continue
        value = name.strip()
        if not value:
            continue
        if not _ENV_NAME_RE.fullmatch(value):
            raise ValueError(f"Invalid environment variable name: {value!r}")
        if value not in seen:
            seen.add(value)
            normalized.append(value)
    return normalized
