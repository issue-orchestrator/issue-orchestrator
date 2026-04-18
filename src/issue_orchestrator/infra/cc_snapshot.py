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
import logging
import shutil
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

SNAPSHOT_DIR_NAME = ".control-center-snapshot"


def snapshot_root(repo_root: Path) -> Path:
    """Return the snapshot parent directory inside ``repo_root``."""
    return repo_root / SNAPSHOT_DIR_NAME


def clean_snapshots(repo_root: Path) -> list[Path]:
    """Remove every snapshot dir under ``repo_root``.

    The caller (the CC launch script) must have already killed every
    running CC before invoking this; any surviving snapshot dir is
    therefore an orphan from a previous CC that was shut down or
    crashed. Returns the list of removed paths for logging.
    """
    root = snapshot_root(repo_root)
    if not root.exists():
        return []

    removed: list[Path] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        shutil.rmtree(entry, ignore_errors=True)
        removed.append(entry)
    return removed


def create_snapshot(repo_root: Path, *, now: float | None = None) -> Path:
    """Copy ``repo_root/src`` into a fresh snapshot dir; return its path.

    The snapshot dir is named by a monotonically-increasing launch id so
    concurrent launches from the same repo root don't clobber each
    other (though ``stop_all_orchestrators`` makes that concurrency
    very unlikely in practice).
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
    shutil.copytree(src, snapshot_dir / "src")
    return snapshot_dir


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
