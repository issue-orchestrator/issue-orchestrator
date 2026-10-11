"""A staged improver toolbox for tests (#8001): a store copy, a clone, and
files OUTSIDE the run directory that no tool may reach."""

from __future__ import annotations

import sqlite3
import subprocess
from pathlib import Path

from issue_orchestrator.contracts.improver_toolbox import TOOLBOX_DIRNAME

#: The content of ``<tmp>/secret.txt``, beside the run directory.
OUTSIDE_CANARY = "canary-outside-0451"


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True)


def build_toolbox_run(tmp_path: Path) -> Path:
    """``<tmp>/run`` with ``toolbox/state/timeline.sqlite`` and ``toolbox/repo``;
    ``<tmp>/outside.sqlite`` and ``<tmp>/secret.txt`` lie outside it."""
    run = tmp_path / "run"
    state = run / TOOLBOX_DIRNAME / "state"
    state.mkdir(parents=True)
    with sqlite3.connect(state / "timeline.sqlite") as conn:
        conn.execute("CREATE TABLE timeline (id INTEGER PRIMARY KEY, event TEXT)")
        conn.executemany("INSERT INTO timeline (event) VALUES (?)", [("review.skipped",), ("rework.queued",)])
    repo = run / TOOLBOX_DIRNAME / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    (repo / "README.md").write_text("porchpin\n")
    _git(repo, "add", "README.md")
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@e", "commit", "-q", "-m", "first commit")
    (tmp_path / "outside.sqlite").write_bytes((state / "timeline.sqlite").read_bytes())
    (tmp_path / "secret.txt").write_text(OUTSIDE_CANARY + "\n")
    return run
