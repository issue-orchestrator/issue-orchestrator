"""Typed run-asset contract for orchestrator-managed completion commands."""

from __future__ import annotations

import json
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NoReturn

from ...domain.session_run import SessionRunAssets
from ...infra.env import ENV_PREFIX, get_env


def require_orchestrator_run_assets_for_session(
    worktree_root: Path,
    session_id: str,
) -> SessionRunAssets:
    """Load the owner-injected run assets for an orchestrated session.

    Active orchestrator-managed completion is not allowed to rediscover a run
    directory. The session owner must inject ``ISSUE_ORCHESTRATOR_RUN_DIR`` and
    the manifest in that directory must prove the requested session identity.
    """
    run_dir_value = get_env("RUN_DIR")
    if not run_dir_value:
        _die(f"{ENV_PREFIX}RUN_DIR is required for orchestrator-managed validation")

    run_dir = Path(run_dir_value).expanduser().resolve()
    if not run_dir.is_dir():
        _die(f"{ENV_PREFIX}RUN_DIR does not exist: {run_dir}")

    manifest_path = run_dir / "manifest.json"
    if not manifest_path.is_file():
        _die(f"{ENV_PREFIX}RUN_DIR is missing manifest.json: {run_dir}")

    manifest = _load_manifest(manifest_path)

    try:
        assets = SessionRunAssets.from_manifest_payload(
            run_dir=run_dir,
            manifest=manifest,
        )
    except (TypeError, ValueError) as exc:
        _die(f"{ENV_PREFIX}RUN_DIR manifest is invalid: {exc}")

    if assets.worktree_path.resolve() != worktree_root.resolve():
        _die(
            f"{ENV_PREFIX}RUN_DIR belongs to worktree "
            f"{assets.worktree_path}, expected {worktree_root}"
        )

    if assets.session_name != session_id:
        _die(
            f"{ENV_PREFIX}RUN_DIR belongs to '{assets.session_name}', "
            f"expected '{session_id}'"
        )

    return assets


def _load_manifest(manifest_path: Path) -> Mapping[str, Any]:
    try:
        raw_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except OSError as exc:
        _die(f"{ENV_PREFIX}RUN_DIR manifest cannot be read: {manifest_path}: {exc}")
    except json.JSONDecodeError as exc:
        _die(f"{ENV_PREFIX}RUN_DIR manifest is invalid JSON: {manifest_path}: {exc}")
    if not isinstance(raw_manifest, dict):
        _die(f"{ENV_PREFIX}RUN_DIR manifest must be a JSON object: {manifest_path}")
    return raw_manifest


def _die(message: str) -> NoReturn:
    print(f"ERROR: {message}", file=sys.stderr)
    print("\nUse --help for usage information.", file=sys.stderr)
    raise SystemExit(1)
