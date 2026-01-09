"""Unit tests for the prepush_check module."""

import pytest
import tempfile
import subprocess
from pathlib import Path
from unittest.mock import patch

from issue_orchestrator.entrypoints.cli_tools.prepush_check import (
    load_validation_cmd,
    run_prepush_check,
)


class TestLoadValidationCmd:
    """Tests for loading validation configuration."""

    @pytest.fixture
    def temp_worktree(self):
        """Create a temporary worktree directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            worktree = Path(tmpdir)
            config_dir = worktree / ".issue-orchestrator"
            config_dir.mkdir()
            yield worktree

    def test_returns_none_when_no_config(self, temp_worktree):
        """Test returns None when config file doesn't exist."""
        cmd, timeout = load_validation_cmd(temp_worktree)
        assert cmd is None
        assert timeout == 0

    def test_returns_none_when_no_cmd(self, temp_worktree):
        """Test returns None when cmd not configured."""
        config_dir = temp_worktree / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("some_key: value\n")

        cmd, timeout = load_validation_cmd(temp_worktree)
        assert cmd is None

    def test_returns_cmd_when_configured(self, temp_worktree):
        """Test returns command when configured."""
        config_dir = temp_worktree / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  cmd: "pytest"
  timeout_seconds: 300
""")

        cmd, timeout = load_validation_cmd(temp_worktree)
        assert cmd == "pytest"
        assert timeout == 300

    def test_uses_default_timeout(self, temp_worktree):
        """Test uses default timeout when not specified."""
        config_dir = temp_worktree / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  cmd: "make test"
""")

        cmd, timeout = load_validation_cmd(temp_worktree)
        assert cmd == "make test"
        assert timeout == 300  # Default


class TestRunPrepushCheck:
    """Tests for run_prepush_check function."""

    @pytest.fixture
    def temp_worktree(self):
        """Create a temporary worktree with git repo."""
        with tempfile.TemporaryDirectory() as tmpdir:
            worktree = Path(tmpdir)
            # Initialize git repo
            subprocess.run(
                ["git", "init"],
                cwd=worktree,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.email", "test@test.com"],
                cwd=worktree,
                capture_output=True,
            )
            subprocess.run(
                ["git", "config", "user.name", "Test"],
                cwd=worktree,
                capture_output=True,
            )
            (worktree / "README.md").write_text("test")
            subprocess.run(
                ["git", "add", "."],
                cwd=worktree,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", "Initial"],
                cwd=worktree,
                capture_output=True,
            )
            yield worktree

    def test_returns_0_when_no_config(self, temp_worktree):
        """Test returns 0 (pass) when no config exists."""
        import os
        orig_cwd = os.getcwd()
        try:
            os.chdir(temp_worktree)
            result = run_prepush_check(verbose=False)
            assert result == 0
        finally:
            os.chdir(orig_cwd)

    def test_returns_0_when_validation_passes(self, temp_worktree):
        """Test returns 0 when validation passes."""
        import os

        # Create config with passing command
        config_dir = temp_worktree / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  cmd: "echo 'ok'"
  timeout_seconds: 10
""")

        orig_cwd = os.getcwd()
        try:
            os.chdir(temp_worktree)
            result = run_prepush_check(verbose=False)
            assert result == 0
        finally:
            os.chdir(orig_cwd)

    def test_returns_1_when_validation_fails(self, temp_worktree):
        """Test returns 1 when validation fails."""
        import os

        # Create config with failing command
        config_dir = temp_worktree / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  cmd: "exit 1"
  timeout_seconds: 10
""")

        orig_cwd = os.getcwd()
        try:
            os.chdir(temp_worktree)
            result = run_prepush_check(verbose=False)
            assert result == 1
        finally:
            os.chdir(orig_cwd)

    def test_uses_cache_on_second_run(self, temp_worktree):
        """Test uses cache on second run."""
        import os

        # Create config with command that creates a file (to track runs)
        config_dir = temp_worktree / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        marker_file = temp_worktree / "validation_ran"
        config_path = config_dir / "default.yaml"
        config_path.write_text(f"""
validation:
  cmd: "touch {marker_file} && echo 'ok'"
  timeout_seconds: 10
""")

        orig_cwd = os.getcwd()
        try:
            os.chdir(temp_worktree)

            # First run creates the marker
            result1 = run_prepush_check(verbose=False)
            assert result1 == 0
            assert marker_file.exists()

            # Delete marker to verify second run uses cache
            marker_file.unlink()

            # Second run should use cache (not recreate marker)
            result2 = run_prepush_check(verbose=False)
            assert result2 == 0
            assert not marker_file.exists()  # Validation didn't run again
        finally:
            os.chdir(orig_cwd)
