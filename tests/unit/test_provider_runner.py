from pathlib import Path

import pytest

from issue_orchestrator.entrypoints.cli_tools.provider_runner import _ensure_run_scoped_session_log


def test_ensure_run_scoped_session_log_creates_symlink(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)

    _ensure_run_scoped_session_log(run_dir)

    session_log = run_dir / "ui-session.log"
    assert session_log.is_symlink()
    assert session_log.resolve() == (run_dir / "provider-runner" / "stdout.log").resolve()


def test_ensure_run_scoped_session_log_replaces_empty_placeholder_file(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    session_log = run_dir / "ui-session.log"
    session_log.write_text("")

    _ensure_run_scoped_session_log(run_dir)

    assert session_log.is_symlink()
    assert session_log.resolve() == (run_dir / "provider-runner" / "stdout.log").resolve()


def test_ensure_run_scoped_session_log_fails_on_non_empty_unwired_file(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    session_log = run_dir / "ui-session.log"
    session_log.write_text("existing")

    with pytest.raises(RuntimeError, match="already exists with content"):
        _ensure_run_scoped_session_log(run_dir)
