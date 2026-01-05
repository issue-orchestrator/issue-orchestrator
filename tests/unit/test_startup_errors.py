"""Tests for startup_errors module."""

import json
from pathlib import Path

import pytest

from issue_orchestrator.infra.startup_errors import (
    StartupError,
    auth_error_invalid_token,
    auth_error_missing_token,
    bootstrap_error_invalid_config,
    bootstrap_error_no_config,
    clear_startup_failure,
    labels_error_missing_labels,
    read_startup_failure,
    runtime_error,
    write_startup_failure,
)


class TestStartupError:
    """Tests for StartupError dataclass."""

    def test_create_startup_error(self) -> None:
        """Create a startup error with all fields."""
        error = StartupError(
            phase="auth",
            message="Token invalid",
            suggested_fix="Get a new token",
            details="HTTP 401",
        )

        assert error.phase == "auth"
        assert error.message == "Token invalid"
        assert error.suggested_fix == "Get a new token"
        assert error.details == "HTTP 401"
        assert error.timestamp  # Should be auto-populated

    def test_to_dict(self) -> None:
        """Convert to dict."""
        error = StartupError(
            phase="bootstrap",
            message="Config missing",
            suggested_fix="Run init",
        )

        data = error.to_dict()

        assert data["phase"] == "bootstrap"
        assert data["message"] == "Config missing"
        assert data["suggested_fix"] == "Run init"
        assert "timestamp" in data

    def test_from_dict(self) -> None:
        """Create from dict."""
        data = {
            "phase": "labels",
            "message": "Missing labels",
            "suggested_fix": "Create labels",
            "details": "foo, bar",
            "timestamp": "2024-01-01T00:00:00Z",
        }

        error = StartupError.from_dict(data)

        assert error.phase == "labels"
        assert error.message == "Missing labels"
        assert error.suggested_fix == "Create labels"
        assert error.details == "foo, bar"
        assert error.timestamp == "2024-01-01T00:00:00Z"


class TestWriteReadFailure:
    """Tests for write/read startup failure functions."""

    def test_write_startup_failure(self, tmp_path: Path) -> None:
        """Write a startup failure to disk."""
        error = StartupError(
            phase="auth",
            message="No token",
            suggested_fix="Set GITHUB_TOKEN",
        )

        path = write_startup_failure(tmp_path, error)

        assert path.exists()
        with open(path) as f:
            data = json.load(f)
        assert data["phase"] == "auth"
        assert data["message"] == "No token"

    def test_read_startup_failure(self, tmp_path: Path) -> None:
        """Read a startup failure from disk."""
        error = StartupError(
            phase="bootstrap",
            message="Config error",
            suggested_fix="Check syntax",
        )
        write_startup_failure(tmp_path, error)

        result = read_startup_failure(tmp_path)

        assert result is not None
        assert result.phase == "bootstrap"
        assert result.message == "Config error"

    def test_read_startup_failure_not_exists(self, tmp_path: Path) -> None:
        """Return None when no failure file exists."""
        result = read_startup_failure(tmp_path)
        assert result is None

    def test_read_startup_failure_invalid_json(self, tmp_path: Path) -> None:
        """Return None when failure file is invalid JSON."""
        state_dir = tmp_path / ".issue-orchestrator" / "state"
        state_dir.mkdir(parents=True)
        failure_path = state_dir / "last_failure.json"

        with open(failure_path, "w") as f:
            f.write("not valid json")

        result = read_startup_failure(tmp_path)
        assert result is None

    def test_clear_startup_failure(self, tmp_path: Path) -> None:
        """Clear startup failure from disk."""
        error = StartupError(
            phase="runtime",
            message="Crash",
            suggested_fix="Restart",
        )
        write_startup_failure(tmp_path, error)

        result = clear_startup_failure(tmp_path)

        assert result is True
        assert read_startup_failure(tmp_path) is None

    def test_clear_startup_failure_not_exists(self, tmp_path: Path) -> None:
        """Return False when no failure file to clear."""
        result = clear_startup_failure(tmp_path)
        assert result is False


class TestPredefinedErrors:
    """Tests for predefined error factory functions."""

    def test_auth_error_missing_token(self) -> None:
        """Create missing token error."""
        error = auth_error_missing_token()

        assert error.phase == "auth"
        assert "token" in error.message.lower()
        assert "GITHUB_TOKEN" in error.suggested_fix

    def test_auth_error_invalid_token(self) -> None:
        """Create invalid token error."""
        error = auth_error_invalid_token(username="testuser", error="401 Unauthorized")

        assert error.phase == "auth"
        assert "invalid" in error.message.lower()
        assert "testuser" in error.details

    def test_bootstrap_error_no_config(self) -> None:
        """Create no config error."""
        error = bootstrap_error_no_config()

        assert error.phase == "bootstrap"
        assert "configuration" in error.message.lower()

    def test_bootstrap_error_invalid_config(self) -> None:
        """Create invalid config error."""
        error = bootstrap_error_invalid_config("YAML syntax error at line 10")

        assert error.phase == "bootstrap"
        assert "invalid" in error.message.lower()
        assert "line 10" in error.details

    def test_labels_error_missing_labels(self) -> None:
        """Create missing labels error."""
        error = labels_error_missing_labels(["orch-ready", "orch-done"])

        assert error.phase == "labels"
        assert "orch-ready" in error.message
        assert "orch-done" in error.message

    def test_runtime_error(self) -> None:
        """Create runtime error."""
        error = runtime_error("Connection timeout", "Failed after 30s")

        assert error.phase == "runtime"
        assert "timeout" in error.message.lower()
        assert "30s" in error.details
