"""Unit tests for FileSystemSessionOutput adapter.

Tests for list_runs() and related functionality.
"""

import json

import pytest

from issue_orchestrator.execution.session_output_adapter import (
    FileSystemSessionOutput,
    INDEX_NAME,
    MANIFEST_NAME,
)


class TestListRuns:
    """Tests for the list_runs method."""

    @pytest.fixture
    def session_output(self):
        """Create a FileSystemSessionOutput instance."""
        return FileSystemSessionOutput()

    @pytest.fixture
    def worktree_with_runs(self, tmp_path):
        """Create a worktree with multiple run directories."""
        sessions_dir = tmp_path / ".issue-orchestrator" / "sessions"
        sessions_dir.mkdir(parents=True)

        # Create run directories
        run1_dir = sessions_dir / "20260117-100000Z__coding-1"
        run1_dir.mkdir()
        run2_dir = sessions_dir / "20260117-110000Z__review-1"
        run2_dir.mkdir()
        run3_dir = sessions_dir / "20260117-120000Z__coding-2"
        run3_dir.mkdir()

        # Create manifests
        (run1_dir / MANIFEST_NAME).write_text(json.dumps({
            "session_name": "coding-1",
            "run_id": "20260117-100000Z",
            "started_at": "2026-01-17T10:00:00Z",
            "ended_at": "2026-01-17T10:30:00Z",
            "issue_number": 123,
            "agent_label": "agent:developer",
            "outcome": "completed",
            "validation_passed": True,
        }))
        (run2_dir / MANIFEST_NAME).write_text(json.dumps({
            "session_name": "review-1",
            "run_id": "20260117-110000Z",
            "started_at": "2026-01-17T11:00:00Z",
            "ended_at": "2026-01-17T11:30:00Z",
            "issue_number": 123,
            "agent_label": "agent:reviewer",
            "outcome": "blocked",
        }))
        (run3_dir / MANIFEST_NAME).write_text(json.dumps({
            "session_name": "coding-2",
            "run_id": "20260117-120000Z",
            "started_at": "2026-01-17T12:00:00Z",
            "issue_number": 123,
            "agent_label": "agent:developer",
            # No ended_at - still in progress
        }))

        # Create index
        index = {
            "runs": [
                {
                    "session_name": "coding-1",
                    "run_id": "20260117-100000Z",
                    "started_at": "2026-01-17T10:00:00Z",
                    "issue_number": 123,
                    "run_dir": str(run1_dir),
                    "agent_label": "agent:developer",
                },
                {
                    "session_name": "review-1",
                    "run_id": "20260117-110000Z",
                    "started_at": "2026-01-17T11:00:00Z",
                    "issue_number": 123,
                    "run_dir": str(run2_dir),
                    "agent_label": "agent:reviewer",
                },
                {
                    "session_name": "coding-2",
                    "run_id": "20260117-120000Z",
                    "started_at": "2026-01-17T12:00:00Z",
                    "issue_number": 123,
                    "run_dir": str(run3_dir),
                    "agent_label": "agent:developer",
                },
            ]
        }
        (sessions_dir / INDEX_NAME).write_text(json.dumps(index))

        return tmp_path

    def test_list_runs_returns_all_runs_sorted_by_time(
        self, session_output, worktree_with_runs
    ):
        """Verify list_runs returns runs sorted by started_at."""
        runs = session_output.list_runs(worktree_with_runs)

        assert len(runs) == 3
        assert runs[0]["session_name"] == "coding-1"
        assert runs[1]["session_name"] == "review-1"
        assert runs[2]["session_name"] == "coding-2"

    def test_list_runs_includes_status_from_manifest(
        self, session_output, worktree_with_runs
    ):
        """Verify list_runs derives status from manifest."""
        runs = session_output.list_runs(worktree_with_runs)

        # First run completed with validation passed
        assert runs[0]["status"] == "completed"
        assert runs[0]["validation_passed"] is True

        # Second run was blocked
        assert runs[1]["status"] == "blocked"

        # Third run is in progress (no ended_at)
        assert runs[2]["status"] == "in_progress"

    def test_list_runs_includes_outcome_and_ended_at(
        self, session_output, worktree_with_runs
    ):
        """Verify list_runs includes outcome and ended_at from manifest."""
        runs = session_output.list_runs(worktree_with_runs)

        assert runs[0]["outcome"] == "completed"
        assert runs[0]["ended_at"] == "2026-01-17T10:30:00Z"

        assert runs[1]["outcome"] == "blocked"
        assert runs[1]["ended_at"] == "2026-01-17T11:30:00Z"

        assert runs[2].get("outcome") is None
        assert runs[2].get("ended_at") is None

    def test_list_runs_returns_empty_for_missing_index(
        self, session_output, tmp_path
    ):
        """Verify list_runs returns empty list when no index exists."""
        runs = session_output.list_runs(tmp_path)
        assert runs == []

    def test_list_runs_returns_empty_for_empty_index(
        self, session_output, tmp_path
    ):
        """Verify list_runs returns empty list when index has no runs."""
        sessions_dir = tmp_path / ".issue-orchestrator" / "sessions"
        sessions_dir.mkdir(parents=True)
        (sessions_dir / INDEX_NAME).write_text(json.dumps({"runs": []}))

        runs = session_output.list_runs(tmp_path)
        assert runs == []

    def test_list_runs_skips_missing_run_dirs(self, session_output, tmp_path):
        """Verify list_runs skips entries where run_dir doesn't exist."""
        sessions_dir = tmp_path / ".issue-orchestrator" / "sessions"
        sessions_dir.mkdir(parents=True)

        # Create one real run
        run1_dir = sessions_dir / "20260117-100000Z__coding-1"
        run1_dir.mkdir()
        (run1_dir / MANIFEST_NAME).write_text(json.dumps({
            "session_name": "coding-1",
            "started_at": "2026-01-17T10:00:00Z",
            "ended_at": "2026-01-17T10:30:00Z",
            "outcome": "completed",
        }))

        # Index references a non-existent run
        index = {
            "runs": [
                {
                    "session_name": "coding-1",
                    "run_id": "20260117-100000Z",
                    "started_at": "2026-01-17T10:00:00Z",
                    "run_dir": str(run1_dir),
                },
                {
                    "session_name": "review-1",
                    "run_id": "20260117-110000Z",
                    "started_at": "2026-01-17T11:00:00Z",
                    "run_dir": str(sessions_dir / "nonexistent"),
                },
            ]
        }
        (sessions_dir / INDEX_NAME).write_text(json.dumps(index))

        runs = session_output.list_runs(tmp_path)
        assert len(runs) == 1
        assert runs[0]["session_name"] == "coding-1"
