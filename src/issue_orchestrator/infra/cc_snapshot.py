"""Frozen source snapshot for the Control Center's serving path.

## Why

The orchestrator is installed editable (``pip install -e .``) so every
subprocess (``coding-done``, ``reviewer-done``, hooks) reads from the
base repo's ``src/`` *right now*, not from the state at CC launch.
``start_control_center.sh`` verifies the base repo is on ``main`` and
clean — but only once, at launch. Nothing re-checks. If anything (a
dev, another Claude Code session, a tool) switches the base repo to a
different branch afterwards, every subsequent agent call silently
reads the new branch's code.

That is the chain that burned a 90-minute session in tixmeup-243: the
planted-paths fix landed on ``main``, the user pulled, restarted the
CC — but the base repo was then switched to a polish-version branch
without the fix, and every ``coding-done`` call inside the live agent
worktrees read the pre-fix code via the editable install.

## What this module does

On CC launch the shell script invokes ``python -m
issue_orchestrator.infra.cc_snapshot create --root <repo>`` which:

1. Cleans up snapshot dirs owned by previous CCs (which ``stop_all_orchestrators``
   has already killed, so they are all orphans).
2. Copies ``<repo>/src`` into
   ``<repo>/.control-center-snapshot/<launch_id>/src``.
3. Prints the snapshot path on stdout; the shell script captures it
   and prepends it to ``PYTHONPATH``.

Because ``PYTHONPATH`` entries precede site-packages in ``sys.path``,
every Python process (the CC itself and every subprocess that
inherits its env) imports ``issue_orchestrator`` from the frozen
snapshot regardless of what happens to the base repo afterwards.

## Scope of the fix

This module owns only the *serving* path. Developer tooling (``pytest``,
``make validate``, interactive debugging) still uses the editable
install and sees live edits to the base repo — that workflow is
untouched.

## Cleanup

Each ``create`` invocation first deletes any existing snapshot dirs.
Rationale: ``scripts/start_control_center.sh`` always runs
``stop_all_orchestrators`` *before* ``create``; once that returns there
are no surviving CCs, so every prior snapshot is orphaned and can
safely be removed. This avoids PID tracking and keeps the directory
bounded at one snapshot per live CC.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import shutil
import sys
import time
from pathlib import Path

from .repo_identity import get_repo_head_sha

logger = logging.getLogger(__name__)

SNAPSHOT_DIR_NAME = ".control-center-snapshot"
SOURCE_METADATA_FILE = "source-metadata.json"
SOURCE_METADATA_SCHEMA_VERSION = 1
# Name of the file ``create_snapshot`` writes inside each snapshot dir
# recording the PID of the CC claiming ownership. ``clean_snapshots``
# skips dirs whose marker PID is still alive, so a stray ``cc_snapshot
# clean`` call cannot delete a running CC's frozen source out from
# under it. The marker is optional — a dir without one is treated as
# an orphan and cleaned, matching the historical behaviour.
OWNER_PID_MARKER = "cc.pid"

# Important caveat the caller should be aware of: the snapshot-creation
# code (this module) is loaded from the base repo's editable install at
# launch time, not from the snapshot it is about to create. Bugs in
# this module therefore land in every run until the base repo itself
# is updated. That is the correct semantics — "freeze at launch"
# freezes the application code, not the bootstrap — but worth stating
# so a future maintainer doesn't assume this file is self-protecting.


def snapshot_root(repo_root: Path) -> Path:
    """Return the snapshot parent directory inside ``repo_root``."""
    return repo_root / SNAPSHOT_DIR_NAME


def _pid_is_live(pid: int) -> bool:
    """Return True if ``pid`` names a live process.

    ``os.kill(pid, 0)`` is the POSIX test: signal 0 does nothing but
    still raises ``ProcessLookupError`` if the pid is absent and
    ``PermissionError`` if it names a process we don't own (still alive
    from our perspective).
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Process exists but is owned by another user. Treat as live
        # and skip cleanup — better to leak disk than to clobber a
        # running CC.
        return True
    except OSError:
        return False
    return True


def _owner_pid_for(dir_path: Path) -> int | None:
    marker = dir_path / OWNER_PID_MARKER
    if not marker.exists():
        return None
    try:
        return int(marker.read_text().strip())
    except (OSError, ValueError):
        return None


def clean_snapshots(repo_root: Path) -> list[Path]:
    """Remove snapshot dirs under ``repo_root`` that are not owned by a
    live CC.

    DANGER — the caller is expected to have run
    ``stop_all_orchestrators`` before calling this; that guarantees
    every surviving snapshot is an orphan. This function *also* checks
    ``cc.pid`` marker files as a second line of defence against a
    stray invocation (CI hook, manual ``python -m
    issue_orchestrator.infra.cc_snapshot create`` while a CC is
    running): any snapshot whose marker references a live process is
    skipped rather than deleted, so a running CC cannot have its
    frozen source ripped out from under it.

    Returns the list of removed paths for logging. Rmtree failures are
    reported on stderr rather than swallowed silently; a lingering
    snapshot that can't be deleted (permission error, busy file) will
    otherwise accumulate to fill the disk.
    """
    root = snapshot_root(repo_root)
    if not root.exists():
        return []

    removed: list[Path] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        owner_pid = _owner_pid_for(entry)
        if owner_pid is not None and _pid_is_live(owner_pid):
            print(
                f"cc-snapshot: skipping {entry} (owned by live PID {owner_pid})",
                file=sys.stderr,
            )
            continue
        try:
            shutil.rmtree(entry)
        except OSError as exc:
            print(
                f"cc-snapshot: failed to remove {entry}: {exc}",
                file=sys.stderr,
            )
            continue
        removed.append(entry)
    return removed


def create_snapshot(repo_root: Path, *, now: float | None = None) -> Path:
    """Copy ``repo_root/src`` into a fresh snapshot dir; return its path.

    The snapshot dir is named by a monotonically-increasing launch id so
    concurrent launches from the same repo root don't clobber each
    other (though ``stop_all_orchestrators`` makes that concurrency
    very unlikely in practice). The snapshot also carries source
    metadata so the frozen Control Center can report the commit it was
    launched from without consulting the mutable base checkout later.
    """
    src = repo_root / "src"
    if not src.is_dir():
        raise FileNotFoundError(
            f"Source tree not found at {src}; refusing to create a "
            "control-center snapshot without a src/ tree to freeze."
        )

    root = snapshot_root(repo_root)
    root.mkdir(exist_ok=True)

    stamp = int((now if now is not None else time.time()) * 1000)
    snapshot_dir = root / f"launch-{stamp}"
    # Guard against an unlikely collision if two launches land in the
    # same millisecond (tests, rapid restart).
    suffix = 0
    while snapshot_dir.exists():
        suffix += 1
        snapshot_dir = root / f"launch-{stamp}-{suffix}"

    # Copy `src/` rather than symlink: the whole point is to be
    # immune to mutations of the base repo's working tree after this
    # moment.
    start = time.time()
    shutil.copytree(src, snapshot_dir / "src")
    _write_source_metadata(snapshot_dir, repo_root)
    duration_ms = int((time.time() - start) * 1000)
    size_bytes = _tree_size(snapshot_dir / "src")
    # Observability: a slow snapshot creation (cold SSD, network FS) is
    # invisible otherwise. stderr so the shell script's stdout capture
    # is unaffected.
    print(
        f"cc-snapshot: froze {size_bytes / (1024 * 1024):.1f} MB in {duration_ms}ms",
        file=sys.stderr,
    )
    return snapshot_dir


def _write_source_metadata(snapshot_dir: Path, repo_root: Path) -> None:
    metadata = {
        "schema_version": SOURCE_METADATA_SCHEMA_VERSION,
        "source_repo_root": str(repo_root.resolve()),
        "commit_sha": get_repo_head_sha(repo_root),
    }
    (snapshot_dir / SOURCE_METADATA_FILE).write_text(
        json.dumps(metadata, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _tree_size(path: Path) -> int:
    """Return cumulative size of regular files under ``path`` in bytes."""
    total = 0
    for entry in path.rglob("*"):
        if entry.is_file() and not entry.is_symlink():
            try:
                total += entry.stat().st_size
            except OSError:
                continue
    return total


def _main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m issue_orchestrator.infra.cc_snapshot",
        description=(
            "Create a frozen src/ snapshot for the Control Center so "
            "subsequent base-repo branch changes cannot leak into "
            "running agent sessions."
        ),
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create_parser = sub.add_parser(
        "create",
        help=(
            "Clean previous snapshots, then create a new one. "
            "Prints the snapshot PYTHONPATH entry on stdout."
        ),
    )
    create_parser.add_argument(
        "--root",
        required=True,
        type=Path,
        help="Base repository root (the editable install target).",
    )

    clean_parser = sub.add_parser(
        "clean",
        help="Remove all snapshot directories (independent of create).",
    )
    clean_parser.add_argument("--root", required=True, type=Path)

    args = parser.parse_args(argv)
    root: Path = args.root.resolve()

    if args.command == "create":
        removed = clean_snapshots(root)
        for path in removed:
            print(f"cc-snapshot: removed orphan {path}", file=sys.stderr)
        snapshot_dir = create_snapshot(root)
        # stdout is the machine-readable contract with the shell script:
        # the path that should be prepended to PYTHONPATH.
        print(snapshot_dir / "src")
        print(
            f"cc-snapshot: created {snapshot_dir} (source frozen from {root}/src)",
            file=sys.stderr,
        )
        return 0

    if args.command == "clean":
        removed = clean_snapshots(root)
        for path in removed:
            print(f"cc-snapshot: removed {path}", file=sys.stderr)
        return 0

    return 2  # unreachable — argparse enforces `required=True`


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    sys.exit(_main())
