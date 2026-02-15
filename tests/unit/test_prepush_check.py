"""Unit tests for the prepush_check module."""

import pytest
import tempfile
import subprocess
from pathlib import Path

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
        cmd, timeout, dirty_check = load_validation_cmd(temp_worktree)
        assert cmd is None
        assert timeout == 0
        assert dirty_check == "tracked"

    def test_returns_none_when_no_cmd(self, temp_worktree):
        """Test returns None when cmd not configured."""
        config_dir = temp_worktree / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("some_key: value\n")

        cmd, timeout, dirty_check = load_validation_cmd(temp_worktree)
        assert cmd is None
        assert dirty_check == "tracked"

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

        cmd, timeout, dirty_check = load_validation_cmd(temp_worktree)
        assert cmd == "pytest"
        assert timeout == 300
        assert dirty_check == "tracked"

    def test_uses_default_timeout(self, temp_worktree):
        """Test uses default timeout when not specified."""
        config_dir = temp_worktree / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  cmd: "make test"
""")

        cmd, timeout, dirty_check = load_validation_cmd(temp_worktree)
        assert cmd == "make test"
        assert timeout == 300  # Default
        assert dirty_check == "tracked"

    def test_reads_dirty_check_mode(self, temp_worktree):
        """Test reads pre_push_dirty_check from config."""
        config_dir = temp_worktree / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True, exist_ok=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  cmd: "make test"
  pre_push_dirty_check: "unstaged"
""")

        cmd, timeout, dirty_check = load_validation_cmd(temp_worktree)
        assert cmd == "make test"
        assert timeout == 300
        assert dirty_check == "unstaged"


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

    def test_blocks_when_tracked_dirty(self, temp_worktree):
        """Test blocks push when tracked files are dirty."""
        import os

        config_dir = temp_worktree / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  cmd: "echo 'ok'"
  pre_push_dirty_check: "tracked"
""")

        # Modify tracked file without committing
        (temp_worktree / "README.md").write_text("dirty")

        orig_cwd = os.getcwd()
        try:
            os.chdir(temp_worktree)
            result = run_prepush_check(verbose=False)
            assert result == 1
        finally:
            os.chdir(orig_cwd)

    def test_allows_when_dirty_check_off(self, temp_worktree):
        """Test allows push when dirty check is disabled."""
        import os

        config_dir = temp_worktree / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  cmd: "echo 'ok'"
  pre_push_dirty_check: "off"
""")

        (temp_worktree / "README.md").write_text("dirty")

        orig_cwd = os.getcwd()
        try:
            os.chdir(temp_worktree)
            result = run_prepush_check(verbose=False)
            assert result == 0
        finally:
            os.chdir(orig_cwd)

    def test_allows_staged_when_unstaged_mode(self, temp_worktree):
        """Test unstaged mode allows staged changes."""
        import os
        import subprocess

        config_dir = temp_worktree / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  cmd: "echo 'ok'"
  pre_push_dirty_check: "unstaged"
""")

        (temp_worktree / "README.md").write_text("dirty")
        subprocess.run(["git", "add", "README.md"], cwd=temp_worktree, check=True, capture_output=True)

        orig_cwd = os.getcwd()
        try:
            os.chdir(temp_worktree)
            result = run_prepush_check(verbose=False)
            assert result == 0
        finally:
            os.chdir(orig_cwd)

    def test_blocks_when_all_mode_with_untracked_files(self, temp_worktree):
        """Mode 'all' blocks when untracked files are present."""
        import os

        config_dir = temp_worktree / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  cmd: "echo 'ok'"
  pre_push_dirty_check: "all"
""")

        (temp_worktree / "new_untracked.txt").write_text("new file")

        orig_cwd = os.getcwd()
        try:
            os.chdir(temp_worktree)
            result = run_prepush_check(verbose=False)
            assert result == 1
        finally:
            os.chdir(orig_cwd)

    def test_rejects_invalid_dirty_mode(self, temp_worktree, capsys):
        """Test invalid dirty mode exits 1 and reports error when verbose."""
        import os

        config_dir = temp_worktree / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  cmd: "echo 'ok'"
  pre_push_dirty_check: "bogus"
""")

        orig_cwd = os.getcwd()
        try:
            os.chdir(temp_worktree)
            result = run_prepush_check(verbose=True)
            captured = capsys.readouterr()
            assert result == 1
            assert "Invalid validation.pre_push_dirty_check value" in captured.out
        finally:
            os.chdir(orig_cwd)

    def test_dirty_only_skips_validation_command(self, temp_worktree):
        """Dirty-only mode should not execute validation command."""
        import os

        config_dir = temp_worktree / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        marker_file = temp_worktree / "validation_ran"
        config_path = config_dir / "default.yaml"
        config_path.write_text(f"""
validation:
  cmd: "touch {marker_file} && exit 1"
  pre_push_dirty_check: "tracked"
""")

        orig_cwd = os.getcwd()
        try:
            os.chdir(temp_worktree)
            result = run_prepush_check(verbose=False, dirty_only=True)
            assert result == 0
            assert not marker_file.exists()
        finally:
            os.chdir(orig_cwd)

    def test_dirty_only_still_blocks_when_dirty(self, temp_worktree):
        """Dirty-only mode must still enforce dirty-tree policy."""
        import os

        config_dir = temp_worktree / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        config_path = config_dir / "default.yaml"
        config_path.write_text("""
validation:
  cmd: "echo 'ok'"
  pre_push_dirty_check: "tracked"
""")

        (temp_worktree / "README.md").write_text("dirty")

        orig_cwd = os.getcwd()
        try:
            os.chdir(temp_worktree)
            result = run_prepush_check(verbose=False, dirty_only=True)
            assert result == 1
        finally:
            os.chdir(orig_cwd)
