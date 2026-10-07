"""Stage the empowered improver's read-only toolbox: ``<run dir>/toolbox/`` (#8001).

The staged ``improver-data/`` bundle is the improver's starting map; the
toolbox is what it may dig into beyond it:

* ``state/`` — a byte copy of EVERY SQLite store in the engine's state
  directory (:func:`~..infra.sqlite_snapshot.snapshot_sqlite`: never opened
  live, each copy self-contained, so a reader needs no sidecar);
* ``logs/`` — byte copies of the engine's logs, whole (``improver-data``
  carries only a tail);
* ``repo/`` — a clone of the audited repository's checkout, ``--no-hardlinks``
  so nothing in the run can reach the checkout's own files.

The engine's source is already staged (``improver-data/engine-source/``).
A source the engine does not have is recorded as missing in
``toolbox.json``, never a crash: the agent is told what it lacks.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from ..contracts.improver_toolbox import (
    TOOLBOX_DIRNAME,
    TOOLBOX_LOGS_DIRNAME,
    TOOLBOX_MANIFEST,
    TOOLBOX_REPO_DIRNAME,
    TOOLBOX_STATE_DIRNAME,
    ToolboxManifest,
    ToolboxSource,
)
from ..domain.engine_activity import EngineRef
from ..domain.read_only_sqlite import ReadOnlySqliteAccessError
from ..infra.sqlite_snapshot import snapshot_sqlite
from ..ports.command_runner import CommandRunner

#: Seconds a store copy may take to open before it is reported unreadable.
SQLITE_TIMEOUT = 120.0
#: Seconds the clone of the audited repository may take.
CLONE_TIMEOUT = 600


class ImproverToolboxStager:
    def __init__(self, *, runner: CommandRunner, clock: Callable[[], datetime]) -> None:
        self._runner = runner
        self._clock = clock

    def stage(self, engine: EngineRef, run_dir: Path) -> ToolboxManifest:
        root = run_dir / TOOLBOX_DIRNAME
        root.mkdir()
        staged_at = self._clock()
        sources = [
            *self._stores(engine.state_dir, root / TOOLBOX_STATE_DIRNAME),
            *self._logs(engine.state_dir / "logs", root / TOOLBOX_LOGS_DIRNAME),
            self._clone(engine.checkout, root / TOOLBOX_REPO_DIRNAME),
        ]
        manifest = ToolboxManifest(audited_repo=engine.repo, staged_at=staged_at, sources=tuple(sources))
        (root / TOOLBOX_MANIFEST).write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return manifest

    @staticmethod
    def _stores(state_dir: Path, destination: Path) -> list[ToolboxSource]:
        destination.mkdir()
        sources = []
        for live in sorted(state_dir.glob("*.sqlite")):
            path = f"{TOOLBOX_STATE_DIRNAME}/{live.name}"
            try:
                snapshot_sqlite(live, destination / live.name, timeout=SQLITE_TIMEOUT)
            except ReadOnlySqliteAccessError as error:
                sources.append(ToolboxSource(path=path, staged=False, detail=str(error)))
                continue
            sources.append(ToolboxSource(path=path, staged=True, detail="byte copy"))
        if not sources:
            sources.append(ToolboxSource(path=TOOLBOX_STATE_DIRNAME, staged=False, detail=f"no store in {state_dir}"))
        return sources

    @staticmethod
    def _logs(logs: Path, destination: Path) -> list[ToolboxSource]:
        destination.mkdir()
        files = sorted(p for p in logs.glob("*") if p.is_file()) if logs.is_dir() else []
        if not files:
            return [ToolboxSource(path=TOOLBOX_LOGS_DIRNAME, staged=False, detail=f"no log in {logs}")]
        for log in files:
            shutil.copyfile(log, destination / log.name)
        return [
            ToolboxSource(path=f"{TOOLBOX_LOGS_DIRNAME}/{log.name}", staged=True, detail="byte copy")
            for log in files
        ]

    def _clone(self, checkout: Path, destination: Path) -> ToolboxSource:
        path = TOOLBOX_REPO_DIRNAME
        if not (checkout / ".git").exists():
            return ToolboxSource(path=path, staged=False, detail=f"{checkout} is not a Git checkout")
        done = self._runner.run(
            ["git", "clone", "--quiet", "--no-hardlinks", str(checkout), str(destination)],
            timeout_seconds=CLONE_TIMEOUT,
        )
        if done.returncode or done.timed_out:
            shutil.rmtree(destination, ignore_errors=True)
            return ToolboxSource(path=path, staged=False, detail=f"clone failed: {done.stderr.strip()[-300:]}")
        return ToolboxSource(path=path, staged=True, detail=f"clone of {checkout}")


__all__ = ["ImproverToolboxStager"]
