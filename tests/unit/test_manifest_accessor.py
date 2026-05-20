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
    recording = run_dir / "terminal-recording.jsonl"
    recording.write_text('{"event_type":"output","offset_ms":0,"data_b64":"aGVsbG8K","schema_version":1}\n', encoding="utf-8")

    artifact = accessor.get_terminal_recording()
    assert artifact.descriptor.artifact_type == "terminal_recording"
    assert artifact.path == recording
    assert artifact.descriptor.length_bytes is not None


def test_get_terminal_recording_returns_run_scoped_recording(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    recording = run_dir / "terminal-recording.jsonl"
    recording.write_text('{"event_type":"output","offset_ms":0,"data_b64":"aGVsbG8K","schema_version":1}\n', encoding="utf-8")

    artifact = accessor.get_terminal_recording()
    assert artifact.descriptor.artifact_type == "terminal_recording"
    assert artifact.path == recording


def test_get_terminal_recording_raises_when_empty(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    (run_dir / "terminal-recording.jsonl").write_text("", encoding="utf-8")

    with pytest.raises(ArtifactNotFoundError, match="terminal recording is empty"):
        accessor.get_terminal_recording()


def test_get_review_exchange_phase_terminal_recording_returns_phase_scoped_recording(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    recording = run_dir / "review-exchange" / "round-002" / "reviewer" / "terminal-recording.jsonl"
    recording.parent.mkdir(parents=True, exist_ok=True)
    recording.write_text(
        '{"event_type":"output","offset_ms":0,"data_b64":"aGVsbG8K","schema_version":1}\n',
        encoding="utf-8",
    )

    artifact = accessor.get_review_exchange_phase_terminal_recording(round_index=2, role="reviewer")
    assert artifact.path == recording


def test_get_review_exchange_phase_terminal_recording_raises_when_missing(tmp_path: Path) -> None:
    accessor, _worktree, _run_dir = _build_accessor(tmp_path)

    with pytest.raises(ArtifactNotFoundError, match="review exchange recording not found"):
        accessor.get_review_exchange_phase_terminal_recording(round_index=1, role="reviewer")


def test_get_review_exchange_phase_terminal_recording_resolves_persistent_layout(
    tmp_path: Path,
) -> None:
    """Persistent runner writes one continuous recording per role at
    ``<run_dir>/<role>/terminal-recording.jsonl`` (no per-round subdir).
    The accessor must serve that file when present so timeline review
    actions don't 404 after the cutover (#6160 review feedback).
    """
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    persistent_recording = run_dir / "reviewer" / "terminal-recording.jsonl"
    persistent_recording.parent.mkdir(parents=True, exist_ok=True)
    persistent_recording.write_text(
        '{"event_type":"output","offset_ms":0,"data_b64":"aGVsbG8K","schema_version":1}\n',
        encoding="utf-8",
    )

    artifact = accessor.get_review_exchange_phase_terminal_recording(
        round_index=2, role="reviewer",
    )
    # The persistent layout has no per-round directory; the chapter
    # offsets in chapters.json are how the UI scrubs to a specific
    # round inside the role's continuous recording.
    assert artifact.path == persistent_recording


def test_get_review_exchange_phase_terminal_recording_prefers_persistent_when_both_exist(
    tmp_path: Path,
) -> None:
    """When both layouts are present (e.g. mid-migration), the
    persistent layout wins — that's the live one the new runner writes."""
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    persistent_recording = run_dir / "coder" / "terminal-recording.jsonl"
    persistent_recording.parent.mkdir(parents=True, exist_ok=True)
    persistent_recording.write_text(
        '{"event_type":"output","offset_ms":0,"data_b64":"bmV3","schema_version":1}\n',
        encoding="utf-8",
    )
    legacy_recording = (
        run_dir / "review-exchange" / "round-001" / "coder" / "terminal-recording.jsonl"
    )
    legacy_recording.parent.mkdir(parents=True, exist_ok=True)
    legacy_recording.write_text(
        '{"event_type":"output","offset_ms":0,"data_b64":"b2xk","schema_version":1}\n',
        encoding="utf-8",
    )

    artifact = accessor.get_review_exchange_phase_terminal_recording(
        round_index=1, role="coder",
    )
    assert artifact.path == persistent_recording


def test_get_review_exchange_phase_terminal_recording_falls_back_to_legacy_layout(
    tmp_path: Path,
) -> None:
    """Pre-cutover runs that wrote the spawn-per-phase layout must
    still resolve. The fallback path keeps existing artifacts viewable
    after the cutover lands."""
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    legacy_recording = (
        run_dir / "review-exchange" / "round-003" / "reviewer" / "terminal-recording.jsonl"
    )
    legacy_recording.parent.mkdir(parents=True, exist_ok=True)
    legacy_recording.write_text(
        '{"event_type":"output","offset_ms":0,"data_b64":"bGVnYWN5","schema_version":1}\n',
        encoding="utf-8",
    )

    artifact = accessor.get_review_exchange_phase_terminal_recording(
        round_index=3, role="reviewer",
    )
    assert artifact.path == legacy_recording


def test_get_review_artifact_requires_persisted_turn_artifact_path(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    turns = run_dir / "review-exchange" / "turns"
    turns.mkdir(parents=True)
    report = turns / "round-001.reviewer.attempt-001.review-report.md"
    report.write_text("# Review\n\nLooks good.\n", encoding="utf-8")

    artifact = accessor.get_review_artifact(
        artifact_path=str(report),
        artifact_type="review_report",
    )

    assert artifact.path == report
    assert artifact.descriptor.content_type == "text/markdown"


def test_get_review_artifact_rejects_client_selected_non_turn_file(
    tmp_path: Path,
) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    stray = run_dir / "anything-not-emitted-by-this-run.md"
    stray.write_text("# Not a review artifact\n", encoding="utf-8")

    with pytest.raises(ArtifactNotFoundError, match="not a persisted review turn artifact"):
        accessor.get_review_artifact(
            artifact_path=str(stray),
            artifact_type="review_report",
        )


def test_get_review_artifact_rejects_wrong_filename_for_type(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    turns = run_dir / "review-exchange" / "turns"
    turns.mkdir(parents=True)
    report = turns / "round-001.reviewer.attempt-001.not-a-review.md"
    report.write_text("# Not a review artifact\n", encoding="utf-8")

    with pytest.raises(ArtifactNotFoundError, match="not a persisted review turn artifact"):
        accessor.get_review_artifact(
            artifact_path=str(report),
            artifact_type="review_report",
        )


def test_get_agent_log_uses_terminal_recording_even_when_claude_log_exists(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    recording = run_dir / "terminal-recording.jsonl"
    recording.write_text('{"event_type":"output","offset_ms":0,"data_b64":"aGVsbG8K","schema_version":1}\n', encoding="utf-8")
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    claude_log = claude_dir / "run.jsonl"
    claude_log.write_text('{"message":"ok"}\n', encoding="utf-8")

    FileSystemSessionOutput().update_manifest(
        run_dir,
        {
            "claude_log_dir": str(claude_dir),
        },
    )

    artifact = accessor.get_agent_log()
    assert artifact.path == recording
    assert artifact.path != claude_log


def test_get_agent_log_raises_when_terminal_recording_empty(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    (run_dir / "terminal-recording.jsonl").write_text("", encoding="utf-8")
    with pytest.raises(ArtifactNotFoundError, match="terminal recording is empty"):
        accessor.get_agent_log()


def test_get_agent_log_allow_empty_returns_terminal_recording(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    recording = run_dir / "terminal-recording.jsonl"
    recording.write_text("", encoding="utf-8")

    artifact = accessor.get_agent_log(allow_empty=True)
    assert artifact.path == recording


def test_get_agent_log_raises_when_terminal_recording_only_empty_by_default(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    recording = run_dir / "terminal-recording.jsonl"
    recording.write_text("", encoding="utf-8")

    with pytest.raises(ArtifactNotFoundError, match="terminal recording is empty"):
        accessor.get_agent_log()


def test_get_agent_log_raises_when_terminal_recording_empty_even_if_claude_log_exists(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    recording = run_dir / "terminal-recording.jsonl"
    recording.write_text("", encoding="utf-8")
    claude = run_dir / "claude.jsonl"
    claude.write_text('{"type":"assistant","message":{"content":[{"type":"text","text":"ok"}]}}\n', encoding="utf-8")
    FileSystemSessionOutput().update_manifest(
        run_dir,
        {
            "claude_log_path": str(claude),
        },
    )

    with pytest.raises(ArtifactNotFoundError, match="terminal recording is empty"):
        accessor.get_agent_log()


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


def test_get_claude_log_falls_back_to_manifest_log_dir(tmp_path: Path) -> None:
    accessor, _worktree, run_dir = _build_accessor(tmp_path)
    claude_dir = tmp_path / "claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    claude = claude_dir / "latest.jsonl"
    claude.write_text('{"type":"assistant","message":{"content":[{"type":"text","text":"ok"}]}}\n', encoding="utf-8")
    (run_dir / "manifest.json").write_text(
        json.dumps(
            {
                "session_name": "issue-123",
                "run_id": run_dir.name.split("__", 1)[0],
                "run_dir": str(run_dir),
                "claude_log_dir": str(claude_dir),
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
