"""Unit tests for E2E runner manager."""

import pytest
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

from issue_orchestrator.infra.e2e_runner import (
    E2ERunnerManager,
    E2EAlreadyRunning,
    _resolve_repo_python,
    get_e2e_runner_manager,
    maybe_trigger_e2e,
)
from issue_orchestrator.infra.e2e_db import E2EDB, E2ERun


class TestE2ERunnerManager:
    """Test the E2ERunnerManager class."""

    @pytest.fixture
    def manager(self) -> E2ERunnerManager:
        """Create a fresh manager for each test."""
        return E2ERunnerManager()

    @pytest.fixture
    def mock_popen(self):
        """Mock subprocess.Popen for testing."""
        with patch("subprocess.Popen") as mock:
            proc = MagicMock()
            proc.pid = 12345
            proc.poll.return_value = None  # Running
            mock.return_value = proc
            yield mock, proc

    def test_start_success(self, manager: E2ERunnerManager, mock_popen, tmp_path: Path):
        """Test successful start of E2E worker."""
        popen_mock, proc = mock_popen

        result = manager.start(
            repo_root=tmp_path,
            orchestrator_id="test-orch",
            pytest_args=["tests/e2e", "-v"],
            allow_retry_once=True,
        )

        assert result["pid"] == 12345
        assert "log_path" in result

        # Verify subprocess was called correctly
        popen_mock.assert_called_once()
        call_args = popen_mock.call_args
        cmd = call_args[0][0]
        assert "--repo-root" in cmd
        assert "--orchestrator-id" in cmd
        assert "--allow-retry-once" in cmd

    def test_start_already_running(self, manager: E2ERunnerManager, mock_popen, tmp_path: Path):
        """Test that starting while running raises AlreadyRunning."""
        popen_mock, proc = mock_popen

        # Start first run
        manager.start(tmp_path, "test-orch", ["tests/e2e"])

        # Second start should fail
        with pytest.raises(E2EAlreadyRunning) as exc_info:
            manager.start(tmp_path, "test-orch", ["tests/e2e"])

        assert exc_info.value.orchestrator_id == "test-orch"
        assert exc_info.value.pid == 12345

    def test_start_after_previous_finished(self, manager: E2ERunnerManager, mock_popen, tmp_path: Path):
        """Test starting after previous run finished."""
        popen_mock, proc = mock_popen

        # Start first run
        manager.start(tmp_path, "test-orch", ["tests/e2e"])

        # Simulate process finishing
        proc.poll.return_value = 0

        # Should be able to start again
        result = manager.start(tmp_path, "test-orch", ["tests/e2e"])
        assert result["pid"] == 12345

    def test_status_not_running(self, manager: E2ERunnerManager):
        """Test status when no process is running."""
        status = manager.status("unknown-orch")

        assert status["running"] is False
        assert status["pid"] is None
        assert status["exit_code"] is None

    def test_status_running(self, manager: E2ERunnerManager, mock_popen, tmp_path: Path):
        """Test status when process is running."""
        popen_mock, proc = mock_popen

        manager.start(tmp_path, "test-orch", ["tests/e2e"])

        status = manager.status("test-orch")

        assert status["running"] is True
        assert status["pid"] == 12345
        assert status["exit_code"] is None

    def test_status_finished(self, manager: E2ERunnerManager, mock_popen, tmp_path: Path):
        """Test status after process finished."""
        popen_mock, proc = mock_popen

        manager.start(tmp_path, "test-orch", ["tests/e2e"])

        # Simulate process finishing
        proc.poll.return_value = 0

        status = manager.status("test-orch")

        assert status["running"] is False
        assert status["pid"] == 12345
        assert status["exit_code"] == 0

    def test_stop_running_process(self, manager: E2ERunnerManager, mock_popen, tmp_path: Path):
        """Test stopping a running process."""
        popen_mock, proc = mock_popen
        proc.wait.return_value = None

        manager.start(tmp_path, "test-orch", ["tests/e2e"])

        with patch("os.kill") as kill_mock:
            result = manager.stop("test-orch")

        assert result is True
        kill_mock.assert_called_once()

    def test_stop_not_running(self, manager: E2ERunnerManager):
        """Test stopping when nothing is running."""
        result = manager.stop("unknown-orch")
        assert result is False

    def test_cleanup_finished(self, manager: E2ERunnerManager, mock_popen, tmp_path: Path):
        """Test cleanup of finished processes."""
        popen_mock, proc = mock_popen

        # Start multiple runs
        manager.start(tmp_path, "orch-1", ["tests/e2e"])

        # Create another mock process
        proc2 = MagicMock()
        proc2.pid = 54321
        proc2.poll.return_value = 0  # Already finished
        popen_mock.return_value = proc2
        manager.start(tmp_path, "orch-2", ["tests/e2e"])

        # Mark first as finished too
        proc.poll.return_value = 1

        finished = manager.cleanup_finished()

        assert "orch-1" in finished
        assert "orch-2" in finished


class TestMaybeTriggerE2E:
    """Test the maybe_trigger_e2e function."""

    @pytest.fixture
    def mock_config(self, tmp_path: Path):
        """Create a mock config with E2E enabled."""
        config = MagicMock()
        config.e2e.enabled = True
        config.e2e.auto_run_interval_minutes = 30
        config.e2e.pytest_args = ["tests/e2e", "-v"]
        config.e2e.allow_retry_once = True
        config.e2e.quarantine_file = "tests/e2e/quarantine.txt"
        config.e2e.role = "auto"  # Default role
        config.repo_root = tmp_path
        config.orchestrator_id = "test-orch"
        return config

    def test_trigger_disabled(self, mock_config, tmp_path: Path):
        """Test that trigger returns False when E2E is disabled."""
        mock_config.e2e.enabled = False

        result = maybe_trigger_e2e(mock_config, tmp_path, "test-orch")

        assert result is False

    def test_trigger_auto_run_zero(self, mock_config, tmp_path: Path):
        """Test that trigger returns False when auto_run_interval is 0."""
        mock_config.e2e.auto_run_interval_minutes = 0

        result = maybe_trigger_e2e(mock_config, tmp_path, "test-orch")

        assert result is False

    @patch("issue_orchestrator.infra.e2e_runner.get_e2e_runner_manager")
    def test_trigger_already_running(self, mock_get_manager, mock_config, tmp_path: Path):
        """Test that trigger returns False when already running."""
        mock_manager = MagicMock()
        mock_manager.status.return_value = {"running": True, "pid": 123}
        mock_get_manager.return_value = mock_manager

        result = maybe_trigger_e2e(mock_config, tmp_path, "test-orch")

        assert result is False

    @patch("issue_orchestrator.infra.e2e_runner._get_main_head")
    @patch("issue_orchestrator.infra.e2e_runner.get_e2e_runner_manager")
    @patch("issue_orchestrator.infra.e2e_runner.E2EDB")
    def test_trigger_too_soon(self, mock_db_class, mock_get_manager, mock_get_head, mock_config, tmp_path: Path):
        """Test that trigger returns False when last run was too recent."""
        from datetime import datetime, timezone, timedelta

        mock_manager = MagicMock()
        mock_manager.status.return_value = {"running": False}
        mock_get_manager.return_value = mock_manager

        # Mock DB to return a recent run
        mock_db = MagicMock()
        recent_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        mock_run = MagicMock()
        mock_run.finished_at = recent_time
        mock_run.commit_sha = "abc123"
        mock_db.latest_run.return_value = mock_run
        mock_db_class.return_value = mock_db

        # Mock HEAD check
        mock_get_head.return_value = "def456"  # Different commit

        # Create the DB file so the check proceeds
        (tmp_path / ".issue-orchestrator").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".issue-orchestrator" / "e2e.db").touch()

        result = maybe_trigger_e2e(mock_config, tmp_path, "test-orch")

        assert result is False

    @patch("issue_orchestrator.infra.e2e_runner._get_main_head")
    @patch("issue_orchestrator.infra.e2e_runner.get_e2e_runner_manager")
    @patch("issue_orchestrator.infra.e2e_runner.E2EDB")
    def test_trigger_same_head_skips(self, mock_db_class, mock_get_manager, mock_get_head, mock_config, tmp_path: Path):
        """Test that trigger returns False when main HEAD unchanged."""
        from datetime import datetime, timezone, timedelta

        mock_manager = MagicMock()
        mock_manager.status.return_value = {"running": False}
        mock_get_manager.return_value = mock_manager

        # Mock DB to return an old run with same commit
        mock_db = MagicMock()
        old_time = (datetime.now(timezone.utc) - timedelta(hours=5)).isoformat()
        mock_run = MagicMock()
        mock_run.finished_at = old_time
        mock_run.commit_sha = "abc123"
        mock_db.latest_run.return_value = mock_run
        mock_db_class.return_value = mock_db

        # Mock HEAD check - same commit
        mock_get_head.return_value = "abc123"

        # Create the DB file so the check proceeds
        (tmp_path / ".issue-orchestrator").mkdir(parents=True, exist_ok=True)
        (tmp_path / ".issue-orchestrator" / "e2e.db").touch()

        result = maybe_trigger_e2e(mock_config, tmp_path, "test-orch")

        assert result is False

    @patch("issue_orchestrator.infra.e2e_runner._get_main_head")
    @patch("issue_orchestrator.infra.e2e_runner.get_e2e_runner_manager")
    def test_trigger_success(self, mock_get_manager, mock_get_head, mock_config, tmp_path: Path):
        """Test successful trigger when all conditions met."""
        mock_manager = MagicMock()
        mock_manager.status.return_value = {"running": False}
        mock_manager.start_or_resume.return_value = {"pid": 123, "log_path": "/tmp/log", "resumed": False}
        mock_get_manager.return_value = mock_manager
        mock_get_head.return_value = "abc123"

        # No DB file - first run
        result = maybe_trigger_e2e(mock_config, tmp_path, "test-orch")

        assert result is True
        mock_manager.start_or_resume.assert_called_once()

    @patch("issue_orchestrator.infra.e2e_runner._get_main_head")
    @patch("issue_orchestrator.infra.e2e_runner.get_e2e_runner_manager")
    def test_trigger_handles_start_exception(self, mock_get_manager, mock_get_head, mock_config, tmp_path: Path):
        """Test that trigger handles exceptions from start_or_resume."""
        mock_manager = MagicMock()
        mock_manager.status.return_value = {"running": False}
        mock_manager.start_or_resume.side_effect = Exception("Failed to start")
        mock_get_manager.return_value = mock_manager
        mock_get_head.return_value = "abc123"

        result = maybe_trigger_e2e(mock_config, tmp_path, "test-orch")

        assert result is False


class TestGetE2ERunnerManager:
    """Test the singleton getter."""

    def test_returns_same_instance(self):
        """Test that get_e2e_runner_manager returns the same instance."""
        # Reset singleton for test isolation
        import issue_orchestrator.infra.e2e_runner as module
        module._runner_manager = None  # noqa: SLF001

        manager1 = get_e2e_runner_manager()
        manager2 = get_e2e_runner_manager()

        assert manager1 is manager2


class TestStopOnFirstFailure:
    """Tests for the stop_on_first_failure configuration."""

    @pytest.fixture
    def manager(self) -> E2ERunnerManager:
        """Create a fresh manager for each test."""
        return E2ERunnerManager()

    @pytest.fixture
    def mock_popen(self):
        """Mock subprocess.Popen for testing."""
        with patch("subprocess.Popen") as mock:
            proc = MagicMock()
            proc.pid = 12345
            proc.poll.return_value = None
            mock.return_value = proc
            yield mock, proc

    def test_start_adds_x_flag_when_stop_on_first_failure_true(
        self, manager: E2ERunnerManager, mock_popen, tmp_path: Path
    ):
        """Test that -x flag is added to pytest_args when stop_on_first_failure=True."""
        popen_mock, proc = mock_popen

        with patch("builtins.open", MagicMock()):
            manager.start(
                repo_root=tmp_path,
                orchestrator_id="test-orch",
                pytest_args=["tests/e2e", "-v"],
                stop_on_first_failure=True,
            )

        # Extract the pytest-args-json from the command
        call_args = popen_mock.call_args
        cmd = call_args[0][0]

        # Find the pytest args in the command
        pytest_args_idx = cmd.index("--pytest-args-json") + 1
        import json
        pytest_args = json.loads(cmd[pytest_args_idx])

        assert "-x" in pytest_args

    def test_start_no_x_flag_when_stop_on_first_failure_false(
        self, manager: E2ERunnerManager, mock_popen, tmp_path: Path
    ):
        """Test that -x flag is NOT added when stop_on_first_failure=False."""
        popen_mock, proc = mock_popen

        with patch("builtins.open", MagicMock()):
            manager.start(
                repo_root=tmp_path,
                orchestrator_id="test-orch",
                pytest_args=["tests/e2e", "-v"],
                stop_on_first_failure=False,
            )

        call_args = popen_mock.call_args
        cmd = call_args[0][0]

        pytest_args_idx = cmd.index("--pytest-args-json") + 1
        import json
        pytest_args = json.loads(cmd[pytest_args_idx])

        assert "-x" not in pytest_args

    def test_start_no_duplicate_x_flag(
        self, manager: E2ERunnerManager, mock_popen, tmp_path: Path
    ):
        """Test that -x flag is not duplicated if already in pytest_args."""
        popen_mock, proc = mock_popen

        with patch("builtins.open", MagicMock()):
            manager.start(
                repo_root=tmp_path,
                orchestrator_id="test-orch",
                pytest_args=["tests/e2e", "-v", "-x"],  # Already has -x
                stop_on_first_failure=True,
            )

        call_args = popen_mock.call_args
        cmd = call_args[0][0]

        pytest_args_idx = cmd.index("--pytest-args-json") + 1
        import json
        pytest_args = json.loads(cmd[pytest_args_idx])

        # Should only have one -x
        assert pytest_args.count("-x") == 1


class TestLogFileCapture:
    """Tests for log file output capture."""

    @pytest.fixture
    def manager(self) -> E2ERunnerManager:
        """Create a fresh manager for each test."""
        return E2ERunnerManager()

    def test_start_opens_log_file_for_writing(
        self, manager: E2ERunnerManager, tmp_path: Path
    ):
        """Test that start() opens log file and passes it to Popen."""
        mock_file = MagicMock()

        with patch("subprocess.Popen") as popen_mock, \
             patch("builtins.open", return_value=mock_file) as open_mock:
            proc = MagicMock()
            proc.pid = 12345
            proc.poll.return_value = None
            popen_mock.return_value = proc

            result = manager.start(
                repo_root=tmp_path,
                orchestrator_id="test-orch",
                pytest_args=["tests/e2e"],
            )

            # Verify open was called with write mode
            open_mock.assert_called_once()
            call_args = open_mock.call_args
            assert call_args[0][1] == "w"  # Write mode

            # Verify log file handle was passed to Popen as stdout
            popen_call = popen_mock.call_args
            assert popen_call.kwargs["stdout"] == mock_file

    def test_start_returns_log_path(
        self, manager: E2ERunnerManager, tmp_path: Path
    ):
        """Test that start() returns the log path in the result."""
        with patch("subprocess.Popen") as popen_mock, \
             patch("builtins.open", MagicMock()):
            proc = MagicMock()
            proc.pid = 12345
            proc.poll.return_value = None
            popen_mock.return_value = proc

            result = manager.start(
                repo_root=tmp_path,
                orchestrator_id="test-orch",
                pytest_args=["tests/e2e"],
            )

            assert "log_path" in result
            assert result["log_path"].endswith(".log")
            assert "e2e" in result["log_path"]


class TestResolveRepoPython:
    """Test _resolve_repo_python uses the repo's venv when available."""

    def test_uses_repo_venv_when_exists(self, tmp_path: Path):
        """When repo_root has .venv/bin/python, use it."""
        venv_python = tmp_path / ".venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_python.touch()

        result = _resolve_repo_python(tmp_path)
        assert result == str(venv_python)

    def test_falls_back_to_sys_executable(self, tmp_path: Path):
        """When no venv exists, fall back to sys.executable."""
        import sys

        result = _resolve_repo_python(tmp_path)
        assert result == sys.executable

    def test_worker_uses_repo_venv(self, tmp_path: Path):
        """Verify the E2E worker subprocess gets the repo's Python, not sys.executable."""
        import sys

        venv_python = tmp_path / ".venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_python.touch()

        manager = E2ERunnerManager()
        with patch("subprocess.Popen") as popen_mock, \
             patch("builtins.open", MagicMock()):
            proc = MagicMock()
            proc.pid = 12345
            proc.poll.return_value = None
            popen_mock.return_value = proc

            manager.start(
                repo_root=tmp_path,
                orchestrator_id="test-orch",
                pytest_args=["tests/e2e"],
            )

            cmd = popen_mock.call_args[0][0]
            assert cmd[0] == str(venv_python), (
                f"Expected repo venv python {venv_python}, got {cmd[0]}"
            )
            assert cmd[0] != sys.executable
