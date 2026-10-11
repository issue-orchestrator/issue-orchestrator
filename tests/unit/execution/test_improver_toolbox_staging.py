"""The empowered improver's toolbox is staged from copies; the engine is only read (#8001)."""

from __future__ import annotations

import os
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from issue_orchestrator.contracts.improver_toolbox import TOOLBOX_DIRNAME, TOOLBOX_MANIFEST, ToolboxManifest
from issue_orchestrator.domain.engine_activity import EngineRef
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.execution.improver_toolbox_staging import ImproverToolboxStager

NOW = datetime(2026, 10, 4, 7, 36, tzinfo=UTC)


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def _engine(tmp_path: Path) -> EngineRef:
    checkout = tmp_path / "porchpin"
    checkout.mkdir()
    _git(checkout, "init", "-q", "-b", "main")
    (checkout / "app.py").write_text("print('hi')\n")
    _git(checkout, "add", "app.py")
    _git(checkout, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "porchpin commit")
    state = checkout / ".issue-orchestrator" / "state"
    (state / "logs").mkdir(parents=True)
    (state / "logs" / "orchestrator.log").write_text("2026-10-04 02:00:01 INFO auth_expired parked\n")
    live = sqlite3.connect(state / "timeline.sqlite")
    live.execute("PRAGMA journal_mode=WAL")
    live.execute("CREATE TABLE timeline (event TEXT)")
    live.execute("INSERT INTO timeline VALUES ('review.skipped')")
    live.commit()
    live.close()
    return EngineRef(engine_id="repo-abc", repo="porchpin/porchpin", state_dir=state)


def _tree(root: Path) -> dict[str, tuple[int, int]]:
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime_ns)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_the_toolbox_holds_copies_of_every_store_the_logs_and_a_clone(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    run = tmp_path / "run"
    run.mkdir()
    before = _tree(engine.checkout)

    manifest = ImproverToolboxStager(runner=LocalCommandRunner(), clock=lambda: NOW).stage(engine, run)

    toolbox = run / TOOLBOX_DIRNAME
    assert {s.path: s.staged for s in manifest.sources} == {
        "state/timeline.sqlite": True, "logs/orchestrator.log": True, "repo": True,
    }
    assert manifest.staged_at == NOW
    assert ToolboxManifest.model_validate_json((toolbox / TOOLBOX_MANIFEST).read_text()) == manifest
    # A self-contained copy: no sidecar is needed to read it.
    copy = toolbox / "state" / "timeline.sqlite"
    assert not copy.with_name("timeline.sqlite-wal").exists()
    with sqlite3.connect(f"{copy.as_uri()}?mode=ro&immutable=1", uri=True) as conn:
        assert conn.execute("SELECT event FROM timeline").fetchall() == [("review.skipped",)]
    assert "auth_expired" in (toolbox / "logs" / "orchestrator.log").read_text()
    log = subprocess.run(["git", "log", "--oneline"], cwd=toolbox / "repo", capture_output=True, text=True)
    assert "porchpin commit" in log.stdout
    # The engine's checkout and state were only read; the clone shares no file with them.
    assert _tree(engine.checkout) == before
    clone_objects = {(p.stat().st_ino) for p in (toolbox / "repo" / ".git" / "objects").rglob("*") if p.is_file()}
    source_objects = {(p.stat().st_ino) for p in (engine.checkout / ".git" / "objects").rglob("*") if p.is_file()}
    assert not clone_objects & source_objects


def test_a_missing_source_is_recorded_not_fatal(tmp_path: Path) -> None:
    checkout = tmp_path / "not-git"
    state = checkout / ".issue-orchestrator" / "state"
    state.mkdir(parents=True)
    run = tmp_path / "run"
    run.mkdir()

    manifest = ImproverToolboxStager(runner=LocalCommandRunner(), clock=lambda: NOW).stage(
        EngineRef(engine_id="repo-x", repo="o/r", state_dir=state), run
    )

    assert {s.path: s.staged for s in manifest.sources} == {"state": False, "logs": False, "repo": False}
    assert all(s.detail for s in manifest.sources)


def test_an_engine_ref_outside_a_checkout_has_no_checkout(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        _ = EngineRef(engine_id="x", repo="o/r", state_dir=tmp_path / "state").checkout


def test_the_copies_are_not_the_live_files(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    run = tmp_path / "run"
    run.mkdir()

    ImproverToolboxStager(runner=LocalCommandRunner(), clock=lambda: NOW).stage(engine, run)

    live = engine.state_dir / "timeline.sqlite"
    copy = run / TOOLBOX_DIRNAME / "state" / "timeline.sqlite"
    assert os.stat(live).st_ino != os.stat(copy).st_ino


def test_a_symlinked_log_or_store_is_never_followed(tmp_path: Path) -> None:
    """r1 F2: a symlink in the state dir could point at any file the
    orchestrator can read; its copy would land where the agent reads."""
    engine = _engine(tmp_path)
    (tmp_path / "secret.log").write_text("canary-log-3391\n")
    with sqlite3.connect(tmp_path / "foreign.sqlite") as conn:
        conn.execute("CREATE TABLE secret (v TEXT)")
        conn.execute("INSERT INTO secret VALUES ('canary-store-3391')")
    (engine.state_dir / "logs" / "linked.log").symlink_to(tmp_path / "secret.log")
    (engine.state_dir / "linked.sqlite").symlink_to(tmp_path / "foreign.sqlite")
    run = tmp_path / "run"
    run.mkdir()

    manifest = ImproverToolboxStager(runner=LocalCommandRunner(), clock=lambda: NOW).stage(engine, run)

    staged = {s.path: (s.staged, s.detail) for s in manifest.sources}
    assert staged["logs/linked.log"] == (False, "a symlink; not followed")
    assert staged["state/linked.sqlite"] == (False, "a symlink; not followed")
    assert staged["logs/orchestrator.log"][0] and staged["state/timeline.sqlite"][0]
    for path in (run / "toolbox").rglob("*"):
        if path.is_file():
            assert b"canary-" not in path.read_bytes(), path


def test_a_symlinked_logs_directory_is_never_followed(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "orchestrator.log").write_text("canary-dir-3391\n")
    logs = engine.state_dir / "logs"
    for f in logs.iterdir():
        f.unlink()
    logs.rmdir()
    logs.symlink_to(elsewhere)
    run = tmp_path / "run"
    run.mkdir()

    manifest = ImproverToolboxStager(runner=LocalCommandRunner(), clock=lambda: NOW).stage(engine, run)

    assert ("logs", False, "a symlink; not followed") in {(s.path, s.staged, s.detail) for s in manifest.sources}
    assert not any((run / "toolbox" / "logs").iterdir())
