"""Tests for the trace-issue functionality (CLI command and shell script)."""

import argparse
import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from issue_orchestrator.entrypoints.cli import cmd_trace


@pytest.fixture
def trace_issue_script() -> Path:
    """Path to the trace-issue script."""
    return Path(__file__).parent.parent.parent / "tools" / "trace-issue"


@pytest.fixture
def mock_log_file(tmp_path: Path) -> Path:
    """Create a mock orchestrator log file."""
    log_dir = tmp_path / ".issue-orchestrator" / "state" / "logs"
    log_dir.mkdir(parents=True)
    log_file = log_dir / "orchestrator.log"
    log_file.write_text(
        """\
2026-01-09 10:00:00 [INFO] Starting orchestrator on port 8080
2026-01-09 10:00:01 [INFO] [issue-123] Session starting
2026-01-09 10:00:02 [INFO] [issue-456] Session starting
2026-01-09 10:00:03 [INFO] [issue-123] Session completed
2026-01-09 10:00:04 [INFO] issue=123 something happened
2026-01-09 10:00:05 [INFO] issue_number=456 another event
2026-01-09 10:00:06 [INFO] issue #123 final event
2026-01-09 11:00:00 [INFO] Starting orchestrator on port 8080
2026-01-09 11:00:01 [INFO] [issue-789] Session starting
2026-01-09 11:00:02 [INFO] [issue-123] New run session
"""
    )
    return tmp_path


class TestTraceIssue:
    """Tests for trace-issue script."""

    def test_script_exists_and_is_executable(self, trace_issue_script: Path):
        """Script exists and has execute permission."""
        assert trace_issue_script.exists()
        assert trace_issue_script.stat().st_mode & 0o111  # Has execute bit

    def test_no_args_shows_usage(self, trace_issue_script: Path):
        """Running without args shows usage."""
        result = subprocess.run(
            [str(trace_issue_script)],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 1
        assert "Usage:" in result.stderr

    def test_traces_issue_from_last_startup(
        self, trace_issue_script: Path, mock_log_file: Path
    ):
        """Only shows entries from the last startup for the specified issue."""
        result = subprocess.run(
            [str(trace_issue_script), "123"],
            capture_output=True,
            text=True,
            cwd=mock_log_file,
        )
        assert result.returncode == 0
        # Should only have the entry from after the second startup
        lines = result.stdout.strip().split("\n")
        assert len(lines) == 1
        assert "[issue-123] New run session" in lines[0]
        # Should NOT have entries from the first run
        assert "Session starting" not in result.stdout
        assert "Session completed" not in result.stdout

    def test_traces_different_issue(
        self, trace_issue_script: Path, mock_log_file: Path
    ):
        """Traces a different issue number."""
        result = subprocess.run(
            [str(trace_issue_script), "789"],
            capture_output=True,
            text=True,
            cwd=mock_log_file,
        )
        assert result.returncode == 0
        lines = result.stdout.strip().split("\n")
        assert len(lines) == 1
        assert "[issue-789] Session starting" in lines[0]

    def test_matches_multiple_patterns(
        self, trace_issue_script: Path, tmp_path: Path
    ):
        """Matches [issue-N], issue=N, issue_number=N, and issue #N patterns."""
        log_dir = tmp_path / ".issue-orchestrator" / "state" / "logs"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "orchestrator.log"
        log_file.write_text(
            """\
2026-01-09 10:00:00 [INFO] Starting orchestrator on port 8080
2026-01-09 10:00:01 [INFO] [issue-42] bracket format
2026-01-09 10:00:02 [INFO] issue=42 equals format
2026-01-09 10:00:03 [INFO] issue_number=42 underscore format
2026-01-09 10:00:04 [INFO] issue #42 hash format
2026-01-09 10:00:05 [INFO] issue=421 should not match
"""
        )

        result = subprocess.run(
            [str(trace_issue_script), "42"],
            capture_output=True,
            text=True,
            cwd=tmp_path,
        )
        assert result.returncode == 0
        lines = result.stdout.strip().split("\n")
        assert len(lines) == 4
        assert "bracket format" in result.stdout
        assert "equals format" in result.stdout
        assert "underscore format" in result.stdout
        assert "hash format" in result.stdout
        # Should NOT match 421
        assert "should not match" not in result.stdout

    def test_no_log_file_shows_error(
        self, trace_issue_script: Path, tmp_path: Path
    ):
        """Shows error when log file doesn't exist."""
        result = subprocess.run(
            [str(trace_issue_script), "123"],
            capture_output=True,
            text=True,
            cwd=tmp_path,
        )
        assert result.returncode == 1
        assert "Error: orchestrator.log not found" in result.stderr

    def test_no_startup_marker_shows_warning(
        self, trace_issue_script: Path, tmp_path: Path
    ):
        """Shows warning when no startup marker found."""
        log_dir = tmp_path / ".issue-orchestrator" / "state" / "logs"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "orchestrator.log"
        log_file.write_text(
            """\
2026-01-09 10:00:01 [INFO] [issue-123] Session starting
2026-01-09 10:00:02 [INFO] [issue-123] Session completed
"""
        )

        result = subprocess.run(
            [str(trace_issue_script), "123"],
            capture_output=True,
            text=True,
            cwd=tmp_path,
        )
        assert result.returncode == 0
        assert "Warning: No startup marker found" in result.stderr
        # Should still show all entries
        lines = result.stdout.strip().split("\n")
        assert len(lines) == 2


class TestCmdTrace:
    """Tests for the cmd_trace CLI command."""

    def test_traces_issue_entries(self, tmp_path: Path, capsys):
        """CLI command traces issue entries from log file."""
        log_dir = tmp_path / ".issue-orchestrator" / "state" / "logs"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "orchestrator.log"
        log_file.write_text(
            """\
2026-01-09 10:00:00 [INFO] Starting orchestrator on port 8080
2026-01-09 10:00:01 [INFO] [issue-456] Session starting
2026-01-09 10:00:02 [INFO] [issue-456] Session completed
"""
        )

        args = argparse.Namespace(issue_number=456)

        with patch("issue_orchestrator.entrypoints.cli.Path.cwd", return_value=tmp_path):
            with patch("subprocess.run") as mock_run:
                # Make git rev-parse fail so it uses cwd
                mock_run.side_effect = subprocess.CalledProcessError(1, "git")
                result = cmd_trace(args)

        assert result == 0
        captured = capsys.readouterr()
        assert "[issue-456] Session starting" in captured.out
        assert "[issue-456] Session completed" in captured.out

    def test_no_entries_found(self, tmp_path: Path, capsys):
        """CLI command shows message when no entries found."""
        log_dir = tmp_path / ".issue-orchestrator" / "state" / "logs"
        log_dir.mkdir(parents=True)
        log_file = log_dir / "orchestrator.log"
        log_file.write_text(
            """\
2026-01-09 10:00:00 [INFO] Starting orchestrator on port 8080
2026-01-09 10:00:01 [INFO] [issue-123] Session starting
"""
        )

        args = argparse.Namespace(issue_number=999)

        with patch("issue_orchestrator.entrypoints.cli.Path.cwd", return_value=tmp_path):
            with patch("subprocess.run") as mock_run:
                mock_run.side_effect = subprocess.CalledProcessError(1, "git")
                result = cmd_trace(args)

        assert result == 0
        captured = capsys.readouterr()
        assert "No log entries found for issue #999" in captured.out
