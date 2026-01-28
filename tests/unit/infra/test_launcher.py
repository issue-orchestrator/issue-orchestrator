"""Tests for the unified launcher module."""

import pytest
from unittest.mock import MagicMock, patch

from issue_orchestrator.infra.launcher import (
    LaunchResult,
    launch_preflight_only,
    launch_subprocess,
    preflight,
)
from issue_orchestrator.infra.doctor.types import Check, DoctorResult


@pytest.fixture
def mock_config():
    config = MagicMock()
    config.instances = 1
    return config


class TestLaunchResult:
    def test_to_dict_minimal(self):
        doctor = DoctorResult(checks=[Check(name="Test", status="ok", detail="good")])
        result = LaunchResult(doctor=doctor, launched=False, status="ok")
        d = result.to_dict()
        assert d["launched"] is False
        assert d["status"] == "ok"
        assert d["doctor"]["overall"] == "ok"
        assert "error" not in d
        assert "supervisor" not in d

    def test_to_dict_with_error(self):
        doctor = DoctorResult(checks=[Check(name="Test", status="error", detail="bad")])
        result = LaunchResult(
            doctor=doctor, launched=False, status="doctor_error", error="check failed"
        )
        d = result.to_dict()
        assert d["error"] == "check failed"
        assert d["status"] == "doctor_error"

    def test_to_dict_with_supervisor(self):
        doctor = DoctorResult(checks=[])
        result = LaunchResult(
            doctor=doctor,
            launched=True,
            status="ok",
            supervisor={"pid": 123, "port": 8080},
        )
        d = result.to_dict()
        assert d["supervisor"]["pid"] == 123
        assert d["launched"] is True


class TestPreflight:
    @patch("issue_orchestrator.infra.launcher.run_doctor")
    def test_preflight_ok(self, mock_doctor, mock_config):
        mock_doctor.return_value = DoctorResult(
            checks=[Check(name="Test", status="ok", detail="good")]
        )
        result = preflight(mock_config)
        assert result.status == "ok"
        assert result.launched is False

    @patch("issue_orchestrator.infra.launcher.run_doctor")
    def test_preflight_warning(self, mock_doctor, mock_config):
        mock_doctor.return_value = DoctorResult(
            checks=[Check(name="Repo", status="warning", detail="not configured")]
        )
        result = preflight(mock_config)
        assert result.status == "doctor_warning"
        assert result.launched is False

    @patch("issue_orchestrator.infra.launcher.run_doctor")
    def test_preflight_error(self, mock_doctor, mock_config):
        mock_doctor.return_value = DoctorResult(
            checks=[Check(name="Hooks", status="error", detail="not installed")]
        )
        result = preflight(mock_config)
        assert result.status == "doctor_error"
        assert result.launched is False


class TestLaunchPreflightOnly:
    @patch("issue_orchestrator.infra.launcher.run_doctor")
    def test_is_alias_for_preflight(self, mock_doctor, mock_config):
        mock_doctor.return_value = DoctorResult(
            checks=[Check(name="Test", status="ok", detail="good")]
        )
        result = launch_preflight_only(mock_config)
        assert result.status == "ok"
        assert result.launched is False


class TestLaunchSubprocess:
    @patch("issue_orchestrator.infra.launcher.supervisor")
    @patch("issue_orchestrator.infra.launcher.run_doctor")
    def test_launch_ok(self, mock_doctor, mock_supervisor, mock_config, tmp_path):
        mock_doctor.return_value = DoctorResult(
            checks=[Check(name="Test", status="ok", detail="good")]
        )
        mock_lock = MagicMock()
        mock_lock.pid = 42
        mock_lock.http_port = 8080
        mock_lock.instance_id = None
        mock_supervisor.start.return_value = mock_lock

        result = launch_subprocess(tmp_path, mock_config)
        assert result.launched is True
        assert result.status == "ok"
        assert result.supervisor["pid"] == 42
        assert result.supervisor["port"] == 8080

    @patch("issue_orchestrator.infra.launcher.supervisor")
    @patch("issue_orchestrator.infra.launcher.run_doctor")
    def test_launch_blocked_by_doctor_error(
        self, mock_doctor, mock_supervisor, mock_config, tmp_path
    ):
        mock_doctor.return_value = DoctorResult(
            checks=[Check(name="Hooks", status="error", detail="not installed")]
        )
        result = launch_subprocess(tmp_path, mock_config)
        assert result.launched is False
        assert result.status == "doctor_error"
        mock_supervisor.start.assert_not_called()

    @patch("issue_orchestrator.infra.launcher.supervisor")
    @patch("issue_orchestrator.infra.launcher.run_doctor")
    def test_launch_with_doctor_warning_still_launches(
        self, mock_doctor, mock_supervisor, mock_config, tmp_path
    ):
        mock_doctor.return_value = DoctorResult(
            checks=[Check(name="Repo", status="warning", detail="not set")]
        )
        mock_lock = MagicMock()
        mock_lock.pid = 42
        mock_lock.http_port = 8080
        mock_lock.instance_id = None
        mock_supervisor.start.return_value = mock_lock

        result = launch_subprocess(tmp_path, mock_config)
        assert result.launched is True
        assert result.status == "doctor_warning"

    @patch("issue_orchestrator.infra.launcher.supervisor")
    @patch("issue_orchestrator.infra.launcher.run_doctor")
    def test_launch_supervisor_error(
        self, mock_doctor, mock_supervisor, mock_config, tmp_path
    ):
        mock_doctor.return_value = DoctorResult(
            checks=[Check(name="Test", status="ok", detail="good")]
        )
        mock_supervisor.start.side_effect = RuntimeError("port in use")

        result = launch_subprocess(tmp_path, mock_config)
        assert result.launched is False
        assert result.status == "launch_error"
        assert "port in use" in result.error

    @patch("issue_orchestrator.infra.launcher.supervisor")
    @patch("issue_orchestrator.infra.launcher.run_doctor")
    def test_launch_multi_instance(
        self, mock_doctor, mock_supervisor, mock_config, tmp_path
    ):
        mock_config.instances = 3
        mock_doctor.return_value = DoctorResult(
            checks=[Check(name="Test", status="ok", detail="good")]
        )
        mock_info1 = MagicMock(pid=1, http_port=8081, instance_id="i1")
        mock_info2 = MagicMock(pid=2, http_port=8082, instance_id="i2")
        mock_supervisor.start_instances.return_value = [mock_info1, mock_info2]

        result = launch_subprocess(tmp_path, mock_config)
        assert result.launched is True
        assert "instances" in result.supervisor
        assert len(result.supervisor["instances"]) == 2
