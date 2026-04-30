"""Build Control Center repository status payloads."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ..execution.control_center_runtime import (
    client_dashboard_url,
    detect_orchestrator_by_port,
    enrich_runtime_health,
)
from ..execution.orchestrator_http_api import probe_orchestrator_json
from ..infra.supervisor import SupervisorOps

if TYPE_CHECKING:
    from ..infra.repo_registry import RegisteredRepo

logger = logging.getLogger(__name__)


def build_repos_status(
    *,
    supervisor: SupervisorOps,
    preferred_repo_root: Path | None,
) -> list[dict[str, Any]]:
    """Build status payloads for registered repositories."""
    from ..infra import repo_registry
    from ..infra.config import list_configs

    preferred_repo = str(preferred_repo_root) if preferred_repo_root else None
    repos = repo_registry.list_repos()
    if preferred_repo and all(repo.path != preferred_repo for repo in repos):
        try:
            repo_registry.add_repo(preferred_repo)
            repos = repo_registry.list_repos()
        except ValueError:
            repos = repo_registry.list_repos()

    if preferred_repo:
        repos = sorted(repos, key=lambda repo: 0 if repo.path == preferred_repo else 1)

    cwd = Path.cwd().resolve()
    result: list[dict[str, Any]] = []

    for repo in repos:
        path = Path(repo.path)
        path_resolved = path.resolve() if path.exists() else path
        expected_instances = _expected_instances_for_repo(path, repo.selected_config)
        available_configs = list_configs(path) if path.exists() else []
        repo_data: dict[str, Any] = {
            "path": repo.path,
            "name": repo.name,
            "added_at": repo.added_at,
            "exists": path.exists(),
            "is_current_dir": path_resolved == cwd,
            "configs": available_configs,
            "selected_config": repo.selected_config,
            "expected_instances": expected_instances,
        }

        if expected_instances > 1 and path.exists():
            _populate_multi_instance_status(
                repo_data=repo_data,
                repo=repo,
                repo_path=path,
                expected_instances=expected_instances,
                supervisor=supervisor,
            )
        else:
            _populate_single_instance_status(
                repo_data=repo_data,
                repo=repo,
                repo_path=path,
                supervisor=supervisor,
            )

        repo_data["dashboard_url"] = client_dashboard_url((repo_data.get("status") or {}).get("port"))
        repo_data["health"] = repo.health.to_dict() if repo.health else None
        result.append(repo_data)

    return result


def _expected_instances_for_repo(
    repo_path: Path,
    selected_config: str | None,
) -> int:
    """Return configured instance count, falling back to single-instance mode."""
    from ..infra.config import Config, get_config_path

    if not repo_path.exists() or not selected_config:
        return 1
    try:
        config_path = get_config_path(repo_path, selected_config)
        if not config_path.exists():
            return 1
        config = Config.load(config_path)
    except Exception:
        logger.debug(
            "Falling back to single-instance mode for repo=%s config=%s",
            repo_path,
            selected_config,
            exc_info=True,
        )
        return 1
    return config.instances


def _populate_multi_instance_status(
    *,
    repo_data: dict[str, Any],
    repo: RegisteredRepo,
    repo_path: Path,
    expected_instances: int,
    supervisor: SupervisorOps,
) -> None:
    """Attach status payloads for a multi-instance repository."""
    multi_status = supervisor.status_all_instances(repo_path)
    repo_data["instances"] = []

    for instance_status in multi_status.instances:
        instance_data = instance_status.to_dict()
        if instance_status.state == "running" and instance_status.port:
            _apply_internal_runtime_state(instance_data, instance_status.port)

        enriched_instance = enrich_runtime_health(
            repo_path,
            instance_data,
            instance_id=instance_data.get("instance_id"),
        )
        resolved_instance = enriched_instance or instance_data
        resolved_instance["dashboard_url"] = client_dashboard_url(resolved_instance.get("port"))
        repo_data["instances"].append(resolved_instance)

    running_count = sum(1 for status in multi_status.instances if status.state == "running")
    if running_count == expected_instances:
        repo_data["status"] = {"state": "running", "running_count": running_count}
    elif running_count > 0:
        repo_data["status"] = {"state": "partial", "running_count": running_count}
    else:
        repo_data["status"] = {"state": "stopped", "running_count": 0}


def _populate_single_instance_status(
    *,
    repo_data: dict[str, Any],
    repo: RegisteredRepo,
    repo_path: Path,
    supervisor: SupervisorOps,
) -> None:
    """Attach status payloads for a single-instance repository."""
    status_info = supervisor.status(repo_path) if repo_path.exists() else None
    repo_data["status"] = enrich_runtime_health(
        repo_path,
        status_info.to_dict() if status_info else None,
    )

    if status_info and status_info.state != "running" and repo_path.exists():
        detected = detect_orchestrator_by_port(repo_path, repo.selected_config)
        if detected is not None:
            status_data = detected.get("status", {})
            orphaned_status = {
                "state": "running",
                "pid": None,
                "port": detected["port"],
                "started_at": None,
                "recovered": False,
                "error": None,
                "orphaned": True,
                "health": detected.get("health", "unknown"),
                "tick_age_seconds": detected.get("tick_age_seconds"),
                "shutdown_requested": status_data.get("shutdown_requested", False),
                "active_session_count": len(status_data.get("active_sessions", [])),
            }
            repo_data["status"] = enrich_runtime_health(
                repo_path,
                orphaned_status,
                orphaned=True,
            )

    if status_info and status_info.state == "running" and status_info.port:
        status_payload = repo_data.get("status")
        if isinstance(status_payload, dict):
            _apply_internal_runtime_state(status_payload, status_info.port)


def _apply_internal_runtime_state(status_payload: dict[str, Any], port: int) -> None:
    """Attach best-effort internal runtime fields from the orchestrator API."""
    internal = probe_orchestrator_json(
        f"http://127.0.0.1:{port}/api/status",
        timeout_seconds=2.0,
    )
    if internal is None:
        return

    status_payload["paused"] = internal.get("paused", False)
    status_payload["shutdown_requested"] = internal.get("shutdown_requested", False)
    active_sessions = internal.get("active_sessions", [])
    status_payload["active_session_count"] = len(active_sessions)
    status_payload["e2e_role"] = internal.get("e2e_role")
    # The CC frontend uses startup_status to keep the Open button in an
    # "Initializing…" state until the engine has finished its first
    # GitHub fetch + reconcile. Without it, opening mid-startup shows
    # SSE-driven UI updates as visible flashes.
    status_payload["startup_status"] = internal.get("startup_status")


__all__ = ["build_repos_status"]
