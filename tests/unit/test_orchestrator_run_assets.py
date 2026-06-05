"""Tests for typed run assets injected into completion commands."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from issue_orchestrator.entrypoints.cli_tools.orchestrator_run_assets import (
    require_orchestrator_run_assets_for_session,
)


def _run_dir(worktree: Path) -> Path:
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-1"
    run_dir.mkdir(parents=True)
    return run_dir


def _write_manifest(run_dir: Path, payload: object) -> None:
    (run_dir / "manifest.json").write_text(json.dumps(payload), encoding="utf-8")


def _valid_manifest(worktree: Path, run_dir: Path) -> dict[str, str]:
    return {
        "session_name": "test-123",
        "run_id": "run-1",
        "started_at": "2026-06-04T00:00:00Z",
        "worktree": str(worktree),
        "run_dir": str(run_dir),
        "log_path": str(run_dir / "terminal-recording.jsonl"),
    }


def test_require_orchestrator_run_assets_reports_manifest_read_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = _run_dir(tmp_path)
    _write_manifest(run_dir, _valid_manifest(tmp_path, run_dir))
    monkeypatch.setenv("ISSUE_ORCHESTRATOR_RUN_DIR", str(run_dir))

    with patch("pathlib.Path.read_text", side_effect=OSError("pruned")):
        with pytest.raises(SystemExit) as exc_info:
            require_orchestrator_run_assets_for_session(tmp_path, "test-123")

    assert exc_info.value.code == 1
    assert "manifest cannot be read" in capsys.readouterr().err


def test_require_orchestrator_run_assets_rejects_non_object_manifest(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = _run_dir(tmp_path)
    _write_manifest(run_dir, ["not", "an", "object"])
    monkeypatch.setenv("ISSUE_ORCHESTRATOR_RUN_DIR", str(run_dir))

    with pytest.raises(SystemExit) as exc_info:
        require_orchestrator_run_assets_for_session(tmp_path, "test-123")

    assert exc_info.value.code == 1
    assert "manifest must be a JSON object" in capsys.readouterr().err


def test_require_orchestrator_run_assets_reports_invalid_manifest_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    run_dir = _run_dir(tmp_path)
    manifest = _valid_manifest(tmp_path, run_dir)
    manifest["worktree"] = ""
    _write_manifest(run_dir, manifest)
    monkeypatch.setenv("ISSUE_ORCHESTRATOR_RUN_DIR", str(run_dir))

    with pytest.raises(SystemExit) as exc_info:
        require_orchestrator_run_assets_for_session(tmp_path, "test-123")

    assert exc_info.value.code == 1
    assert "manifest is invalid" in capsys.readouterr().err
