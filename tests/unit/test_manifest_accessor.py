from __future__ import annotations

import json
from pathlib import Path

import pytest

from issue_orchestrator.execution.manifest_accessor import (
    ArtifactNotFoundError,
    ManifestAccessor,
    RunIdentity,
)
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput


def _build_accessor(tmp_path: Path, *, issue_number: int = 123) -> tuple[ManifestAccessor, Path, Path]:
    session_output = FileSystemSessionOutput()
    worktree = tmp_path / "wt"
    worktree.mkdir(parents=True)
    run = session_output.start_run(worktree, f"issue-{issue_number}", issue_number=issue_number)
    identity = RunIdentity(issue_number=issue_number, run_dir=run.run_dir)
    return ManifestAccessor(identity), worktree, run.run_dir


def test_get_agent_log_returns_run_scoped_log(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    (run_dir / "session.log").write_text("hello\n", encoding="utf-8")

    artifact = accessor.get_agent_log()
    assert artifact.descriptor.artifact_type == "agent_log"
    assert artifact.path == run_dir / "session.log"
    assert artifact.descriptor.length_bytes is not None


def test_get_agent_log_prefers_non_empty_alternate_when_session_log_empty(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    (run_dir / "session.log").write_text("", encoding="utf-8")
    provider_stdout = run_dir / "provider-runner" / "stdout.log"
    provider_stdout.parent.mkdir(parents=True, exist_ok=True)
    provider_stdout.write_text("provider output\n", encoding="utf-8")

    artifact = accessor.get_agent_log()
    assert artifact.path == provider_stdout


def test_get_claude_log_reads_manifest_path(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    claude = run_dir / "claude.jsonl"
    claude.write_text('{"type":"assistant","content":"ok"}\n', encoding="utf-8")
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "session_name": "issue-123",
                "run_id": run_dir.name.split("__", 1)[0],
                "run_dir": str(run_dir),
                "claude_log_path": str(claude),
            }
        ),
        encoding="utf-8",
    )

    artifact = accessor.get_claude_log()
    assert artifact.descriptor.artifact_type == "claude_log"
    assert artifact.path == claude


def test_get_completion_record_uses_worktree_relative_manifest_path(tmp_path: Path) -> None:
    accessor, worktree, run_dir = _build_accessor(tmp_path)
    completion_rel = ".issue-orchestrator/sessions/issue-123/completion-backend.json"
    completion = worktree / completion_rel
    completion.parent.mkdir(parents=True, exist_ok=True)
    completion.write_text('{"status":"completed"}\n', encoding="utf-8")
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "session_name": "issue-123",
                "run_id": run_dir.name.split("__", 1)[0],
                "run_dir": str(run_dir),
                "completion_path": completion_rel,
            }
        ),
        encoding="utf-8",
    )

    artifact = accessor.get_completion_record()
    assert artifact.descriptor.artifact_type == "completion_record"
    assert artifact.path == completion
    assert artifact.descriptor.content_type == "application/json"


def test_get_validation_record_raises_when_missing(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "session_name": "issue-123",
                "run_id": run_dir.name.split("__", 1)[0],
                "run_dir": str(run_dir),
                "validation_record_path": "validation.json",
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ArtifactNotFoundError):
        accessor.get_validation_record()
