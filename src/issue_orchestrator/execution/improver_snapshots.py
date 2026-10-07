"""Frozen snapshots of an engine, for comparing improver arms on equal ground (#8001).

A snapshot is what one improver run saw, kept unchanged: ``improver-data/``
(the staged bundle) and, optionally, ``toolbox/`` (byte copies of the
engine's stores and logs, and a clone of its repository). A tournament or a
challenger evaluation copies it into each arm's run dir, so every arm reads
the same evidence however much later it runs.

Staged inputs written by an older io are upgraded to today's contracts on
import, each change named in the snapshot's ``upgrades``; an input that
still does not load refuses the import (an arm could not be judged on it).

Copies on macOS use APFS clones (``cp -c``): a snapshot's gigabyte of logs
costs no space until something changes it, and nothing does.
"""

from __future__ import annotations

import json
import shutil
import sys
from datetime import datetime
from pathlib import Path

from ..contracts.engine_audit import EngineAuditReport
from ..contracts.improver_inputs import AUDIT_FILE, IMPROVER_DATA_DIRNAME, INPUTS_FILE, INTERVENTIONS_FILE, InputsManifest
from ..contracts.improver_toolbox import (
    TOOLBOX_DIRNAME,
    TOOLBOX_LOGS_DIRNAME,
    TOOLBOX_MANIFEST,
    TOOLBOX_REPO_DIRNAME,
    TOOLBOX_STATE_DIRNAME,
    ToolboxManifest,
    ToolboxSource,
)
from ..contracts.improver_tournament import SNAPSHOT_MANIFEST, FrozenSnapshot
from ..domain.engine_activity import EngineRef
from ..ports.command_runner import CommandRunner

SNAPSHOTS_DIRNAME = "snapshots"

#: Interventions staged before #8001 increment 4 carry no source or
#: attribution: each kind came from one engine record and one operator surface.
_LEGACY_INTERVENTION_SOURCES = {
    "reset_retry": "timeline",
    "proposal_approved": "charter_ledger",
    "proposal_declined": "charter_ledger",
    "operator_pause": "pause_journal",
}


class SnapshotUnavailable(RuntimeError):
    """No such snapshot, or one that cannot be imported."""


class FrozenSnapshotStore:
    def __init__(self, root: Path, runner: CommandRunner) -> None:
        self._root = root / SNAPSHOTS_DIRNAME
        self._runner = runner

    def ids(self) -> tuple[str, ...]:
        return tuple(sorted(p.parent.name for p in self._root.glob(f"*/{SNAPSHOT_MANIFEST}")))

    def get(self, snapshot_id: str) -> FrozenSnapshot:
        manifest = self._root / snapshot_id / SNAPSHOT_MANIFEST
        if not manifest.is_file():
            raise SnapshotUnavailable(f"no frozen snapshot {snapshot_id!r}; have {', '.join(self.ids()) or 'none'}")
        return FrozenSnapshot.model_validate_json(manifest.read_text(encoding="utf-8"))

    def import_(
        self,
        snapshot_id: str,
        *,
        improver_data: Path,
        taken_at: datetime,
        origin: str,
        state_dir: Path | None = None,
        clone: Path | None = None,
    ) -> FrozenSnapshot:
        """Freeze ``improver_data`` (and, for empowered arms, the engine's
        store copies and logs in ``state_dir`` and a repository ``clone``)."""
        target = self._root / snapshot_id
        if target.exists():
            raise SnapshotUnavailable(f"snapshot {snapshot_id!r} exists; a snapshot is never changed")
        if (state_dir is None) != (clone is None):
            raise SnapshotUnavailable("a toolbox needs both the store copies and the repository clone")
        target.mkdir(parents=True)
        try:
            data = target / IMPROVER_DATA_DIRNAME
            self._copy(improver_data, data)
            upgrades = upgrade_staged_inputs(data)
            manifest = InputsManifest.model_validate_json((data / INPUTS_FILE).read_text(encoding="utf-8"))
            _require_loadable(data)
            if state_dir is not None and clone is not None:
                self._freeze_toolbox(target / TOOLBOX_DIRNAME, state_dir, clone, manifest.audited_repo, taken_at)
            snapshot = FrozenSnapshot(
                id=snapshot_id, taken_at=taken_at, audited_repo=manifest.audited_repo,
                engine_id=manifest.engine_id, engine_commit=_engine_commit(data), origin=origin,
                upgrades=upgrades, has_toolbox=state_dir is not None,
            )
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise
        (target / SNAPSHOT_MANIFEST).write_text(snapshot.model_dump_json(indent=2) + "\n", encoding="utf-8")
        return snapshot

    def engine(self, snapshot_id: str) -> EngineRef:
        """The snapshot's engine, as a run on it names it (its state is the frozen copy)."""
        snapshot = self.get(snapshot_id)
        return EngineRef(
            engine_id=snapshot.engine_id,
            repo=snapshot.audited_repo,
            state_dir=self._root / snapshot_id / TOOLBOX_DIRNAME / TOOLBOX_STATE_DIRNAME,
        )

    def copy_inputs(self, snapshot_id: str, run_dir: Path) -> Path:
        """The snapshot's staged bundle, copied into ``run_dir``; its path."""
        self.get(snapshot_id)
        data = run_dir / IMPROVER_DATA_DIRNAME
        self._copy(self._root / snapshot_id / IMPROVER_DATA_DIRNAME, data)
        return data

    def copy_toolbox(self, snapshot_id: str, run_dir: Path) -> ToolboxManifest:
        """The snapshot's toolbox, copied into ``run_dir``; its manifest."""
        if not self.get(snapshot_id).has_toolbox:
            raise SnapshotUnavailable(f"snapshot {snapshot_id!r} has no toolbox: it can run scripted arms only")
        toolbox = run_dir / TOOLBOX_DIRNAME
        self._copy(self._root / snapshot_id / TOOLBOX_DIRNAME, toolbox)
        return ToolboxManifest.model_validate_json((toolbox / TOOLBOX_MANIFEST).read_text(encoding="utf-8"))

    def _freeze_toolbox(self, toolbox: Path, state_dir: Path, clone: Path, repo: str, taken_at: datetime) -> None:
        if not (clone / ".git").exists():
            raise SnapshotUnavailable(f"{clone} is not a Git clone")
        state, logs = toolbox / TOOLBOX_STATE_DIRNAME, toolbox / TOOLBOX_LOGS_DIRNAME
        state.mkdir(parents=True)
        logs.mkdir()
        sources: list[ToolboxSource] = []
        for store in sorted(state_dir.glob("*.sqlite")):
            if store.is_symlink():
                raise SnapshotUnavailable(f"{store} is a symlink; a snapshot copies only files")
            self._copy(store, state / store.name)
            for sidecar in ("-wal", "-shm"):
                beside = store.with_name(store.name + sidecar)
                if beside.is_file() and not beside.is_symlink():
                    self._copy(beside, state / beside.name)
            sources.append(ToolboxSource(path=f"{TOOLBOX_STATE_DIRNAME}/{store.name}", staged=True, detail="byte copy"))
        for log in sorted(p for p in (state_dir / "logs").glob("*") if p.is_file() and not p.is_symlink()):
            self._copy(log, logs / log.name)
            sources.append(ToolboxSource(path=f"{TOOLBOX_LOGS_DIRNAME}/{log.name}", staged=True, detail="byte copy"))
        self._copy(clone, toolbox / TOOLBOX_REPO_DIRNAME)
        sources.append(ToolboxSource(path=TOOLBOX_REPO_DIRNAME, staged=True, detail=f"clone, as of {taken_at.isoformat()}"))
        manifest = ToolboxManifest(audited_repo=repo, staged_at=taken_at, sources=tuple(sources))
        (toolbox / TOOLBOX_MANIFEST).write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")

    def _copy(self, source: Path, target: Path) -> None:
        """An independent copy: an APFS clone on macOS, a plain copy elsewhere."""
        if sys.platform == "darwin":
            done = self._runner.run(["/bin/cp", "-cRp", str(source), str(target)], timeout_seconds=1800)
            if done.returncode:
                raise SnapshotUnavailable(f"cannot copy {source}: {done.stderr.strip()}")
        elif source.is_dir():
            shutil.copytree(source, target, symlinks=True)
        else:
            shutil.copy2(source, target)


def upgrade_staged_inputs(data: Path) -> tuple[str, ...]:
    """Bring a staged bundle written by an older io to today's contracts, in
    place; each change it made, in words."""
    upgrades: list[str] = []
    path = data / INTERVENTIONS_FILE
    if path.is_file():
        doc = json.loads(path.read_text(encoding="utf-8"))
        if "github" not in doc:
            for entry in doc.get("interventions", []):
                kind = entry.get("kind")
                if kind not in _LEGACY_INTERVENTION_SOURCES:
                    raise SnapshotUnavailable(f"{path}: unknown legacy intervention kind {kind!r}")
                entry.setdefault("source", _LEGACY_INTERVENTION_SOURCES[kind])
                entry.setdefault("attribution", "operator_surface")
            doc["github"] = {
                "repo": json.loads((data / INPUTS_FILE).read_text(encoding="utf-8"))["audited_repo"],
                "read": False,
                "detail": "staged before the GitHub hand-action read existed (#8001)",
                "sources": [],
                "automation_excluded": 0,
                "attribution_limits": [],
            }
            path.write_text(json.dumps(doc, indent=2) + "\n", encoding="utf-8")
            upgrades.append(
                "interventions.json: each intervention's source and attribution from its kind; GitHub hand"
                " actions marked unread (staged before #8001)"
            )
    return tuple(upgrades)


def _require_loadable(data: Path) -> None:
    from ..entrypoints.improver_staging import load_staged_evidence

    try:
        load_staged_evidence(data)
    except Exception as error:
        raise SnapshotUnavailable(f"the staged inputs do not load with today's contracts: {error}") from error


def _engine_commit(data: Path) -> str:
    return str(json.loads((data / "engine-start.json").read_text(encoding="utf-8"))["engine_commit"])


def audit_of(data: Path) -> EngineAuditReport:
    return EngineAuditReport.model_validate_json((data / AUDIT_FILE).read_text(encoding="utf-8"))


__all__ = [
    "SNAPSHOTS_DIRNAME",
    "FrozenSnapshotStore",
    "SnapshotUnavailable",
    "audit_of",
    "upgrade_staged_inputs",
]
