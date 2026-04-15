"""Shared Control Center runtime and identity helpers."""

from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

from ..execution.git_working_copy import GitWorkingCopy
from ..infra.repo_identity import (
    RepoIdentity,
    build_repo_identity_with_status,
    diff_repo_identity,
)

LOCK_HEARTBEAT_UNRESPONSIVE_SECONDS = 45


def build_repo_identity(repo_root: Path) -> RepoIdentity:
    """Build repo identity with execution-layer git status resolution."""
    git = GitWorkingCopy()

    def _resolve_repo_status(root: Path) -> tuple[str | None, list[str]]:
        branch: str | None = None
        try:
            branch = git.get_current_branch(root)
        except Exception:
            branch = None
        dirty_lines = git.get_status_porcelain_lines(root)
        return branch, dirty_lines

    return build_repo_identity_with_status(repo_root, status_resolver=_resolve_repo_status)


def get_selected_config(repo_root: Path) -> str:
    """Return the selected config name for a repo, defaulting to default.yaml."""
    from ..infra.repo_registry import load_registry

    registry = load_registry()
    normalized = str(repo_root.resolve())
    for repo in registry.repos:
        if repo.path == normalized:
            return repo.selected_config or "default.yaml"
    return "default.yaml"


def _load_config_port(repo_root: Path, config_name: str | None) -> int | None:
    """Load the web port from a repo config."""
    from ..infra.config import Config, get_config_path

    config_path = get_config_path(repo_root, config_name or "default.yaml")
    if not config_path.exists():
        return None
    try:
        config = Config.load(config_path)
    except Exception:
        return None
    return config.web_port


def client_dashboard_url(port: int | None) -> str | None:
    """Resolve the browser-facing dashboard URL for a repo engine port."""
    if port is None or port == 0:
        return None

    from ..infra.client_urls import resolve_client_dashboard_url

    return resolve_client_dashboard_url(port)


def detect_orchestrator_by_port(
    repo_root: Path,
    config_name: str | None,
    *,
    expected_identity: RepoIdentity | None = None,
) -> dict[str, Any] | None:
    """Detect an orchestrator by probing the configured port."""
    port = _load_config_port(repo_root, config_name)
    if not port:
        return None

    base_url = f"http://127.0.0.1:{port}"
    info = _read_json(f"{base_url}/api/info", timeout=0.6)
    if info is None or info.get("repo_root") != str(repo_root):
        return None

    details: dict[str, Any] = {"port": port, "info": info}
    annotate_identity_mismatch(details, info, expected_identity)
    _annotate_orchestrator_health(details, base_url)
    return details


def annotate_identity_mismatch(
    details: dict[str, Any],
    info: dict[str, Any],
    expected_identity: RepoIdentity | None,
) -> None:
    """Attach identity drift details when the observed engine differs."""
    if expected_identity is None:
        return
    observed_identity_payload = info.get("repo_identity")
    if not isinstance(observed_identity_payload, dict):
        return
    observed_identity = RepoIdentity(
        repo_root=str(observed_identity_payload.get("repo_root", "")),
        commit_sha=(
            str(observed_identity_payload["commit_sha"])
            if observed_identity_payload.get("commit_sha")
            else None
        ),
        branch=(
            str(observed_identity_payload["branch"])
            if observed_identity_payload.get("branch")
            else None
        ),
        working_tree_dirty=bool(observed_identity_payload.get("working_tree_dirty", False)),
        dirty_fingerprint=(
            str(observed_identity_payload["dirty_fingerprint"])
            if observed_identity_payload.get("dirty_fingerprint")
            else None
        ),
        source_root=(
            str(observed_identity_payload["source_root"])
            if observed_identity_payload.get("source_root")
            else None
        ),
    )
    identity_mismatch = diff_repo_identity(expected_identity, observed_identity)
    for volatile_field in ("working_tree_dirty", "dirty_fingerprint"):
        identity_mismatch.pop(volatile_field, None)
    if identity_mismatch:
        details["identity_mismatch"] = identity_mismatch
        details["observed_identity"] = observed_identity.to_dict()
        details["expected_identity"] = expected_identity.to_dict()


def _annotate_orchestrator_health(details: dict[str, Any], base_url: str) -> None:
    status_data = _read_json(f"{base_url}/api/status", timeout=0.6)
    if status_data is None:
        details.setdefault("health", "unknown")
        return

    details["status"] = status_data
    last_tick = status_data.get("last_tick_time")
    if not isinstance(last_tick, (int, float)) or last_tick <= 0:
        return
    tick_age = time.time() - last_tick
    details["tick_age_seconds"] = tick_age
    details["health"] = "stale" if tick_age > 120 else "ok"


def confirm_orchestrator_at_port(repo_root: Path, port: int) -> bool:
    """Confirm the orchestrator at a port belongs to the repo_root."""
    info = _read_json(f"http://127.0.0.1:{port}/api/info", timeout=0.6)
    return info is not None and info.get("repo_root") == str(repo_root)


def is_shutdown_complete(port: int | None) -> bool:
    """Check if an orchestrator is in shutdown-complete state."""
    if not port:
        return False
    data = _read_json(f"http://127.0.0.1:{port}/api/status", timeout=2.0)
    if data is None:
        return False
    shutdown_requested = data.get("shutdown_requested", False)
    active_sessions = data.get("active_sessions", [])
    return shutdown_requested and len(active_sessions) == 0


def _read_json(url: str, *, timeout: float) -> dict[str, Any] | None:
    try:
        with urlopen(url, timeout=timeout) as response:
            if getattr(response, "status", None) != 200:
                return None
            payload = response.read().decode("utf-8", errors="replace")
    except (HTTPError, OSError, URLError, ValueError):
        return None

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def heartbeat_age_seconds(iso_timestamp: str | None) -> int | None:
    """Return heartbeat age in seconds for an ISO timestamp."""
    if not iso_timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))


def enrich_runtime_health(
    repo_path: Path,
    status_payload: dict[str, Any] | None,
    *,
    orphaned: bool = False,
    instance_id: str | None = None,
) -> dict[str, Any] | None:
    """Annotate runtime payloads with lock heartbeat and health state."""
    if status_payload is None:
        return None

    from ..infra.repo_lock import read_lock

    lock_info = read_lock(repo_path, instance_id=instance_id)
    last_heartbeat_at = lock_info.last_heartbeat_at if lock_info is not None else None
    heartbeat_age = heartbeat_age_seconds(last_heartbeat_at)
    status_payload["last_heartbeat_at"] = last_heartbeat_at
    status_payload["heartbeat_age_seconds"] = heartbeat_age

    if orphaned:
        status_payload["runtime_health"] = "orphaned"
        return status_payload

    state = status_payload.get("state")
    if state == "failed":
        status_payload["runtime_health"] = "stale_lock"
        return status_payload
    if (
        state == "running"
        and heartbeat_age is not None
        and heartbeat_age > LOCK_HEARTBEAT_UNRESPONSIVE_SECONDS
    ):
        status_payload["runtime_health"] = "unresponsive"
        status_payload["unresponsive"] = True
        return status_payload
    if state == "running":
        status_payload["runtime_health"] = "healthy"
        status_payload["unresponsive"] = False
        return status_payload
    status_payload["runtime_health"] = "not_running"
    return status_payload


__all__ = [
    "LOCK_HEARTBEAT_UNRESPONSIVE_SECONDS",
    "annotate_identity_mismatch",
    "build_repo_identity",
    "client_dashboard_url",
    "confirm_orchestrator_at_port",
    "detect_orchestrator_by_port",
    "enrich_runtime_health",
    "get_selected_config",
    "heartbeat_age_seconds",
    "is_shutdown_complete",
]
