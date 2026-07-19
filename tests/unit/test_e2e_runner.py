"""Unit tests for E2E runner manager."""

import json
import os

import pytest
import subprocess
from pathlib import Path
from unittest.mock import patch, MagicMock

from issue_orchestrator.infra import e2e_runner
from issue_orchestrator.infra.config_models import E2EConfig
from issue_orchestrator.infra.e2e_runner import (
    E2ERunnerManager,
    E2EAlreadyRunning,
    E2ESlotSignals,
    _build_worker_env,
    _resolve_repo_python,
    get_e2e_runner_manager,
    is_e2e_due,
    make_e2e_slot_reader,
    maybe_trigger_e2e,
)
from issue_orchestrator.infra.e2e_db import E2EDB, E2ERun


@pytest.fixture
def e2e_worktree_path(tmp_path: Path) -> Path:
    """Return a worktree directory for tests that need it."""
    wt = tmp_path / "repo-e2e-worktree"
    wt.mkdir()
    return wt


def test_build_worker_env_sets_source_root_when_pythonpath_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("PYTHONPATH", raising=False)

    env = _build_worker_env()

    source_root = str(Path(e2e_runner.__file__).resolve().parents[2])
    assert env["PYTHONPATH"] == source_root


def test_build_worker_env_prepends_source_root_and_preserves_pythonpath(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    existing = os.pathsep.join(["/tmp/one", "/tmp/two"])
    monkeypatch.setenv("PYTHONPATH", existing)

    env = _build_worker_env()

    source_root = str(Path(e2e_runner.__file__).resolve().parents[2])
    assert env["PYTHONPATH"] == os.pathsep.join([source_root, existing])


@pytest.fixture(autouse=True)
def mock_ensure_e2e_worktree(e2e_worktree_path: Path):
    """Patch ensure_e2e_worktree so start()/resume don't run real git commands."""
    with patch(
        "issue_orchestrator.infra.e2e_runner.ensure_e2e_worktree",
        return_value=e2e_worktree_path,
    ) as mock:
        yield mock


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


class TestWorktreeIsolation:
    """Test that E2E runs use the worktree for isolation."""

    @pytest.fixture
    def manager(self) -> E2ERunnerManager:
        return E2ERunnerManager()

    @pytest.fixture
    def mock_popen(self):
        with patch("subprocess.Popen") as mock:
            proc = MagicMock()
            proc.pid = 12345
            proc.poll.return_value = None
            mock.return_value = proc
            yield mock, proc

    def test_start_uses_worktree_as_cwd(
        self,
        manager: E2ERunnerManager,
        mock_popen,
        tmp_path: Path,
        e2e_worktree_path: Path,
    ):
        """start() should run the subprocess with cwd=worktree."""
        popen_mock, _ = mock_popen

        manager.start(repo_root=tmp_path, orchestrator_id="test-orch", pytest_args=["tests/e2e"])

        popen_call = popen_mock.call_args
        assert popen_call.kwargs["cwd"] == e2e_worktree_path

    def test_start_resets_blocked_signal_mask_in_worker(
        self,
        manager: E2ERunnerManager,
        mock_popen,
        tmp_path: Path,
    ):
        """The worker spawn must reset the orchestrator's blocked signal mask.

        Regression for PR #6452 P1: E2E workers are stopped via SIGTERM in
        ``E2ERunnerManager.stop``, so they must not inherit the orchestrator's
        process-wide SIGTERM/SIGINT block — otherwise graceful stop is skipped
        and the worker is force-killed. Asserts the manager's own spawn path
        wires the reset preexec (KeyError here if it is ever dropped), not just
        that the generic helper exists.
        """
        from issue_orchestrator.infra.shutdown_signals import child_signal_reset_preexec

        popen_mock, _ = mock_popen
        manager.start(
            repo_root=tmp_path, orchestrator_id="test-orch", pytest_args=["tests/e2e"]
        )

        assert popen_mock.call_args.kwargs["preexec_fn"] is child_signal_reset_preexec()

    def test_start_repo_root_arg_points_to_worktree(
        self,
        manager: E2ERunnerManager,
        mock_popen,
        tmp_path: Path,
        e2e_worktree_path: Path,
    ):
        """--repo-root in the subprocess cmd should point to the worktree."""
        popen_mock, _ = mock_popen

        manager.start(repo_root=tmp_path, orchestrator_id="test-orch", pytest_args=["tests/e2e"])

        cmd = popen_mock.call_args[0][0]
        repo_root_idx = cmd.index("--repo-root") + 1
        assert cmd[repo_root_idx] == str(e2e_worktree_path)

    def test_start_db_path_stays_in_base_repo(
        self,
        manager: E2ERunnerManager,
        mock_popen,
        tmp_path: Path,
        e2e_worktree_path: Path,
    ):
        """--db-path should stay in the base repo (not the worktree)."""
        popen_mock, _ = mock_popen

        manager.start(repo_root=tmp_path, orchestrator_id="test-orch", pytest_args=["tests/e2e"])

        cmd = popen_mock.call_args[0][0]
        db_path_idx = cmd.index("--db-path") + 1
        db_path = Path(cmd[db_path_idx])
        # DB must be under the base repo, not the worktree
        assert str(db_path).startswith(str(tmp_path))
        assert str(e2e_worktree_path) not in str(db_path)

    def test_start_log_path_stays_in_base_repo(
        self,
        manager: E2ERunnerManager,
        mock_popen,
        tmp_path: Path,
        e2e_worktree_path: Path,
    ):
        """--log-file should stay in the base repo."""
        popen_mock, _ = mock_popen

        manager.start(repo_root=tmp_path, orchestrator_id="test-orch", pytest_args=["tests/e2e"])

        cmd = popen_mock.call_args[0][0]
        log_idx = cmd.index("--log-file") + 1
        log_path = cmd[log_idx]
        assert str(tmp_path) in log_path
        assert str(e2e_worktree_path) not in log_path

    def test_start_uses_worktree_python(
        self,
        manager: E2ERunnerManager,
        mock_popen,
        tmp_path: Path,
        e2e_worktree_path: Path,
    ):
        """Python interpreter should come from the worktree's venv."""
        popen_mock, _ = mock_popen

        # Create venv in worktree (not base repo)
        wt_python = e2e_worktree_path / ".venv" / "bin" / "python"
        wt_python.parent.mkdir(parents=True)
        wt_python.touch()

        manager.start(repo_root=tmp_path, orchestrator_id="test-orch", pytest_args=["tests/e2e"])

        cmd = popen_mock.call_args[0][0]
        assert cmd[0] == str(wt_python)

    def test_resume_uses_worktree_as_cwd(
        self,
        tmp_path: Path,
        e2e_worktree_path: Path,
    ):
        """_resume_run should also use the worktree as cwd."""
        # Set up DB with an interrupted run
        db_path = tmp_path / ".issue-orchestrator" / "e2e.db"
        db_path.parent.mkdir(parents=True)
        db = E2EDB(db_path)
        run_id = db.start_run(
            repo_root=str(tmp_path),
            orchestrator_id="test-orch",
            pytest_args=["tests/e2e", "-v"],
            commit_sha="abc123",
            branch="main",
        )
        db.upsert_test_result(run_id, "tests/e2e/test_foo.py::test_bar", "passed", 1.0)
        db.finish_run(run_id, "interrupted")

        manager = E2ERunnerManager()
        with patch("subprocess.Popen") as popen_mock, \
             patch("builtins.open", MagicMock()):
            proc = MagicMock()
            proc.pid = 12345
            proc.poll.return_value = None
            popen_mock.return_value = proc

            result = manager.start_or_resume(
                repo_root=tmp_path,
                orchestrator_id="test-orch",
                pytest_args=["tests/e2e", "-v"],
            )

            assert result["resumed"] is True
            popen_call = popen_mock.call_args
            assert popen_call.kwargs["cwd"] == e2e_worktree_path

    def test_resume_repo_root_points_to_worktree(
        self,
        tmp_path: Path,
        e2e_worktree_path: Path,
    ):
        """On resume, --repo-root should point to the worktree."""
        db_path = tmp_path / ".issue-orchestrator" / "e2e.db"
        db_path.parent.mkdir(parents=True)
        db = E2EDB(db_path)
        run_id = db.start_run(
            repo_root=str(tmp_path),
            orchestrator_id="test-orch",
            pytest_args=["tests/e2e"],
            commit_sha="abc123",
            branch="main",
        )
        db.upsert_test_result(run_id, "tests/e2e/test_foo.py::test_bar", "passed", 1.0)
        db.finish_run(run_id, "interrupted")

        manager = E2ERunnerManager()
        with patch("subprocess.Popen") as popen_mock, \
             patch("builtins.open", MagicMock()):
            proc = MagicMock()
            proc.pid = 12345
            proc.poll.return_value = None
            popen_mock.return_value = proc

            manager.start_or_resume(
                repo_root=tmp_path,
                orchestrator_id="test-orch",
                pytest_args=["tests/e2e"],
            )

            cmd = popen_mock.call_args[0][0]
            repo_root_idx = cmd.index("--repo-root") + 1
            assert cmd[repo_root_idx] == str(e2e_worktree_path)

    def test_resume_db_path_stays_in_base_repo(
        self,
        tmp_path: Path,
        e2e_worktree_path: Path,
    ):
        """On resume, --db-path should stay in the base repo."""
        db_path = tmp_path / ".issue-orchestrator" / "e2e.db"
        db_path.parent.mkdir(parents=True)
        db = E2EDB(db_path)
        run_id = db.start_run(
            repo_root=str(tmp_path),
            orchestrator_id="test-orch",
            pytest_args=["tests/e2e"],
            commit_sha="abc123",
            branch="main",
        )
        db.upsert_test_result(run_id, "tests/e2e/test_foo.py::test_bar", "passed", 1.0)
        db.finish_run(run_id, "interrupted")

        manager = E2ERunnerManager()
        with patch("subprocess.Popen") as popen_mock, \
             patch("builtins.open", MagicMock()):
            proc = MagicMock()
            proc.pid = 12345
            proc.poll.return_value = None
            popen_mock.return_value = proc

            manager.start_or_resume(
                repo_root=tmp_path,
                orchestrator_id="test-orch",
                pytest_args=["tests/e2e"],
            )

            cmd = popen_mock.call_args[0][0]
            db_path_idx = cmd.index("--db-path") + 1
            assert str(tmp_path) in cmd[db_path_idx]
            assert str(e2e_worktree_path) not in cmd[db_path_idx]

    def test_ensure_e2e_worktree_called_with_repo_root(
        self,
        manager: E2ERunnerManager,
        mock_popen,
        mock_ensure_e2e_worktree,
        tmp_path: Path,
    ):
        """ensure_e2e_worktree should receive the original repo_root."""
        manager.start(repo_root=tmp_path, orchestrator_id="test-orch", pytest_args=["tests/e2e"])

        mock_ensure_e2e_worktree.assert_called_once_with(tmp_path)


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
        # First-class worker workload OFF by default: these tests assert the
        # unchanged trigger path (no worker-slot start-gate). A truthy MagicMock
        # default would silently arm the gate, so pin it to the real default.
        config.e2e.occupies_session_slot = False
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


class TestE2EWorkerSlotStartGate:
    """e2e.occupies_session_slot: the worker-slot start-gate on the trigger.

    When the flag is on, a run only starts if the caller reports a free worker
    slot. When off, ``worker_slot_free`` is ignored (byte-for-byte unchanged).
    """

    @pytest.fixture
    def slot_config(self, tmp_path: Path):
        config = MagicMock()
        config.e2e.enabled = True
        config.e2e.auto_run_interval_minutes = 30
        config.e2e.pytest_args = ["tests/e2e", "-v"]
        config.e2e.allow_retry_once = True
        config.e2e.quarantine_file = "tests/e2e/quarantine.txt"
        config.e2e.role = "auto"
        config.e2e.occupies_session_slot = True
        config.e2e.auto_quarantine = False
        config.e2e.run_retention_count = 50
        config.repo_root = tmp_path
        config.orchestrator_id = "test-orch"
        return config

    @patch("issue_orchestrator.infra.e2e_runner._get_main_head")
    @patch("issue_orchestrator.infra.e2e_runner.get_e2e_runner_manager")
    def test_flag_on_no_free_slot_defers(self, mock_get_manager, mock_get_head, slot_config, tmp_path):
        mock_manager = MagicMock()
        mock_manager.status.return_value = {"running": False}
        mock_get_manager.return_value = mock_manager
        mock_get_head.return_value = "abc123"

        result = maybe_trigger_e2e(
            slot_config, tmp_path, "test-orch", worker_slot_free=False
        )

        assert result is False
        mock_manager.start_or_resume.assert_not_called()

    @patch("issue_orchestrator.infra.e2e_runner._get_main_head")
    @patch("issue_orchestrator.infra.e2e_runner.get_e2e_runner_manager")
    def test_flag_on_free_slot_starts(self, mock_get_manager, mock_get_head, slot_config, tmp_path):
        mock_manager = MagicMock()
        mock_manager.status.return_value = {"running": False}
        mock_manager.start_or_resume.return_value = {"pid": 1, "log_path": "/tmp/l", "resumed": False}
        mock_get_manager.return_value = mock_manager
        mock_get_head.return_value = "abc123"

        result = maybe_trigger_e2e(
            slot_config, tmp_path, "test-orch", worker_slot_free=True
        )

        assert result is True
        mock_manager.start_or_resume.assert_called_once()

    @patch("issue_orchestrator.infra.e2e_runner._get_main_head")
    @patch("issue_orchestrator.infra.e2e_runner.get_e2e_runner_manager")
    def test_flag_off_ignores_worker_slot(self, mock_get_manager, mock_get_head, slot_config, tmp_path):
        """Flag off: a saturated worker budget does NOT block the trigger."""
        slot_config.e2e.occupies_session_slot = False
        mock_manager = MagicMock()
        mock_manager.status.return_value = {"running": False}
        mock_manager.start_or_resume.return_value = {"pid": 1, "log_path": "/tmp/l", "resumed": False}
        mock_get_manager.return_value = mock_manager
        mock_get_head.return_value = "abc123"

        result = maybe_trigger_e2e(
            slot_config, tmp_path, "test-orch", worker_slot_free=False
        )

        assert result is True


class TestE2ESlotReader:
    """make_e2e_slot_reader: the observation feed the fact gatherer threads
    into the snapshot so the planner learns E2E is running / due."""

    def _config(self, tmp_path, *, occupies: bool):
        config = MagicMock()
        config.repo_root = tmp_path
        config.orchestrator_id = "test-orch"
        config.e2e.occupies_session_slot = occupies
        return config

    @patch("issue_orchestrator.infra.e2e_runner.get_e2e_runner_manager")
    def test_flag_off_returns_empty_without_touching_runner(self, mock_get_manager, tmp_path):
        reader = make_e2e_slot_reader(self._config(tmp_path, occupies=False))

        assert reader() == E2ESlotSignals(occupies_slot=False, due=False)
        mock_get_manager.assert_not_called()

    @patch("issue_orchestrator.infra.e2e_runner.get_e2e_runner_manager")
    def test_running_reports_occupies_slot(self, mock_get_manager, tmp_path):
        mock_manager = MagicMock()
        mock_manager.status.return_value = {"running": True, "pid": 7}
        mock_get_manager.return_value = mock_manager

        reader = make_e2e_slot_reader(self._config(tmp_path, occupies=True))
        signals = reader()

        assert signals.occupies_slot is True
        assert signals.due is False

    @patch("issue_orchestrator.infra.e2e_runner.is_e2e_due")
    @patch("issue_orchestrator.infra.e2e_runner.get_e2e_runner_manager")
    def test_due_when_not_running_and_due(self, mock_get_manager, mock_is_due, tmp_path):
        mock_manager = MagicMock()
        mock_manager.status.return_value = {"running": False}
        mock_get_manager.return_value = mock_manager
        mock_is_due.return_value = True

        reader = make_e2e_slot_reader(self._config(tmp_path, occupies=True))
        signals = reader()

        assert signals.occupies_slot is False
        assert signals.due is True

    @patch("issue_orchestrator.infra.e2e_runner.is_e2e_due")
    @patch("issue_orchestrator.infra.e2e_runner.get_e2e_runner_manager")
    def test_not_running_not_due_is_empty(self, mock_get_manager, mock_is_due, tmp_path):
        mock_manager = MagicMock()
        mock_manager.status.return_value = {"running": False}
        mock_get_manager.return_value = mock_manager
        mock_is_due.return_value = False

        reader = make_e2e_slot_reader(self._config(tmp_path, occupies=True))

        assert reader() == E2ESlotSignals(occupies_slot=False, due=False)


def test_command_runner_execution_spec_requires_command() -> None:
    """Command-mode E2E config must declare the command to execute."""
    config = E2EConfig(runner_kind="command", command=[])

    with pytest.raises(ValueError, match="e2e.command must be configured"):
        config.execution_spec()


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

        # Extract the execution-spec-json from the command
        call_args = popen_mock.call_args
        cmd = call_args[0][0]

        execution_spec_idx = cmd.index("--execution-spec-json") + 1
        execution_spec = json.loads(cmd[execution_spec_idx])
        pytest_args = execution_spec["pytest_args"]

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

        execution_spec_idx = cmd.index("--execution-spec-json") + 1
        execution_spec = json.loads(cmd[execution_spec_idx])
        pytest_args = execution_spec["pytest_args"]

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

        execution_spec_idx = cmd.index("--execution-spec-json") + 1
        execution_spec = json.loads(cmd[execution_spec_idx])
        pytest_args = execution_spec["pytest_args"]

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


def test_command_runner_interrupted_run_restarts_fresh(tmp_path: Path) -> None:
    """Command-mode interrupted runs should fail the stale run and start fresh."""
    db_path = tmp_path / ".issue-orchestrator" / "e2e.db"
    db_path.parent.mkdir(parents=True)
    db = E2EDB(db_path)
    interrupted_id = db.start_run(
        repo_root=str(tmp_path),
        orchestrator_id="test-orch",
        pytest_args=[],
        commit_sha="abc123",
        branch="main",
        command=["python", "scripts/run_suite.py"],
        runner_kind="command",
    )
    db.finish_run(interrupted_id, "interrupted")

    manager = E2ERunnerManager()
    with patch("subprocess.Popen") as popen_mock, \
         patch("builtins.open", MagicMock()):
        proc = MagicMock()
        proc.pid = 12345
        proc.poll.return_value = None
        popen_mock.return_value = proc

        result = manager.start_or_resume(
            repo_root=tmp_path,
            orchestrator_id="test-orch",
            execution_spec=e2e_runner.E2EExecutionSpec(
                runner_kind="command",
                command=("python", "scripts/run_suite.py"),
                junit_xml_paths=("artifacts/results.xml",),
            ),
        )

    assert result["resumed"] is False

    stale_run = db.get_run(interrupted_id)
    assert stale_run is not None
    assert stale_run.status == "failed"
    assert stale_run.note == "Interrupted command-style run restarted from scratch"


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

    def test_worker_uses_worktree_venv(self, tmp_path: Path, e2e_worktree_path: Path):
        """Verify the E2E worker subprocess gets the worktree's Python."""
        import sys

        # Create venv in the worktree (where _resolve_repo_python now looks)
        venv_python = e2e_worktree_path / ".venv" / "bin" / "python"
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
                f"Expected worktree venv python {venv_python}, got {cmd[0]}"
            )
            assert cmd[0] != sys.executable

    def test_resume_run_uses_worktree_venv(self, tmp_path: Path, e2e_worktree_path: Path):
        """Verify _resume_run also uses the worktree's Python."""
        import sys

        # Create venv in the worktree
        venv_python = e2e_worktree_path / ".venv" / "bin" / "python"
        venv_python.parent.mkdir(parents=True)
        venv_python.touch()

        # Set up DB with an interrupted run so start_or_resume triggers _resume_run
        db_path = tmp_path / ".issue-orchestrator" / "e2e.db"
        db_path.parent.mkdir(parents=True)
        db = E2EDB(db_path)
        run_id = db.start_run(
            repo_root=str(tmp_path),
            orchestrator_id="test-orch",
            pytest_args=["tests/e2e", "-v"],
            commit_sha="abc123",
            branch="main",
        )
        # Add a passed test so resume path is taken (not "no progress, start fresh")
        db.upsert_test_result(run_id, "tests/e2e/test_foo.py::test_bar", "passed", 1.0)
        # Mark as interrupted so get_interrupted_run() finds it
        db.finish_run(run_id, "interrupted")

        manager = E2ERunnerManager()
        with patch("subprocess.Popen") as popen_mock, \
             patch("builtins.open", MagicMock()):
            proc = MagicMock()
            proc.pid = 12345
            proc.poll.return_value = None
            popen_mock.return_value = proc

            result = manager.start_or_resume(
                repo_root=tmp_path,
                orchestrator_id="test-orch",
                pytest_args=["tests/e2e", "-v"],
            )

            assert result["resumed"] is True
            cmd = popen_mock.call_args[0][0]
            assert cmd[0] == str(venv_python), (
                f"Expected worktree venv python {venv_python}, got {cmd[0]}"
            )
            assert cmd[0] != sys.executable
