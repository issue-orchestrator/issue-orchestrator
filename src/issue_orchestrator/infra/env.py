"""Centralized environment variable access with project prefix.

All orchestrator-specific env vars should use this module to ensure
consistent prefixing. The prefix is defined once here.

Usage:
    from issue_orchestrator.infra.env import get_env, set_env

    # Reads ISSUE_ORCHESTRATOR_LOG_FILE
    log_file = get_env("LOG_FILE")

    # Sets ISSUE_ORCHESTRATOR_REPO_ROOT
    set_env("REPO_ROOT", "/path/to/repo")
"""

import os

ENV_PREFIX = "ISSUE_ORCHESTRATOR_"


def get_env(name: str, default: str | None = None) -> str | None:
    """Get an env var with the project prefix.

    Args:
        name: Variable name without prefix (e.g., "LOG_FILE")
        default: Default value if not set

    Returns:
        The value of ISSUE_ORCHESTRATOR_{name}, or default if not set.
    """
    return os.environ.get(f"{ENV_PREFIX}{name}", default)


def set_env(name: str, value: str) -> None:
    """Set an env var with the project prefix.

    Args:
        name: Variable name without prefix (e.g., "REPO_ROOT")
        value: Value to set
    """
    os.environ[f"{ENV_PREFIX}{name}"] = value


def get_env_bool(name: str, default: bool = False) -> bool:
    """Get an env var as a boolean.

    Truthy values: "1", "true", "yes" (case-insensitive)
    Falsy values: "0", "false", "no", "" or unset

    Args:
        name: Variable name without prefix
        default: Default if not set

    Returns:
        Boolean interpretation of the env var.
    """
    value = get_env(name)
    if value is None:
        return default
    return value.lower() in {"1", "true", "yes"}
