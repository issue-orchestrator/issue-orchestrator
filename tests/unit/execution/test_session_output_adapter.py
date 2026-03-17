"""Unit tests for FileSystemSessionOutput adapter.

Tests for list_runs() and related functionality.
"""

import base64
import json

import pytest

from issue_orchestrator.execution.session_output_adapter import (
    FileSystemSessionOutput,
    INDEX_NAME,
    MANIFEST_NAME,
    REVIEW_EXCHANGE_DIR_NAME,
    REVIEW_EXCHANGE_SUMMARY_NAME,
    VALIDATION_RECORD_NAME,
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

    def test_list_runs_derives_validation_failed_status(
        self, session_output, tmp_path
    ):
        """Verify validation_passed=False leads to validation_failed status.

        When the manifest has validation_passed=False, _derive_run_status
        should return 'validation_failed' even if outcome is 'completed'.
        This ensures the UI shows the correct status for failed validation.
        """
        sessions_dir = tmp_path / ".issue-orchestrator" / "sessions"
        sessions_dir.mkdir(parents=True)

        # Create run with validation_passed=False (validation failed case)
        run_dir = sessions_dir / "20260117-100000Z__coding-1"
        run_dir.mkdir()
        (run_dir / MANIFEST_NAME).write_text(json.dumps({
            "session_name": "coding-1",
            "run_id": "20260117-100000Z",
            "started_at": "2026-01-17T10:00:00Z",
            "ended_at": "2026-01-17T10:30:00Z",
            "issue_number": 123,
            "agent_label": "agent:developer",
            "outcome": "completed",  # Agent reported completed
            "validation_passed": False,  # But validation failed
            "validation_failure_reason": "make validate failed",
        }))

        # Create index
        index = {
            "runs": [{
                "session_name": "coding-1",
                "run_id": "20260117-100000Z",
                "started_at": "2026-01-17T10:00:00Z",
                "issue_number": 123,
                "run_dir": str(run_dir),
                "agent_label": "agent:developer",
            }]
        }
        (sessions_dir / INDEX_NAME).write_text(json.dumps(index))

        runs = session_output.list_runs(tmp_path)

        assert len(runs) == 1
        # Status should be validation_failed, not completed
        assert runs[0]["status"] == "validation_failed"
        assert runs[0]["outcome"] == "completed"
        assert runs[0]["validation_passed"] is False


class TestReviewExchangeSummary:
    """Tests for review exchange summary persistence."""

    @pytest.fixture
    def session_output(self):
        """Create a FileSystemSessionOutput instance."""
        return FileSystemSessionOutput()

    def test_store_review_exchange_summary_writes_manifest_and_validation(
        self, session_output, tmp_path
    ):
        worktree = tmp_path
        session_name = "issue-1"
        summary = {"status": "ok", "completed_rounds": 2}
        validation_source = tmp_path / "validation-source.json"
        validation_source.write_text(json.dumps({"passed": True}))

        stored = session_output.store_review_exchange_summary(
            worktree,
            session_name,
            summary,
            validation_record_path=validation_source,
        )

        run_dir = session_output.find_run_dir(worktree, session_name=session_name)
        assert run_dir is not None
        summary_path = run_dir / REVIEW_EXCHANGE_DIR_NAME / REVIEW_EXCHANGE_SUMMARY_NAME
        assert summary_path.exists()
        assert json.loads(summary_path.read_text()) == summary
        assert stored.summary == summary
        assert stored.summary_path == summary_path
        assert stored.exchange_dir == summary_path.parent
        assert stored.validation_record_path == run_dir / VALIDATION_RECORD_NAME
        assert (run_dir / VALIDATION_RECORD_NAME).exists()

        manifest = session_output.read_manifest(run_dir)
        assert manifest is not None
        assert manifest.get("review_exchange_dir") == str(summary_path.parent)

    def test_load_review_exchange_summary_returns_none_when_missing(
        self, session_output, tmp_path
    ):
        assert (
            session_output.load_review_exchange_summary(tmp_path, "missing-session") is None
        )

    def test_load_review_exchange_summary_returns_none_for_invalid_json(
        self, session_output, tmp_path
    ):
        worktree = tmp_path
        session_name = "issue-2"
        run_dir = session_output.ensure_run_dir(worktree, session_name)
        exchange_dir = run_dir / REVIEW_EXCHANGE_DIR_NAME
        exchange_dir.mkdir(parents=True, exist_ok=True)
        (exchange_dir / REVIEW_EXCHANGE_SUMMARY_NAME).write_text("not-json")

        assert (
            session_output.load_review_exchange_summary(worktree, session_name) is None
        )

    def test_load_review_exchange_summary_falls_back_to_newest_run_with_summary(
        self, session_output, tmp_path
    ):
        import time

        worktree = tmp_path
        session_name = "issue-3"

        older_run = session_output.start_run(worktree, session_name, issue_number=3)
        older_exchange = older_run.run_dir / REVIEW_EXCHANGE_DIR_NAME
        older_exchange.mkdir(parents=True, exist_ok=True)
        (older_exchange / REVIEW_EXCHANGE_SUMMARY_NAME).write_text(
            json.dumps({"status": "error", "completed_rounds": 2, "response_text": "stale"})
        )
        session_output.update_manifest(
            older_run.run_dir,
            {"review_exchange_dir": str(older_exchange)},
        )

        # Ensure a distinct run_id directory for the latest run.
        time.sleep(1.1)
        newer_run = session_output.start_run(worktree, session_name, issue_number=3)
        # Newest run has no summary yet.
        assert newer_run.run_dir.exists()

        loaded = session_output.load_review_exchange_summary(worktree, session_name)
        assert loaded is not None
        assert loaded.summary["response_text"] == "stale"
        assert loaded.exchange_dir == older_exchange


class TestRunRetentionMetadata:
    """Tests for retention metadata persisted in run manifests."""

    def test_start_run_writes_retention_metadata(self, tmp_path):
        session_output = FileSystemSessionOutput()
        run = session_output.start_run(
            tmp_path,
            "issue-123",
            issue_number=123,
            retention_tier="cold",
            retention_days=14,
            retention_pinned=True,
        )

        manifest = json.loads((run.run_dir / MANIFEST_NAME).read_text())
        assert manifest["retention_tier"] == "cold"
        assert manifest["retention_days"] == 14
        assert manifest["retention_pinned"] is True
        assert "retention_expires_at" in manifest


class TestSessionLogCleaning:
    @staticmethod
    def _decoded_recording(path):
        chunks = []
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            event = json.loads(raw_line)
            if event.get("event_type") != "output":
                continue
            chunks.append(base64.b64decode(event["data_b64"]).decode("utf-8"))
        return "".join(chunks)

    def test_append_cleaned_session_log_filters_noise_at_write_time(self, tmp_path):
        session_output = FileSystemSessionOutput()
        run = session_output.start_run(tmp_path, "issue-123", issue_number=123)
        recording = run.run_dir / "terminal-recording.jsonl"

        session_output.append_cleaned_session_log(
            run.run_dir,
            "Line one\n\n✶ Thinking…\nLine two\nRecentactivity\n",
            header="[2026-03-12T12:00:00Z] round=1 role=reviewer section=prompt\n",
        )

        assert self._decoded_recording(recording) == (
            "[2026-03-12T12:00:00Z] round=1 role=reviewer section=prompt\n"
            "Line one\n"
            "Line two\n\n"
        )

    def test_append_cleaned_session_log_skips_orphan_header_when_body_is_only_noise(
        self, tmp_path
    ):
        session_output = FileSystemSessionOutput()
        run = session_output.start_run(tmp_path, "issue-123", issue_number=123)
        recording = run.run_dir / "terminal-recording.jsonl"

        session_output.append_cleaned_session_log(
            run.run_dir,
            "✶ Thinking…\nRecentactivity\n",
            header="[2026-03-12T12:00:00Z] round=1 role=reviewer section=prompt\n",
        )

        assert recording.read_text(encoding="utf-8") == ""

    def test_append_review_exchange_session_log_entry_preserves_header_outside_filter(
        self, tmp_path
    ):
        session_output = FileSystemSessionOutput()
        run = session_output.start_run(tmp_path, "issue-123", issue_number=123)
        recording = run.run_dir / "terminal-recording.jsonl"

        session_output.append_review_exchange_session_log_entry(
            run.run_dir,
            round_index=3,
            role="reviewer",
            section="feedback",
            content="Line one\n✶ Thinking…\nLine two\n",
        )

        content = self._decoded_recording(recording)
        assert "round=3 role=reviewer section=feedback" in content
        assert "Line one" in content
        assert "Line two" in content
        assert "Thinking" not in content


class TestClaudeReplayCapture:
    @staticmethod
    def _decoded_recording(path):
        chunks = []
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            if not raw_line.strip():
                continue
            event = json.loads(raw_line)
            if event.get("event_type") != "output":
                continue
            chunks.append(base64.b64decode(event["data_b64"]).decode("utf-8"))
        return "".join(chunks)

    def test_attach_claude_log_backfills_terminal_recording_when_empty(self, tmp_path):
        session_output = FileSystemSessionOutput()
        claude_dir = tmp_path / "claude-project"
        claude_dir.mkdir()
        run = session_output.start_run(
            tmp_path,
            "issue-123",
            issue_number=123,
            claude_log_dir=str(claude_dir),
        )
        claude_log = claude_dir / "session.jsonl"
        claude_log.write_text(
            "\n".join([
                json.dumps({
                    "type": "stream_event",
                    "event": {
                        "type": "content_block_delta",
                        "delta": {"type": "text_delta", "text": "Hello"},
                    },
                }),
                json.dumps({
                    "type": "assistant",
                    "message": {
                        "content": [
                            {"type": "text", "text": " world"},
                            {"type": "tool_use", "name": "Bash", "input": {"command": "pwd"}},
                        ],
                    },
                }),
            ]) + "\n",
            encoding="utf-8",
        )

        attached = session_output.attach_claude_log(run.run_dir)

        assert attached == claude_log
        decoded = self._decoded_recording(run.run_dir / "terminal-recording.jsonl")
        assert "Hello" in decoded
        assert " world" in decoded
        assert "Bash: pwd" in decoded

    def test_sync_claude_replay_capture_updates_manifest_and_recording(self, tmp_path):
        session_output = FileSystemSessionOutput()
        claude_dir = tmp_path / "claude-project"
        claude_dir.mkdir()
        run = session_output.start_run(
            tmp_path,
            "issue-123",
            issue_number=123,
            claude_log_dir=str(claude_dir),
        )
        claude_log = claude_dir / "session.jsonl"
        claude_log.write_text(
            json.dumps({
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Captured live"}]},
            }) + "\n",
            encoding="utf-8",
        )

        session_output._sync_claude_replay_capture(run.run_dir)

        manifest = json.loads((run.run_dir / MANIFEST_NAME).read_text())
        assert manifest["claude_log_path"] == str(claude_log)
        decoded = self._decoded_recording(run.run_dir / "terminal-recording.jsonl")
        assert "Captured live" in decoded
        session_output._stop_claude_replay_capture(run.run_dir)
