"""Tests for ai_diagnose module."""

import json
import os
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from issue_orchestrator.infra.ai_diagnose import (
    DiagnosticBundle,
    DiagnoseResult,
    create_diagnostic_bundle,
    _get_safe_env,
)
from issue_orchestrator.infra.startup_errors import StartupError, write_startup_failure


class TestGetSafeEnv:
    """Tests for _get_safe_env function."""

    def test_strips_github_token(self) -> None:
        """GITHUB_TOKEN is stripped from environment."""
        with patch.dict(os.environ, {"GITHUB_TOKEN": "secret123", "PATH": "/usr/bin"}):
            safe_env = _get_safe_env()

        assert "GITHUB_TOKEN" not in safe_env
        assert "PATH" in safe_env

    def test_strips_gh_token(self) -> None:
        """GH_TOKEN is stripped from environment."""
        with patch.dict(os.environ, {"GH_TOKEN": "secret456", "HOME": "/home/user"}):
            safe_env = _get_safe_env()

        assert "GH_TOKEN" not in safe_env
        assert "HOME" in safe_env

    def test_strips_provider_api_key(self) -> None:
        """Registered provider API keys are stripped from environment."""
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-xxx", "SHELL": "/bin/bash"}):
            safe_env = _get_safe_env()

        assert "OPENAI_API_KEY" not in safe_env
        assert "SHELL" in safe_env

    def test_strips_issue_orch_github_token(self) -> None:
        """ISSUE_ORCH_GITHUB_TOKEN is stripped from environment."""
        with patch.dict(os.environ, {"ISSUE_ORCH_GITHUB_TOKEN": "ghp_xxx", "USER": "testuser"}):
            safe_env = _get_safe_env()

        assert "ISSUE_ORCH_GITHUB_TOKEN" not in safe_env
        assert "USER" in safe_env

    def test_keeps_safe_vars(self) -> None:
        """Safe environment variables are preserved."""
        with patch.dict(os.environ, {"PATH": "/usr/bin", "HOME": "/home/user", "LANG": "en_US.UTF-8"}, clear=True):
            safe_env = _get_safe_env()

        assert safe_env["PATH"] == "/usr/bin"
        assert safe_env["HOME"] == "/home/user"
        assert safe_env["LANG"] == "en_US.UTF-8"


class TestDiagnosticBundle:
    """Tests for DiagnosticBundle dataclass."""

    def test_to_summary_with_failure(self, tmp_path: Path) -> None:
        """Generate summary with failure info."""
        bundle = DiagnosticBundle(
            bundle_path=tmp_path,
            last_failure={
                "phase": "auth",
                "message": "Token invalid",
                "suggested_fix": "Get new token",
            },
            log_tail=["Line 1", "Line 2"],
            doctor_output={
                "overall": "error",
                "checks": [{"name": "Auth", "status": "error", "detail": "Failed"}],
            },
        )

        summary = bundle.to_summary()

        assert "Token invalid" in summary
        assert "Get new token" in summary
        assert "Line 1" in summary
        assert "Auth" in summary
        assert "Failed" in summary

    def test_to_summary_without_failure(self, tmp_path: Path) -> None:
        """Generate summary without failure info."""
        bundle = DiagnosticBundle(bundle_path=tmp_path)

        summary = bundle.to_summary()

        assert "No recent failures recorded" in summary


class TestCreateDiagnosticBundle:
    """Tests for create_diagnostic_bundle function."""

    @pytest.fixture(autouse=True)
    def _fast_doctor(self, monkeypatch):
        from issue_orchestrator.infra.doctor.types import DoctorResult

        monkeypatch.setattr(
            "issue_orchestrator.infra.doctor.run_doctor",
            lambda **_: DoctorResult(),
        )

    def test_creates_bundle_directory(self, tmp_path: Path) -> None:
        """Bundle directory is created."""
        bundle = create_diagnostic_bundle(tmp_path)

        assert bundle.bundle_path.exists()
        assert bundle.bundle_path.is_dir()

    def test_includes_last_failure(self, tmp_path: Path) -> None:
        """Bundle includes last failure if present."""
        error = StartupError(
            phase="bootstrap",
            message="Config missing",
            suggested_fix="Run init",
        )
        write_startup_failure(tmp_path, error)

        bundle = create_diagnostic_bundle(tmp_path)

        assert bundle.last_failure is not None
        assert bundle.last_failure["phase"] == "bootstrap"
        assert bundle.last_failure["message"] == "Config missing"

        # Should also be written to bundle directory
        failure_file = bundle.bundle_path / "last_failure.json"
        assert failure_file.exists()

    def test_includes_log_tail(self, tmp_path: Path) -> None:
        """Bundle includes log tail if log file exists."""
        log_dir = tmp_path / ".issue-orchestrator" / "state" / "logs"
        log_dir.mkdir(parents=True)
        log_path = log_dir / "orchestrator.log"

        lines = [f"Log line {i}" for i in range(10)]
        with open(log_path, "w") as f:
            f.write("\n".join(lines))

        bundle = create_diagnostic_bundle(tmp_path)

        assert len(bundle.log_tail) == 10
        assert "Log line 0" in bundle.log_tail[0]

        # Should also be written to bundle directory
        log_tail_file = bundle.bundle_path / "log_tail.txt"
        assert log_tail_file.exists()

    def test_includes_config_files(self, tmp_path: Path) -> None:
        """Bundle includes config files if present."""
        config_content = "repo:\n  name: test/repo\nagents: {}"
        # Config files are now in .issue-orchestrator/config/
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        with open(config_path, "w") as f:
            f.write(config_content)

        bundle = create_diagnostic_bundle(tmp_path)

        assert "default.yaml" in bundle.config_files
        assert "name: test/repo" in bundle.config_files["default.yaml"]

    def test_writes_summary_file(self, tmp_path: Path) -> None:
        """Bundle writes summary.md file."""
        bundle = create_diagnostic_bundle(tmp_path)

        summary_path = bundle.bundle_path / "summary.md"
        assert summary_path.exists()

        content = summary_path.read_text()
        assert "Diagnostic Bundle" in content


class TestDiagnoseResult:
    """Tests for DiagnoseResult dataclass."""

    def test_to_dict_success(self, tmp_path: Path) -> None:
        """Convert successful result to dict."""
        report_path = tmp_path / "report.md"
        result = DiagnoseResult(
            success=True,
            report_path=report_path,
            report_content="Analysis complete",
        )

        data = result.to_dict()

        assert data["success"] is True
        assert data["report_path"] == str(report_path)
        assert data["report_content"] == "Analysis complete"
        assert data["error"] == ""

    def test_to_dict_failure(self) -> None:
        """Convert failed result to dict."""
        result = DiagnoseResult(
            success=False,
            error="claude not found",
        )

        data = result.to_dict()

        assert data["success"] is False
        assert data["report_path"] is None
        assert data["error"] == "claude not found"
