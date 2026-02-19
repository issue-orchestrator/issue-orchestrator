from pathlib import Path

from issue_orchestrator.entrypoints.cli_tools.provider_runner import _ensure_run_scoped_session_log


def test_ensure_run_scoped_session_log_creates_symlink(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)

    _ensure_run_scoped_session_log(run_dir)

    session_log = run_dir / "session.log"
    assert session_log.is_symlink()
    assert session_log.resolve() == (run_dir / "provider-runner" / "stdout.log").resolve()


def test_ensure_run_scoped_session_log_keeps_existing_file(tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    session_log = run_dir / "session.log"
    session_log.write_text("existing")

    _ensure_run_scoped_session_log(run_dir)

    assert session_log.read_text() == "existing"
