"""Orchestrator environment variables - single source of truth.

These env vars are set by the orchestrator when launching agent sessions,
and read by tools like agent-done and prepush-check.

This module provides:
1. Constants for env var names (used by session_launcher, agent_done, tests)
2. Functions to build env exports for shell commands
3. Test helpers to simulate worktree environment
"""

from dataclasses import dataclass
from typing import Optional


# Env var names - the single source of truth
class EnvVars:
    """Orchestrator environment variable names."""

    # Core session env vars (always set)
    COMPLETION_PATH = "ORCHESTRATOR_COMPLETION_PATH"
    AGENT_LABEL = "ORCHESTRATOR_AGENT_LABEL"
    ISSUE_NUMBER = "ORCHESTRATOR_ISSUE_NUMBER"
    API_PORT = "ORCHESTRATOR_API_PORT"

    # Validation env vars (set if validation configured)
    VALIDATION_CMD = "ORCHESTRATOR_VALIDATION_CMD"
    VALIDATION_TIMEOUT = "ORCHESTRATOR_VALIDATION_TIMEOUT"

    # Optional session env vars
    SESSION_ID = "ORCHESTRATOR_SESSION_ID"

    @classmethod
    def all_names(cls) -> list[str]:
        """Return all env var names (for clearing in tests)."""
        return [
            cls.COMPLETION_PATH,
            cls.AGENT_LABEL,
            cls.ISSUE_NUMBER,
            cls.API_PORT,
            cls.VALIDATION_CMD,
            cls.VALIDATION_TIMEOUT,
            cls.SESSION_ID,
        ]

    @classmethod
    def validation_names(cls) -> list[str]:
        """Return validation-related env var names."""
        return [cls.VALIDATION_CMD, cls.VALIDATION_TIMEOUT]


@dataclass
class SessionEnvConfig:
    """Configuration for session environment variables."""

    completion_path: str
    agent_label: str
    issue_number: int
    api_port: int
    validation_cmd: Optional[str] = None
    validation_timeout: Optional[int] = None
    session_id: Optional[str] = None


def build_env_exports(config: SessionEnvConfig) -> str:
    """Build shell export command for session env vars.

    Args:
        config: Session configuration

    Returns:
        Shell command like "export ORCHESTRATOR_COMPLETION_PATH='...' ORCHESTRATOR_AGENT_LABEL='...'"
    """
    exports = f"export {EnvVars.COMPLETION_PATH}='{config.completion_path}'"
    exports += f" {EnvVars.AGENT_LABEL}='{config.agent_label}'"
    exports += f" {EnvVars.ISSUE_NUMBER}='{config.issue_number}'"
    exports += f" {EnvVars.API_PORT}='{config.api_port}'"

    if config.validation_cmd:
        exports += f" {EnvVars.VALIDATION_CMD}='{config.validation_cmd}'"
        if config.validation_timeout:
            exports += f" {EnvVars.VALIDATION_TIMEOUT}='{config.validation_timeout}'"

    if config.session_id:
        exports += f" {EnvVars.SESSION_ID}='{config.session_id}'"

    return exports


def get_test_env_dict() -> dict[str, str]:
    """Get a dict of env vars for simulating worktree environment in tests.

    Returns:
        Dict mapping env var names to test values.
        Used by CI and tests to simulate orchestrator environment.
    """
    return {
        EnvVars.COMPLETION_PATH: ".issue-orchestrator/completion.json",
        EnvVars.AGENT_LABEL: "agent:backend",
        EnvVars.ISSUE_NUMBER: "9999",
        EnvVars.API_PORT: "8080",
        EnvVars.VALIDATION_CMD: "make validate",
        EnvVars.VALIDATION_TIMEOUT: "300",
    }
