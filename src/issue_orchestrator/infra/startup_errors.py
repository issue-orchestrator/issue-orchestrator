"""Startup error handling and recording.

Provides structured error types for startup failures and utilities
to record/read them from disk.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from .repo_identity import state_dir

# Valid startup phases
StartupPhase = Literal["bootstrap", "auth", "labels", "reconcile", "runtime"]


@dataclass
class StartupError:
    """A structured startup failure record.

    Attributes:
        phase: Which phase of startup failed
        message: Human-readable error message
        suggested_fix: Actionable fix suggestion
        details: Optional additional details (stack trace, etc.)
        timestamp: When the error occurred
    """

    phase: StartupPhase
    message: str
    suggested_fix: str
    details: str = ""
    timestamp: str = ""

    def __post_init__(self):
        if not self.timestamp:
            self.timestamp = datetime.now(timezone.utc).isoformat()

    def to_dict(self) -> dict[str, Any]:
        """Convert to dict for JSON serialization."""
        return {
            "phase": self.phase,
            "message": self.message,
            "suggested_fix": self.suggested_fix,
            "details": self.details,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "StartupError":
        """Create from dict (JSON deserialization)."""
        return cls(
            phase=data["phase"],
            message=data["message"],
            suggested_fix=data["suggested_fix"],
            details=data.get("details", ""),
            timestamp=data.get("timestamp", ""),
        )


def write_startup_failure(repo_root: Path | str, error: StartupError) -> Path:
    """Write a startup failure to disk.

    Args:
        repo_root: Repository root path
        error: The startup error to record

    Returns:
        Path to the written failure file
    """
    failure_dir = state_dir(repo_root)
    failure_dir.mkdir(parents=True, exist_ok=True)

    failure_path = failure_dir / "last_failure.json"

    with open(failure_path, "w") as f:
        json.dump(error.to_dict(), f, indent=2)

    return failure_path


def read_startup_failure(repo_root: Path | str) -> StartupError | None:
    """Read the last startup failure from disk.

    Args:
        repo_root: Repository root path

    Returns:
        StartupError if failure file exists, None otherwise
    """
    failure_path = state_dir(repo_root) / "last_failure.json"

    if not failure_path.exists():
        return None

    try:
        with open(failure_path) as f:
            data = json.load(f)
        return StartupError.from_dict(data)
    except (json.JSONDecodeError, KeyError, TypeError):
        return None


def clear_startup_failure(repo_root: Path | str) -> bool:
    """Clear the last startup failure from disk.

    Args:
        repo_root: Repository root path

    Returns:
        True if file was removed, False if it didn't exist
    """
    failure_path = state_dir(repo_root) / "last_failure.json"

    if failure_path.exists():
        failure_path.unlink()
        return True
    return False


# Predefined startup errors for common failure modes


def auth_error_missing_token() -> StartupError:
    """Create error for missing GitHub token."""
    return StartupError(
        phase="auth",
        message="No GitHub token found",
        suggested_fix="Set GITHUB_TOKEN environment variable or run: issue-orch setup",
        details="Checked: ISSUE_ORCH_GITHUB_TOKEN, GITHUB_TOKEN, GH_TOKEN, keyring",
    )


def auth_error_invalid_token(username: str | None = None, error: str = "") -> StartupError:
    """Create error for invalid GitHub token."""
    return StartupError(
        phase="auth",
        message="GitHub token is invalid or expired",
        suggested_fix="Generate a new token at https://github.com/settings/tokens",
        details=f"User: {username or 'unknown'}, Error: {error}",
    )


def bootstrap_error_no_config() -> StartupError:
    """Create error for missing config file."""
    return StartupError(
        phase="bootstrap",
        message="No configuration file found",
        suggested_fix="Run: issue-orchestrator setup",
        details="Searched for config in .issue-orchestrator/config/",
    )


def bootstrap_error_invalid_config(error: str) -> StartupError:
    """Create error for invalid config file."""
    return StartupError(
        phase="bootstrap",
        message="Configuration file is invalid",
        suggested_fix="Check your config file in .issue-orchestrator/config/ for syntax errors",
        details=error,
    )


def labels_error_missing_labels(labels: list[str]) -> StartupError:
    """Create error for missing labels in repo."""
    return StartupError(
        phase="labels",
        message=f"Required labels missing from repository: {', '.join(labels)}",
        suggested_fix="Run: issue-orch setup --create-labels",
        details=f"Missing: {labels}",
    )


def runtime_error(message: str, details: str = "") -> StartupError:
    """Create a generic runtime error."""
    return StartupError(
        phase="runtime",
        message=message,
        suggested_fix="Check the orchestrator logs for more details",
        details=details,
    )
