from __future__ import annotations

import json
from pathlib import Path

import pytest

from issue_orchestrator.execution.manifest_accessor import ManifestAccessor, RunIdentity
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput


def _build_accessor(tmp_path: Path, *, issue_number: int = 123) -> tuple[ManifestAccessor, Path, Path]:
    session_output = FileSystemSessionOutput()
    worktree = tmp_path / "wt"
    worktree.mkdir(parents=True)
    run = session_output.start_run(worktree, f"issue-{issue_number}", issue_number=issue_number)
    identity = RunIdentity(issue_number=issue_number, run_dir=run.run_dir)
    return ManifestAccessor(identity), worktree, run.run_dir


def test_manifest_accessor_get_agent_log_integration(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    expected = run_dir / "ui-session.log"
    expected.write_text("agent output\n", encoding="utf-8")

    artifact = accessor.get_agent_log()

    assert artifact.descriptor.artifact_type == "agent_log"
    assert artifact.path == expected
    assert artifact.descriptor.length_bytes == expected.stat().st_size


def test_manifest_accessor_get_claude_log_integration(tmp_path: Path) -> None:
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


def test_manifest_accessor_get_completion_record_integration(tmp_path: Path) -> None:
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


def test_manifest_accessor_get_validation_record_integration(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    validation_rel = "validation.json"
    validation = run_dir / validation_rel
    validation.write_text('{"ok":true}\n', encoding="utf-8")
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "session_name": "issue-123",
                "run_id": run_dir.name.split("__", 1)[0],
                "run_dir": str(run_dir),
                "validation_record_path": validation_rel,
            }
        ),
        encoding="utf-8",
    )

    artifact = accessor.get_validation_record()

    assert artifact.descriptor.artifact_type == "validation_record"
    assert artifact.path == validation
    assert artifact.descriptor.content_type == "application/json"


def test_manifest_accessor_rejects_empty_completion_record(tmp_path: Path) -> None:
    accessor, worktree, run_dir = _build_accessor(tmp_path)
    completion_rel = ".issue-orchestrator/sessions/issue-123/completion-backend.json"
    completion = worktree / completion_rel
    completion.parent.mkdir(parents=True, exist_ok=True)
    completion.write_text("", encoding="utf-8")
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

    with pytest.raises(FileNotFoundError, match="empty"):
        accessor.get_completion_record()


def test_manifest_accessor_rejects_invalid_validation_json(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    validation_rel = "validation.json"
    validation = run_dir / validation_rel
    validation.write_text("{invalid", encoding="utf-8")
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "session_name": "issue-123",
                "run_id": run_dir.name.split("__", 1)[0],
                "run_dir": str(run_dir),
                "validation_record_path": validation_rel,
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(FileNotFoundError, match="invalid JSON"):
        accessor.get_validation_record()
