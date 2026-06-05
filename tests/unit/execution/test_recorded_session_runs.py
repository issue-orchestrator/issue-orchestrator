"""Tests for typed recorded session-run lookup ownership."""

import json

from issue_orchestrator.execution.recorded_session_runs import RecordedSessionRunLookup
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput


def test_assets_for_exact_session_returns_typed_assets(tmp_path):
    session_output = FileSystemSessionOutput()
    worktree = tmp_path
    session_name = "debug-123"
    run = session_output.start_run(
        worktree,
        session_name,
        issue_number=123,
        agent_label="agent:web",
    )

    assets = RecordedSessionRunLookup(session_output).assets_for_exact_session(
        worktree,
        session_name,
    )

    assert assets == run
    assert assets is not None
    assert assets.run_dir == run.run_dir
    assert assets.session_name == session_name


def test_assets_for_exact_session_refuses_invalid_manifest(tmp_path):
    session_output = FileSystemSessionOutput()
    worktree = tmp_path
    session_name = "debug-123"
    run = session_output.start_run(
        worktree,
        session_name,
        issue_number=123,
        agent_label="agent:web",
    )
    (run.run_dir / "manifest.json").write_text(
        json.dumps({"session_name": session_name}),
        encoding="utf-8",
    )

    assets = RecordedSessionRunLookup(session_output).assets_for_exact_session(
        worktree,
        session_name,
    )

    assert assets is None


def test_debug_resume_target_requires_manifest_completion_path(tmp_path):
    session_output = FileSystemSessionOutput()
    worktree = tmp_path
    run = session_output.start_run(
        worktree,
        "debug-123",
        issue_number=123,
        agent_label="agent:web",
    )
    completion_path = f".issue-orchestrator/sessions/{run.run_dir.name}/completion.json"
    session_output.update_manifest(run.run_dir, {"completion_path": completion_path})

    target = RecordedSessionRunLookup(session_output).debug_resume_target(
        worktree,
        issue_number=123,
    )

    assert target is not None
    assert target.run_dir == run.run_dir
    assert target.completion_path == completion_path
    assert target.completion_file() == worktree / completion_path
