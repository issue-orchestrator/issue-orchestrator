"""MCP server for issue-orchestrator control and data access."""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

from ..infra import supervisor
from ..infra.config import Config, get_config_path
from ..execution.orchestrator_http_api import OrchestratorHttpApi
from ..ports.orchestrator_api import OrchestratorApi

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class McpSettings:
    repo_root: Path
    config_path: Path
    instance_id: str | None
    host: str
    auto_start: bool


class OrchestratorHttpClient:
    def __init__(self, settings: McpSettings) -> None:
        self._settings = settings
        self._client = httpx.Client(timeout=10.0)
        self._cached_port: int | None = None

    def _status(self) -> supervisor.SupervisorStatus:
        return supervisor.status(self._settings.repo_root, instance_id=self._settings.instance_id)

    def _start(self) -> supervisor.SupervisorStatus:
        lock = supervisor.start(
            self._settings.repo_root,
            config_name=self._settings.config_path.name,
            instance_id=self._settings.instance_id,
        )
        self._cached_port = lock.http_port
        return supervisor.status(self._settings.repo_root, instance_id=self._settings.instance_id)

    def _ensure_running(self) -> supervisor.SupervisorStatus:
        status = self._status()
        if status.state != "running":
            if not self._settings.auto_start:
                raise RuntimeError("Orchestrator not running")
            status = self._start()
        if status.state != "running":
            raise RuntimeError(f"Orchestrator not running (state={status.state})")
        if status.port is None:
            raise RuntimeError("Orchestrator running but no port available")
        return status

    def base_url(self) -> str:
        if self._cached_port:
            return f"http://{self._settings.host}:{self._cached_port}"
        status = self._ensure_running()
        self._cached_port = status.port
        return f"http://{self._settings.host}:{status.port}"

    def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        url = f"{self.base_url()}{path}"
        response = self._client.request(method, url, **kwargs)
        response.raise_for_status()
        return response.json()

    def close(self) -> None:
        self._client.close()

    @property
    def http_client(self) -> httpx.Client:
        return self._client


mcp = FastMCP("Issue Orchestrator", json_response=True)


def _make_client(settings: McpSettings) -> OrchestratorHttpClient:
    return OrchestratorHttpClient(settings)


@mcp.tool(name="orchestrator.status")
def orchestrator_status() -> dict[str, Any]:
    """Return supervisor status and live orchestrator status."""
    status = _CLIENT._status()
    result: dict[str, Any] = {"supervisor": status.to_dict()}
    if status.state == "running" and status.port is not None:
        result["status"] = _API.status()
        result["info"] = _API.info()
    return result


@mcp.tool(name="orchestrator.start")
def orchestrator_start() -> dict[str, Any]:
    """Start the orchestrator process if not running."""
    status = _CLIENT._status()
    if status.state != "running":
        status = _CLIENT._start()
    return {"supervisor": status.to_dict()}


@mcp.tool(name="orchestrator.stop")
def orchestrator_stop(force: bool = False) -> dict[str, Any]:
    """Stop the orchestrator process."""
    stopped = supervisor.stop(
        _SETTINGS.repo_root,
        instance_id=_SETTINGS.instance_id,
        force=force,
    )
    return {"stopped": stopped}


@mcp.tool(name="orchestrator.pause")
def orchestrator_pause() -> dict[str, Any]:
    """Pause the orchestrator."""
    return _API.pause()


@mcp.tool(name="orchestrator.resume")
def orchestrator_resume() -> dict[str, Any]:
    """Resume the orchestrator."""
    return _API.resume()


@mcp.tool(name="orchestrator.refresh")
def orchestrator_refresh(inflight_stable_ids: list[str] | None = None) -> dict[str, Any]:
    """Request an immediate refresh of issues."""
    return _API.refresh(inflight_stable_ids or [])


@mcp.tool(name="orchestrator.shutdown")
def orchestrator_shutdown(force: bool = False) -> dict[str, Any]:
    """Request orchestrator shutdown (graceful by default)."""
    return _API.shutdown(force=force)


@mcp.tool(name="orchestrator.snapshot")
def orchestrator_snapshot() -> dict[str, Any]:
    """Return a combined snapshot of orchestrator state for UI consumption."""
    return {
        "status": _API.status(),
        "info": _API.info(),
        "blocked": _API.blocked_issues(),
        "stale": _API.stale_issues(),
        "dependency_problems": _API.dependency_problems(),
        "excluded": _API.excluded_issues(),
        "publish_jobs": _API.publish_jobs(),
        "history": _API.history(),
    }


@mcp.tool(name="orchestrator.session.worktree")
def orchestrator_session_worktree(issue_number: int) -> dict[str, Any]:
    """Return worktree path and session name for an issue."""
    return _API.session_worktree(issue_number)


@mcp.tool(name="orchestrator.session.manifest")
def orchestrator_session_manifest(issue_number: int) -> dict[str, Any]:
    """Return session manifest for an issue."""
    return _API.session_manifest(issue_number)


@mcp.tool(name="orchestrator.session.phases")
def orchestrator_session_phases(issue_number: int) -> dict[str, Any]:
    """Return session phase history for an issue."""
    return _API.session_phases(issue_number)


@mcp.tool(name="orchestrator.session.claude_log")
def orchestrator_session_claude_log(issue_number: int, limit: int = 200) -> dict[str, Any]:
    """Return parsed Claude log entries for an issue."""
    return _API.session_claude_log(issue_number, limit)


@mcp.tool(name="orchestrator.session.orchestrator_log")
def orchestrator_session_orchestrator_log(issue_number: int) -> dict[str, Any]:
    """Return filtered orchestrator log paths for an issue."""
    return _API.session_orchestrator_log(issue_number)


@mcp.tool(name="orchestrator.session.send")
def orchestrator_session_send(issue_number: int, text: str) -> dict[str, Any]:
    """Send input to a running agent session."""
    return _API.send(issue_number, text)


@mcp.tool(name="orchestrator.session.kill")
def orchestrator_session_kill(issue_number: int) -> dict[str, Any]:
    """Kill a running session."""
    return _API.kill(issue_number)


@mcp.tool(name="orchestrator.session.focus")
def orchestrator_session_focus(issue_number: int) -> dict[str, Any]:
    """Focus a running session in the terminal backend."""
    return _API.focus(issue_number)


@mcp.tool(name="orchestrator.urls")
def orchestrator_urls() -> dict[str, Any]:
    """Return base URLs for the web dashboard and event stream."""
    base = _CLIENT.base_url()
    return {
        "base_url": base,
        "dashboard_url": f"{base}/",
        "events_url": f"{base}/api/events",
        "config_url": f"{base}/api/config",
    }


@mcp.tool(name="orchestrator.doctor")
def orchestrator_doctor() -> dict[str, Any]:
    """Return orchestrator doctor report."""
    return _API.doctor()


def _resolve_settings(args: argparse.Namespace) -> McpSettings:
    repo_root = Path(args.repo_root or Path.cwd())
    if args.config_path:
        config_path = Path(args.config_path)
    else:
        config_path = get_config_path(repo_root, args.config_name)
    if not config_path.exists():
        raise FileNotFoundError(f"Config file not found: {config_path}")
    return McpSettings(
        repo_root=repo_root,
        config_path=config_path,
        instance_id=args.instance_id,
        host=args.host,
        auto_start=args.auto_start,
    )


def _resolve_repo_root(config_path: Path) -> Path:
    config = Config.load(config_path)
    if not config.repo_root:
        raise ValueError("repo_root missing in config")
    return config.repo_root


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Issue Orchestrator MCP server")
    parser.add_argument("--repo-root", help="Repository root path")
    parser.add_argument("--config", dest="config_path", help="Path to config file")
    parser.add_argument("--config-name", default="default.yaml", help="Config filename (default.yaml)")
    parser.add_argument("--instance-id", help="Instance ID for multi-orchestrator setups")
    parser.add_argument("--host", default="127.0.0.1", help="Host for web API calls")
    parser.add_argument("--auto-start", action="store_true", help="Start orchestrator if not running")
    return parser


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    parser = build_arg_parser()
    args = parser.parse_args()

    global _SETTINGS
    global _CLIENT

    if args.config_path and not args.repo_root:
        repo_root = _resolve_repo_root(Path(args.config_path))
        args.repo_root = str(repo_root)

    _SETTINGS = _resolve_settings(args)
    _CLIENT = _make_client(_SETTINGS)
    global _API
    _API = OrchestratorHttpApi(_CLIENT.base_url, _CLIENT.http_client)

    try:
        mcp.run()
    finally:
        _CLIENT.close()


_SETTINGS: McpSettings
_CLIENT: OrchestratorHttpClient
_API: OrchestratorApi


if __name__ == "__main__":
    main()
