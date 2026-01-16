"""Tests for supervisor module."""

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from issue_orchestrator.infra.supervisor import (
    SupervisorStatus,
    status,
    start,
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


class TestSupervisorStartErrorSurfacing:
    """Tests for error message extraction when orchestrator fails to start."""

    def test_start_failure_extracts_error_from_log(self, tmp_path: Path) -> None:
        """When process exits immediately, extract ERROR line from log."""
        # Setup: create state directory and log file with an error
        state_dir = tmp_path / ".issue-orchestrator" / "state"
        logs_dir = state_dir / "logs"
        logs_dir.mkdir(parents=True)
        log_file = logs_dir / "orchestrator.log"
        log_file.write_text(
            "2024-01-01 [INFO] Starting...\n"
            "2024-01-01 [ERROR] __main__: Could not determine GitHub repository\n"
        )

        # Also need config dir for start() to work
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text("agents: {}\n")

        # Mock subprocess to simulate immediate exit
        mock_process = MagicMock()
        mock_process.pid = 99999
        mock_process.poll.return_value = 1  # Exited with code 1

        with patch("issue_orchestrator.infra.supervisor.subprocess.Popen", return_value=mock_process):
            with patch("issue_orchestrator.infra.supervisor.read_lock", return_value=None):
                with pytest.raises(RuntimeError) as exc_info:
                    start(tmp_path)

        error_msg = str(exc_info.value)
        assert "exited immediately with code 1" in error_msg
        assert "Could not determine GitHub repository" in error_msg
        assert "Full logs at:" in error_msg

    def test_start_failure_shows_last_line_if_no_error(self, tmp_path: Path) -> None:
        """When no ERROR in log, show last non-empty line as hint."""
        state_dir = tmp_path / ".issue-orchestrator" / "state"
        logs_dir = state_dir / "logs"
        logs_dir.mkdir(parents=True)
        log_file = logs_dir / "orchestrator.log"
        log_file.write_text(
            "2024-01-01 [INFO] Starting...\n"
            "2024-01-01 [INFO] Building orchestrator...\n"
            "2024-01-01 [INFO] Some final message\n"
        )

        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text("agents: {}\n")

        mock_process = MagicMock()
        mock_process.pid = 99999
        mock_process.poll.return_value = 1

        with patch("issue_orchestrator.infra.supervisor.subprocess.Popen", return_value=mock_process):
            with patch("issue_orchestrator.infra.supervisor.read_lock", return_value=None):
                with pytest.raises(RuntimeError) as exc_info:
                    start(tmp_path)

        error_msg = str(exc_info.value)
        assert "Some final message" in error_msg

    def test_start_failure_handles_missing_log(self, tmp_path: Path) -> None:
        """When log file doesn't exist, still show basic error."""
        # Don't create log file, but need config dir
        state_dir = tmp_path / ".issue-orchestrator" / "state"
        state_dir.mkdir(parents=True)

        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        (config_dir / "default.yaml").write_text("agents: {}\n")

        mock_process = MagicMock()
        mock_process.pid = 99999
        mock_process.poll.return_value = 1

        with patch("issue_orchestrator.infra.supervisor.subprocess.Popen", return_value=mock_process):
            with patch("issue_orchestrator.infra.supervisor.read_lock", return_value=None):
                with pytest.raises(RuntimeError) as exc_info:
                    start(tmp_path)

        error_msg = str(exc_info.value)
        assert "exited immediately with code 1" in error_msg
        # Should still mention where logs would be
        assert "logs" in error_msg.lower()
