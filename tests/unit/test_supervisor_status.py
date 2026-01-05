"""Tests for supervisor module."""

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from issue_orchestrator.infra.supervisor import (
    SupervisorStatus,
    status,
)


class TestSupervisorStatus:
    """Tests for supervisor status function."""

    def test_status_stopped_no_lock(self, tmp_path: Path) -> None:
        """Return stopped state when no lock file exists."""
        result = status(tmp_path)

        assert result.state == "stopped"
        assert result.pid is None
        assert result.port is None

    def test_status_running_with_live_process(self, tmp_path: Path) -> None:
        """Return running state when lock exists and process is alive."""
        # Create lock file with current process PID
        lock_dir = tmp_path / ".issue-orchestrator"
        lock_dir.mkdir(parents=True)
        lock_path = lock_dir / "lock.json"

        lock_data = {
            "repo_root": str(tmp_path),
            "pid": os.getpid(),  # Current process is alive
            "started_at": "2024-01-01T00:00:00Z",
            "http_port": 8080,
            "state_dir": str(tmp_path / ".issue-orchestrator" / "state"),
            "recovered": False,
        }
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)

        result = status(tmp_path)

        assert result.state == "running"
        assert result.pid == os.getpid()
        assert result.port == 8080
        assert result.started_at == "2024-01-01T00:00:00Z"
        assert result.recovered is False

    def test_status_failed_with_dead_process(self, tmp_path: Path) -> None:
        """Return failed state when lock exists but process is dead."""
        # Create lock file with non-existent PID
        lock_dir = tmp_path / ".issue-orchestrator"
        lock_dir.mkdir(parents=True)
        lock_path = lock_dir / "lock.json"

        lock_data = {
            "repo_root": str(tmp_path),
            "pid": 999999999,  # Very unlikely to be a real process
            "started_at": "2024-01-01T00:00:00Z",
            "http_port": 8080,
            "state_dir": str(tmp_path / ".issue-orchestrator" / "state"),
            "recovered": False,
        }
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)

        result = status(tmp_path)

        assert result.state == "failed"
        assert result.pid == 999999999
        assert result.port == 8080
        assert "stale lock" in result.error.lower()

    def test_status_recovered_flag_preserved(self, tmp_path: Path) -> None:
        """Preserve recovered flag from lock file."""
        lock_dir = tmp_path / ".issue-orchestrator"
        lock_dir.mkdir(parents=True)
        lock_path = lock_dir / "lock.json"

        lock_data = {
            "repo_root": str(tmp_path),
            "pid": os.getpid(),
            "started_at": "2024-01-01T00:00:00Z",
            "http_port": 8080,
            "state_dir": str(tmp_path / ".issue-orchestrator" / "state"),
            "recovered": True,  # Was recovered from stale lock
        }
        with open(lock_path, "w") as f:
            json.dump(lock_data, f)

        result = status(tmp_path)

        assert result.state == "running"
        assert result.recovered is True


class TestSupervisorStatusToDict:
    """Tests for SupervisorStatus.to_dict method."""

    def test_to_dict_all_fields(self) -> None:
        """Convert status with all fields to dict."""
        status_obj = SupervisorStatus(
            state="running",
            pid=12345,
            port=8080,
            started_at="2024-01-01T00:00:00Z",
            recovered=True,
            error=None,
        )

        data = status_obj.to_dict()

        assert data["state"] == "running"
        assert data["pid"] == 12345
        assert data["port"] == 8080
        assert data["started_at"] == "2024-01-01T00:00:00Z"
        assert data["recovered"] is True
        assert data["error"] is None

    def test_to_dict_stopped(self) -> None:
        """Convert stopped status to dict."""
        status_obj = SupervisorStatus(state="stopped")

        data = status_obj.to_dict()

        assert data["state"] == "stopped"
        assert data["pid"] is None
        assert data["port"] is None

    def test_to_dict_failed_with_error(self) -> None:
        """Convert failed status with error to dict."""
        status_obj = SupervisorStatus(
            state="failed",
            pid=12345,
            error="Process not running (stale lock)",
        )

        data = status_obj.to_dict()

        assert data["state"] == "failed"
        assert data["error"] == "Process not running (stale lock)"
