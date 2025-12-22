"""Environment isolation for agent sessions.

This module provides functions to prepare isolated environments for agent sessions:
- Scrub forbidden environment variables (credentials, tokens)
- Set isolated HOME directory
- Generate shell commands to apply isolation

Security principle: Agents should not have access to credentials that could
allow them to perform privileged operations (push, merge, API calls).
"""

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Environment variables that should be scrubbed before agent sessions
# These are credentials that could allow agents to bypass guardrails
FORBIDDEN_ENV_VARS = [
    # GitHub tokens
    "GH_TOKEN",
    "GITHUB_TOKEN",
    "GH_ENTERPRISE_TOKEN",
    "GITHUB_ENTERPRISE_TOKEN",
    # GitHub App credentials
    "GH_APP_ID",
    "GH_APP_PRIVATE_KEY",
    "GH_INSTALLATION_ID",
    # OAuth tokens
    "GITHUB_OAUTH_TOKEN",
    # Other potentially dangerous credentials
    "NPM_TOKEN",
    "PYPI_TOKEN",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    # SSH agent - can forward credentials
    "SSH_AUTH_SOCK",
]

# Environment variables to set for safe git behavior
GIT_SAFE_ENV = {
    "GIT_TERMINAL_PROMPT": "0",  # Disable interactive prompts
    "GIT_ASKPASS": "/usr/bin/false",  # Fail any credential requests
}


def get_forbidden_env_vars() -> list[str]:
    """Get the list of environment variables that should be scrubbed.

    Returns:
        List of environment variable names to unset
    """
    return FORBIDDEN_ENV_VARS.copy()


def build_env_unset_commands() -> list[str]:
    """Build shell commands to unset forbidden environment variables.

    Returns:
        List of shell 'unset' commands
    """
    return [f"unset {var}" for var in FORBIDDEN_ENV_VARS]


def build_git_safe_commands() -> list[str]:
    """Build shell commands to set git-safe environment variables.

    These prevent git from prompting for credentials interactively.

    Returns:
        List of shell 'export' commands
    """
    return [f'export {var}="{val}"' for var, val in GIT_SAFE_ENV.items()]


def build_home_isolation_command(worktree: Path) -> str:
    """Build shell command to set HOME to the worktree.

    This isolates the agent's home directory to prevent access to
    credentials stored in ~/.config, ~/.ssh, ~/.gh, etc.

    Args:
        worktree: Path to the worktree directory

    Returns:
        Shell export command to set HOME
    """
    return f'export HOME="{worktree}"'


def build_isolation_prefix(
    worktree: Path,
    isolation_mode: str = "standard",
    scrub_env: bool = True,
    isolate_home: bool = True,
    git_safe: bool = True,
) -> str:
    """Build a shell command prefix that applies isolation.

    This returns a string of shell commands (separated by &&) that:
    1. Unset forbidden environment variables
    2. Set HOME to the worktree (if standard mode)
    3. Set GIT_TERMINAL_PROMPT=0 and GIT_ASKPASS to prevent prompts

    Args:
        worktree: Path to the worktree directory
        isolation_mode: "standard" or "hardened"
        scrub_env: Whether to scrub environment variables
        isolate_home: Whether to isolate HOME directory
        git_safe: Whether to set git-safe environment variables

    Returns:
        Shell command prefix string
    """
    commands = []

    if scrub_env:
        commands.extend(build_env_unset_commands())
        logger.debug("Added env scrubbing commands for %d variables", len(FORBIDDEN_ENV_VARS))

    if isolate_home and isolation_mode == "standard":
        commands.append(build_home_isolation_command(worktree))
        logger.debug("Added HOME isolation to %s", worktree)

    if git_safe:
        commands.extend(build_git_safe_commands())
        logger.debug("Added git-safe environment variables")

    if commands:
        return " && ".join(commands) + " && "
    return ""


def verify_env_scrubbed() -> dict[str, bool]:
    """Verify that forbidden environment variables are not set.

    This is meant to be run inside an agent session to verify isolation.

    Returns:
        Dict mapping variable names to whether they are absent (True = good)
    """
    import os

    results = {}
    for var in FORBIDDEN_ENV_VARS:
        results[var] = os.environ.get(var) is None
    return results


def all_env_scrubbed() -> bool:
    """Check if all forbidden environment variables are absent.

    Returns:
        True if all forbidden variables are absent
    """
    return all(verify_env_scrubbed().values())
