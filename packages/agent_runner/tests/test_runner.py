"""Tests for AgentRunner."""

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
    """Tests for AgentRunner.run().

    AgentRunner inherits stdout/stderr from the parent process (for PTY
    passthrough). It does NOT capture output — that's the terminal plugin's
    job. These tests verify exit codes, timeouts, and process management.
    """

    def test_run_success_returns_zero_exit_code(
        self, temp_working_dir: Path, temp_output_dir: Path
    ) -> None:
        """Test that successful command returns exit code 0."""
        runner = AgentRunner()
        spec = RunSpec(
            command=[sys.executable, "-c", "pass"],
            working_dir=temp_working_dir,
            timeout_seconds=30,
            output_dir=temp_output_dir,
        )

        result = runner.run(spec)

        assert result.exit_code == 0
        assert not result.timed_out
        assert result.succeeded

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

    def test_run_creates_output_dir_if_missing(
        self, temp_working_dir: Path, tmp_path: Path
    ) -> None:
        """Test that output_dir is created if it doesn't exist."""
        runner = AgentRunner()
        output_dir = tmp_path / "new" / "nested" / "output"
        spec = RunSpec(
            command=[sys.executable, "-c", "pass"],
            working_dir=temp_working_dir,
            timeout_seconds=30,
            output_dir=output_dir,
        )

        result = runner.run(spec)

        assert result.exit_code == 0
        assert output_dir.exists()

    def test_run_uses_correct_working_directory(
        self, tmp_path: Path, temp_output_dir: Path
    ) -> None:
        """Test that command runs in the specified working directory.

        We verify by writing a marker file to cwd and checking it exists
        in the expected location (since stdout is not captured).
        """
        working_dir = tmp_path / "specific-workdir"
        working_dir.mkdir()

        runner = AgentRunner()
        spec = RunSpec(
            command=[
                sys.executable, "-c",
                "from pathlib import Path; Path('marker.txt').write_text('ok')",
            ],
            working_dir=working_dir,
            timeout_seconds=30,
            output_dir=temp_output_dir,
        )

        result = runner.run(spec)

        assert result.exit_code == 0
        assert (working_dir / "marker.txt").read_text() == "ok"

    def test_run_duration_tracked(
        self, temp_working_dir: Path, temp_output_dir: Path
    ) -> None:
        """Test that duration_seconds is populated (non-negative)."""
        runner = AgentRunner()
        spec = RunSpec(
            command=[sys.executable, "-c", "pass"],
            working_dir=temp_working_dir,
            timeout_seconds=30,
            output_dir=temp_output_dir,
        )

        result = runner.run(spec)

        assert result.exit_code == 0
        assert result.duration_seconds >= 0


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
