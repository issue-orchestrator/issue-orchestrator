"""Tests for validate_runner CLI.

The validate_runner captures validation output to a known location
so agents can find failure details without re-running tests.
"""

import os
import subprocess
import sys
from pathlib import Path

import pytest


class TestValidateRunner:
    """Test the validate_runner CLI."""

    @pytest.fixture
    def fake_git_repo(self, tmp_path: Path) -> Path:
        """Create a fake git repo structure for testing."""
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / ".git").mkdir()
        return repo

    def test_captures_output_to_env_var_dir(self, fake_git_repo: Path, tmp_path: Path):
        """Test that output is captured to ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR."""
        output_dir = tmp_path / "session-output"
        output_dir.mkdir()

        result = subprocess.run(
            [
                sys.executable, "-m",
                "issue_orchestrator.entrypoints.cli_tools.validate_runner",
                "--command", "echo 'test output'"
            ],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR": str(output_dir),
            },
        )

        assert result.returncode == 0
        output_file = output_dir / "validation-output.log"
        assert output_file.exists()
        assert "test output" in output_file.read_text()

    def test_falls_back_to_diagnostics_dir(self, fake_git_repo: Path):
        """Test that output falls back to .issue-orchestrator/diagnostics/."""
        result = subprocess.run(
            [
                sys.executable, "-m",
                "issue_orchestrator.entrypoints.cli_tools.validate_runner",
                "--command", "echo 'fallback test'"
            ],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env={k: v for k, v in os.environ.items() if not k.startswith("ISSUE_ORCHESTRATOR")},
        )

        assert result.returncode == 0
        output_file = fake_git_repo / ".issue-orchestrator" / "diagnostics" / "validation-output.log"
        assert output_file.exists()
        assert "fallback test" in output_file.read_text()

    def test_prints_path_on_failure(self, fake_git_repo: Path, tmp_path: Path):
        """Test that failure message includes path to output file."""
        output_dir = tmp_path / "session-output"
        output_dir.mkdir()

        result = subprocess.run(
            [
                sys.executable, "-m",
                "issue_orchestrator.entrypoints.cli_tools.validate_runner",
                "--command", "exit 1"
            ],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR": str(output_dir),
            },
        )

        assert result.returncode == 1
        assert "Full output saved to:" in result.stdout
        assert "validation-output.log" in result.stdout

    def test_returns_command_exit_code(self, fake_git_repo: Path, tmp_path: Path):
        """Test that exit code matches the underlying command."""
        output_dir = tmp_path / "session-output"
        output_dir.mkdir()

        # Test exit code 0
        result = subprocess.run(
            [
                sys.executable, "-m",
                "issue_orchestrator.entrypoints.cli_tools.validate_runner",
                "--command", "exit 0"
            ],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR": str(output_dir),
            },
        )
        assert result.returncode == 0

        # Test exit code 42
        result = subprocess.run(
            [
                sys.executable, "-m",
                "issue_orchestrator.entrypoints.cli_tools.validate_runner",
                "--command", "exit 42"
            ],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR": str(output_dir),
            },
        )
        assert result.returncode == 42

    def test_captures_stderr(self, fake_git_repo: Path, tmp_path: Path):
        """Test that stderr is captured in the output file."""
        output_dir = tmp_path / "session-output"
        output_dir.mkdir()

        subprocess.run(
            [
                sys.executable, "-m",
                "issue_orchestrator.entrypoints.cli_tools.validate_runner",
                "--command", "echo 'stderr message' >&2"
            ],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR": str(output_dir),
            },
        )

        output_file = output_dir / "validation-output.log"
        assert output_file.exists()
        assert "stderr message" in output_file.read_text()

    def test_fails_if_no_command_configured(self, fake_git_repo: Path, tmp_path: Path):
        """Test that it fails with clear error if no command is provided."""
        output_dir = tmp_path / "session-output"
        output_dir.mkdir()

        result = subprocess.run(
            [
                sys.executable, "-m",
                "issue_orchestrator.entrypoints.cli_tools.validate_runner",
            ],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR": str(output_dir),
            },
        )

        assert result.returncode == 2
        assert "No validation command configured" in result.stderr

    def test_streams_output_to_terminal(self, fake_git_repo: Path, tmp_path: Path):
        """Test that output is streamed to terminal while also being captured."""
        output_dir = tmp_path / "session-output"
        output_dir.mkdir()

        result = subprocess.run(
            [
                sys.executable, "-m",
                "issue_orchestrator.entrypoints.cli_tools.validate_runner",
                "--command", "echo 'visible output'"
            ],
            cwd=fake_git_repo,
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "ISSUE_ORCHESTRATOR_VALIDATION_OUTPUT_DIR": str(output_dir),
            },
        )

        # Output should appear in terminal (stdout)
        assert "visible output" in result.stdout

        # And also be captured to file
        output_file = output_dir / "validation-output.log"
        assert "visible output" in output_file.read_text()
