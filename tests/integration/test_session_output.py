"""Integration tests for session output bundles."""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from issue_orchestrator.control.completion_processor import CompletionProcessor
from issue_orchestrator.domain.models import CompletionOutcome, CompletionRecord, get_completion_path
from issue_orchestrator.infra.session_output import SessionOutputManager
from issue_orchestrator.ports.pull_request_tracker import PRInfo
from issue_orchestrator.ports.working_copy import PushResult


class DummyLabelAdapter:
    def add_label(self, issue_number: int, label: str) -> None:
        return None

    def remove_label(self, issue_number: int, label: str) -> None:
        return None


class DummyPRAdapter:
    def create_pr(self, title: str, body: str, head: str, base: str = "main") -> PRInfo:
        return PRInfo(
            number=1,
            title=title,
            url="https://example.com/pr/1",
            branch=head,
            body=body,
            state="open",
            labels=[],
        )

    def add_comment(self, issue_or_pr_number: int, body: str) -> str:
        return "https://example.com/comment/1"


class DummyGitAdapter:
    def push(
        self,
        worktree: Path,
        remote: str = "origin",
        set_upstream: bool = True,
        skip_hooks: bool = False,
    ) -> PushResult:
        return PushResult(success=True, branch="feature", remote=remote, message="ok")

    def get_current_branch(self, worktree: Path) -> str | None:
        return "feature"

    def has_uncommitted_changes(self, worktree: Path) -> bool:
        return False


def _write_completion(path: Path, record: CompletionRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record.to_dict()))


def test_session_output_manifest_and_validation_pointer(tmp_path: Path) -> None:
    session_name = "issue-1"
    run = SessionOutputManager.start_run(
        worktree_path=tmp_path,
        session_name=session_name,
        issue_number=1,
        agent_label="agent:test",
        backend="subprocess",
        claude_log_dir=str(tmp_path / ".claude" / "projects" / "test"),
        orchestrator_log=str(tmp_path / "orchestrator.log"),
    )

    completion_path = get_completion_path(agent_name="agent:test", session_name=session_name)
    record = CompletionRecord(
        session_id=session_name,
        timestamp=datetime.now(timezone.utc).isoformat(),
        outcome=CompletionOutcome.COMPLETED,
        summary="ok",
        validation_record_path=".issue-orchestrator/validation/sha.json",
    )
    _write_completion(tmp_path / completion_path, record)

    processor = CompletionProcessor(
        label_adapter=DummyLabelAdapter(),
        pr_adapter=DummyPRAdapter(),
        git_adapter=DummyGitAdapter(),
        event_bus=None,
    )
    result = processor.process(
        worktree=tmp_path,
        issue_number=1,
        issue_title="Test Issue",
        completion_path=completion_path,
    )
    assert result.success is True

    manifest = json.loads((run.run_dir / "manifest.json").read_text())
    assert manifest["session_name"] == session_name
    assert manifest["validation_record_path"] == ".issue-orchestrator/validation/sha.json"
    pointer = run.run_dir / "validation-record.path"
    assert pointer.exists()
    assert pointer.read_text().strip() == ".issue-orchestrator/validation/sha.json"


def test_orchestrator_tail_scoped_to_run(tmp_path: Path) -> None:
    session_name = "issue-1"
    run1 = SessionOutputManager.start_run(tmp_path, session_name, issue_number=1)
    run2 = SessionOutputManager.start_run(tmp_path, session_name, issue_number=1)

    log_path = tmp_path / "orchestrator.log"
    lines = [
        f"[SESSION_RUN_START] run_id={run1.run_id} session={session_name} issue=1",
        "[issue-1] first run message",
        f"[SESSION_RUN_START] run_id={run2.run_id} session={session_name} issue=1",
        "[issue-1] second run message",
    ]
    log_path.write_text("\n".join(lines))

    tail_path = SessionOutputManager.write_orchestrator_tail(
        worktree_path=tmp_path,
        session_name=session_name,
        log_path=log_path,
        issue_number=1,
    )
    assert tail_path is not None
    tail = tail_path.read_text()
    assert "second run message" in tail
    assert "first run message" not in tail


def test_session_output_selects_claude_log(tmp_path: Path) -> None:
    session_name = "issue-2"
    claude_dir = tmp_path / ".claude" / "projects" / "test"
    claude_dir.mkdir(parents=True, exist_ok=True)
    run = SessionOutputManager.start_run(
        worktree_path=tmp_path,
        session_name=session_name,
        issue_number=2,
        agent_label="agent:test",
        backend="subprocess",
        claude_log_dir=str(claude_dir),
    )

    older = claude_dir / "older.jsonl"
    newer = claude_dir / "newer.jsonl"
    run_start = datetime.fromisoformat(run.started_at)

    older.write_text(
        json.dumps({
            "timestamp": (run_start + timedelta(seconds=120)).isoformat(),
            "sessionId": "older",
        })
        + "\n"
    )
    newer.write_text(
        json.dumps({
            "timestamp": (run_start + timedelta(seconds=30)).isoformat(),
            "sessionId": "newer",
        })
        + "\n"
    )

    os.utime(older, (run_start.timestamp() + 300, run_start.timestamp() + 300))
    os.utime(newer, (run_start.timestamp() + 600, run_start.timestamp() + 600))

    selected = SessionOutputManager.attach_claude_log(tmp_path, session_name)
    assert selected == newer

    manifest = json.loads((run.run_dir / "manifest.json").read_text())
    assert manifest["claude_log_path"] == str(newer)
    assert manifest["claude_session_id"] == "newer"
    assert (run.run_dir / "claude-session.jsonl").is_symlink()
