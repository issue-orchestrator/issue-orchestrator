"""Tests for AgentRunner."""

import subprocess
import sys
from pathlib import Path

import pytest

from agent_runner import AgentRunner, RunSpec


@pytest.fixture
def temp_output_dir(tmp_path: Path) -> Path:
    """Create a temporary output directory."""
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    return output_dir


@pytest.fixture
def temp_working_dir(tmp_path: Path) -> Path:
    """Create a temporary working directory."""
    working_dir = tmp_path / "work"
    working_dir.mkdir()
    return working_dir


class TestAgentRunner:
    """Tests for AgentRunner.run()."""

    def test_run_success_captures_output(
        self, temp_working_dir: Path, temp_output_dir: Path
    ) -> None:
        """Test that successful command captures stdout/stderr."""
        runner = AgentRunner()
        spec = RunSpec(
            command=[sys.executable, "-c", "print('hello'); import sys; print('error', file=sys.stderr)"],
            working_dir=temp_working_dir,
            timeout_seconds=30,
            output_dir=temp_output_dir,
        )

        result = runner.run(spec)

        assert result.exit_code == 0
        assert not result.timed_out
        assert result.succeeded
        assert "hello" in result.stdout
        assert "error" in result.stderr
        assert result.stdout_path.exists()
        assert result.stderr_path.exists()

    def test_run_failure_returns_exit_code(
        self, temp_working_dir: Path, temp_output_dir: Path
    ) -> None:
        """Test that failed command returns correct exit code."""
        runner = AgentRunner()
        spec = RunSpec(
            command=[sys.executable, "-c", "import sys; sys.exit(42)"],
            working_dir=temp_working_dir,
            timeout_seconds=30,
            output_dir=temp_output_dir,
        )

        result = runner.run(spec)

        assert result.exit_code == 42
        assert not result.timed_out
        assert result.failed
        assert not result.succeeded

    def test_run_timeout_terminates_process(
        self, temp_working_dir: Path, temp_output_dir: Path
    ) -> None:
        """Test that timeout terminates the process."""
        runner = AgentRunner()
        spec = RunSpec(
            command=[sys.executable, "-c", "import time; time.sleep(60)"],
            working_dir=temp_working_dir,
            timeout_seconds=1,
            output_dir=temp_output_dir,
        )

        result = runner.run(spec)

        assert result.timed_out
        assert result.exit_code is None
        assert not result.succeeded
        assert result.duration_seconds >= 1
        assert result.duration_seconds < 10  # Should terminate quickly

    def test_run_command_not_found(
        self, temp_working_dir: Path, temp_output_dir: Path
    ) -> None:
        """Test handling of non-existent command."""
        runner = AgentRunner()
        spec = RunSpec(
            command=["nonexistent-command-12345"],
            working_dir=temp_working_dir,
            timeout_seconds=30,
            output_dir=temp_output_dir,
        )

        result = runner.run(spec)

        assert result.exit_code == 127  # Command not found
        assert not result.timed_out
        assert "not found" in result.stderr.lower()

    def test_run_output_written_to_files(
        self, temp_working_dir: Path, temp_output_dir: Path
    ) -> None:
        """Test that output is written to files in output_dir."""
        runner = AgentRunner()
        spec = RunSpec(
            command=[sys.executable, "-c", "print('stdout-content'); import sys; print('stderr-content', file=sys.stderr)"],
            working_dir=temp_working_dir,
            timeout_seconds=30,
            output_dir=temp_output_dir,
        )

        result = runner.run(spec)

        assert result.stdout_path == temp_output_dir / "stdout.log"
        assert result.stderr_path == temp_output_dir / "stderr.log"
        assert result.stdout_path.read_text().strip() == "stdout-content"
        assert result.stderr_path.read_text().strip() == "stderr-content"

    def test_run_creates_output_dir_if_missing(
        self, temp_working_dir: Path, tmp_path: Path
    ) -> None:
        """Test that output_dir is created if it doesn't exist."""
        runner = AgentRunner()
        output_dir = tmp_path / "new" / "nested" / "output"
        spec = RunSpec(
            command=[sys.executable, "-c", "print('hello')"],
            working_dir=temp_working_dir,
            timeout_seconds=30,
            output_dir=output_dir,
        )

        result = runner.run(spec)

        assert result.exit_code == 0
        assert output_dir.exists()
        assert result.stdout_path.exists()

    def test_run_uses_correct_working_directory(
        self, tmp_path: Path, temp_output_dir: Path
    ) -> None:
        """Test that command runs in the specified working directory."""
        working_dir = tmp_path / "specific-workdir"
        working_dir.mkdir()

        runner = AgentRunner()
        spec = RunSpec(
            command=[sys.executable, "-c", "import os; print(os.getcwd())"],
            working_dir=working_dir,
            timeout_seconds=30,
            output_dir=temp_output_dir,
        )

        result = runner.run(spec)

        assert result.exit_code == 0
        assert str(working_dir) in result.stdout

    def test_run_duration_tracked(
        self, temp_working_dir: Path, temp_output_dir: Path
    ) -> None:
        """Test that duration is tracked correctly."""
        runner = AgentRunner()
        spec = RunSpec(
            command=[sys.executable, "-c", "import time; time.sleep(0.5); print('done')"],
            working_dir=temp_working_dir,
            timeout_seconds=30,
            output_dir=temp_output_dir,
        )

        result = runner.run(spec)

        assert result.exit_code == 0
        assert result.duration_seconds >= 0.5
        assert result.duration_seconds < 5  # Reasonable upper bound


class TestRunSpec:
    """Tests for RunSpec validation."""

    def test_empty_command_raises(self, tmp_path: Path) -> None:
        """Test that empty command raises ValueError."""
        with pytest.raises(ValueError, match="command cannot be empty"):
            RunSpec(
                command=[],
                working_dir=tmp_path,
                timeout_seconds=30,
                output_dir=tmp_path,
            )

    def test_zero_timeout_raises(self, tmp_path: Path) -> None:
        """Test that zero timeout raises ValueError."""
        with pytest.raises(ValueError, match="timeout_seconds must be positive"):
            RunSpec(
                command=["echo"],
                working_dir=tmp_path,
                timeout_seconds=0,
                output_dir=tmp_path,
            )

    def test_negative_timeout_raises(self, tmp_path: Path) -> None:
        """Test that negative timeout raises ValueError."""
        with pytest.raises(ValueError, match="timeout_seconds must be positive"):
            RunSpec(
                command=["echo"],
                working_dir=tmp_path,
                timeout_seconds=-1,
                output_dir=tmp_path,
            )
