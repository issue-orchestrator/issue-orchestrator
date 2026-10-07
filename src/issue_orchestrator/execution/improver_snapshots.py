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

A snapshot holds nothing that reaches outside it, or later evidence would
leak into it: a symlink in a copied tree must be relative and stay inside
that tree (a bundle's ``CLAUDE.md -> AGENTS.md`` is fine; ``.git ->
/live/repo/.git`` is refused), the clone's ``.git`` must be its own
directory, and a clone that borrows objects (``--shared``: Git alternates)
is repacked to own them and the borrowing is removed.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
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
from ..contracts.improver_tournament import SNAPSHOT_MANIFEST, FrozenSnapshot, require_slug
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
        manifest = self._root / require_slug(snapshot_id, "a snapshot id") / SNAPSHOT_MANIFEST
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
        target = self._root / require_slug(snapshot_id, "a snapshot id")
        if target.exists():
            raise SnapshotUnavailable(f"snapshot {snapshot_id!r} exists; a snapshot is never changed")
        if (state_dir is None) != (clone is None):
            raise SnapshotUnavailable("a toolbox needs both the store copies and the repository clone")
        target.mkdir(parents=True)
        try:
            data = target / IMPROVER_DATA_DIRNAME
            self._copy(improver_data, data)
            require_self_contained(data)
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
            # Published last, whole or not at all: a snapshot exists once its manifest does.
            handle, temporary = tempfile.mkstemp(dir=target, prefix=".snapshot-")
            with os.fdopen(handle, "w", encoding="utf-8") as out:
                out.write(snapshot.model_dump_json(indent=2) + "\n")
            os.replace(temporary, target / SNAPSHOT_MANIFEST)
        except BaseException:
            shutil.rmtree(target, ignore_errors=True)
            raise
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
        if not (clone / ".git").is_dir():
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
        self._freeze_clone(clone, toolbox / TOOLBOX_REPO_DIRNAME, taken_at)
        sources.append(ToolboxSource(path=TOOLBOX_REPO_DIRNAME, staged=True, detail=f"clone, as of {taken_at.isoformat()}"))
        manifest = ToolboxManifest(audited_repo=repo, staged_at=taken_at, sources=tuple(sources))
        (toolbox / TOOLBOX_MANIFEST).write_text(manifest.model_dump_json(indent=2) + "\n", encoding="utf-8")

    def _freeze_clone(self, clone: Path, target: Path, taken_at: datetime) -> None:
        """A copy of ``clone`` that owns everything it shows, and shows
        nothing after ``taken_at``: only objects its refs reach are kept
        (reflogs expired, repacked, pruned), and a ref reaching a commit
        made after the snapshot refuses it (a clone taken later than the
        staged inputs would show the arms what was done since). Its
        working tree is rebuilt from its commit, so no file edited or
        added after the commit stays in it."""
        self._copy(clone, target)
        git = target / ".git"
        if git.is_symlink() or not git.is_dir():
            raise SnapshotUnavailable(f"{clone}: .git is not the clone's own directory")
        if (git / "commondir").exists():
            raise SnapshotUnavailable(f"{clone} is a linked worktree; freeze a clone")
        require_self_contained(target)
        if self._runner.run(["git", "-C", str(target), "rev-parse", "--verify", "-q", "HEAD"], timeout_seconds=60).returncode:
            raise SnapshotUnavailable(f"{clone} has no commit to freeze")
        self._git(target, "reflog", "expire", "--expire=now", "--all")
        self._git(target, "repack", "-a", "-d", "-q")
        (git / "objects" / "info" / "alternates").unlink(missing_ok=True)
        self._git(target, "prune", "--expire=now")
        self._git(target, "fsck", "--connectivity-only", "--no-progress")
        later = self._git(target, "rev-list", "--all", f"--since={taken_at.isoformat()}").split()
        if later:
            raise SnapshotUnavailable(
                f"{clone} reaches {len(later)} commit(s) made after the snapshot ({taken_at.isoformat()}),"
                f" e.g. {later[0][:12]}; freeze a clone taken with the inputs"
            )
        self._git(target, "reset", "--hard", "-q", "HEAD")
        self._git(target, "clean", "-ffdxq")
        # The rebuilt checkout is the commit's: a link it commits must stay inside too.
        require_self_contained(target)

    def _git(self, repo: Path, *args: str) -> str:
        done = self._runner.run(["git", "-C", str(repo), *args], timeout_seconds=1800)
        if done.returncode:
            raise SnapshotUnavailable(f"git {args[0]} in the frozen clone failed: {done.stderr.strip()}")
        return done.stdout

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


def require_self_contained(tree: Path) -> None:
    """Refuse a symlink in ``tree`` that is absolute or leaves ``tree``."""
    root = os.path.normpath(tree)
    for directory, dirnames, filenames in os.walk(tree):
        for name in (*dirnames, *filenames):
            path = os.path.join(directory, name)
            if not os.path.islink(path):
                continue
            target = os.readlink(path)
            resolved = os.path.normpath(os.path.join(directory, target))
            if os.path.isabs(target) or os.path.commonpath([root, resolved]) != root:
                raise SnapshotUnavailable(
                    f"{os.path.relpath(path, root)} links outside the snapshot ({target}); a snapshot is self-contained"
                )


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
    "require_self_contained",
    "upgrade_staged_inputs",
]
