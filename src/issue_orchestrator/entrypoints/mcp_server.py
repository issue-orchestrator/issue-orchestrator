"""MCP server for issue-orchestrator control and data access."""

from __future__ import annotations

import argparse
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

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
        self._cached_port: int | None = None

    def status(self) -> supervisor.SupervisorStatus:
        return supervisor.status(self._settings.repo_root, instance_id=self._settings.instance_id)

    def start(self) -> supervisor.SupervisorStatus:
        from ..infra.launcher import launch_subprocess

        config = Config.load(self._settings.config_path)
        result = launch_subprocess(
            repo_root=self._settings.repo_root,
            config=config,
            config_name=self._settings.config_path.name,
            instance_id=self._settings.instance_id,
        )
        if not result.launched:
            raise RuntimeError(
                f"Failed to start orchestrator: {result.status}"
                + (f" — {result.error}" if result.error else "")
            )
        if result.supervisor and "port" in result.supervisor:
            self._cached_port = result.supervisor["port"]
        return supervisor.status(self._settings.repo_root, instance_id=self._settings.instance_id)

    def _ensure_running(self) -> supervisor.SupervisorStatus:
        status = self.status()
        if status.state != "running":
            if not self._settings.auto_start:
                raise RuntimeError("Orchestrator not running")
            status = self.start()
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

    def doctor_url(self) -> str | None:
        if self._cached_port:
            return f"http://{self._settings.host}:{self._cached_port}/api/doctor"
        status = self.status()
        if status.state == "running" and status.port:
            return f"http://{self._settings.host}:{status.port}/api/doctor"
        return None

    def update_port(self, port: int) -> None:
        """Update the cached port after an external launch."""
        self._cached_port = port

    def close(self) -> None:
        return None


class McpApp:
    def __init__(self, settings: McpSettings) -> None:
        self._settings = settings
        self._client = OrchestratorHttpClient(settings)
        self._api: OrchestratorApi = OrchestratorHttpApi(self._client.base_url)

    def close(self) -> None:
        self._client.close()
        self._api.close()

    def _safe(self, tool_name: str, fn: Callable[[], dict[str, Any]]) -> dict[str, Any]:
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            logger.exception("MCP tool %s failed", tool_name)
            return {
                "error": {
                    "message": str(exc),
                    "type": exc.__class__.__name__,
                }
            }

    def register(self, server: FastMCP) -> None:
        server.tool(name="orchestrator.status")(self.tool_status)
        server.tool(name="orchestrator.start")(self.tool_start)
        server.tool(name="orchestrator.stop")(self.tool_stop)
        server.tool(name="orchestrator.pause")(self.tool_pause)
        server.tool(name="orchestrator.resume")(self.tool_resume)
        server.tool(name="orchestrator.refresh")(self.tool_refresh)
        server.tool(name="orchestrator.shutdown")(self.tool_shutdown)
        server.tool(name="orchestrator.snapshot")(self.tool_snapshot)
        server.tool(name="orchestrator.session.worktree")(self.tool_session_worktree)
        server.tool(name="orchestrator.session.manifest")(self.tool_session_manifest)
        server.tool(name="orchestrator.session.phases")(self.tool_session_phases)
        server.tool(name="orchestrator.session.claude_log")(self.tool_session_claude_log)
        server.tool(name="orchestrator.session.orchestrator_log")(self.tool_session_orchestrator_log)
        server.tool(name="orchestrator.session.send")(self.tool_session_send)
        server.tool(name="orchestrator.session.kill")(self.tool_session_kill)
        server.tool(name="orchestrator.session.focus")(self.tool_session_focus)
        server.tool(name="orchestrator.urls")(self.tool_urls)
        server.tool(name="orchestrator.doctor")(self.tool_doctor)
        # Unified dashboard tools
        server.tool(name="orchestrator.state")(self.tool_state)
        server.tool(name="orchestrator.repos")(self.tool_repos)
        server.tool(name="orchestrator.repos.start")(self.tool_repos_start)
        server.tool(name="orchestrator.repos.stop")(self.tool_repos_stop)

    def tool_status(self) -> dict[str, Any]:
        return self._safe("orchestrator.status", self.status)

    def tool_start(self) -> dict[str, Any]:
        try:
            return self.start()
        except Exception as exc:  # noqa: BLE001
            logger.exception("MCP tool orchestrator.start failed")
            ui_hint: dict[str, Any] = {"kind": "doctor"}
            doctor_url = self._client.doctor_url()
            if doctor_url:
                ui_hint["url"] = doctor_url
            return {
                "error": {
                    "message": str(exc),
                    "type": exc.__class__.__name__,
                },
                "ui_hint": ui_hint,
            }

    def tool_stop(self, force: bool = False) -> dict[str, Any]:
        return self._safe("orchestrator.stop", lambda: self.stop(force))

    def tool_pause(self) -> dict[str, Any]:
        return self._safe("orchestrator.pause", self.pause)

    def tool_resume(self) -> dict[str, Any]:
        return self._safe("orchestrator.resume", self.resume)

    def tool_refresh(self, inflight_stable_ids: list[str] | None = None) -> dict[str, Any]:
        return self._safe("orchestrator.refresh", lambda: self.refresh(inflight_stable_ids))

    def tool_shutdown(self, force: bool = False) -> dict[str, Any]:
        return self._safe("orchestrator.shutdown", lambda: self.shutdown(force))

    def tool_snapshot(self) -> dict[str, Any]:
        return self._safe("orchestrator.snapshot", self.snapshot)

    def tool_session_worktree(self, issue_number: int) -> dict[str, Any]:
        return self._safe("orchestrator.session.worktree", lambda: self.session_worktree(issue_number))

    def tool_session_manifest(self, issue_number: int) -> dict[str, Any]:
        return self._safe("orchestrator.session.manifest", lambda: self.session_manifest(issue_number))

    def tool_session_phases(self, issue_number: int) -> dict[str, Any]:
        return self._safe("orchestrator.session.phases", lambda: self.session_phases(issue_number))

    def tool_session_claude_log(self, issue_number: int, limit: int = 200) -> dict[str, Any]:
        return self._safe("orchestrator.session.claude_log", lambda: self.session_claude_log(issue_number, limit))

    def tool_session_orchestrator_log(self, issue_number: int) -> dict[str, Any]:
        return self._safe(
            "orchestrator.session.orchestrator_log",
            lambda: self.session_orchestrator_log(issue_number),
        )

    def tool_session_send(self, issue_number: int, text: str) -> dict[str, Any]:
        return self._safe("orchestrator.session.send", lambda: self.session_send(issue_number, text))

    def tool_session_kill(self, issue_number: int) -> dict[str, Any]:
        return self._safe("orchestrator.session.kill", lambda: self.session_kill(issue_number))

    def tool_session_focus(self, issue_number: int) -> dict[str, Any]:
        return self._safe("orchestrator.session.focus", lambda: self.session_focus(issue_number))

    def tool_urls(self) -> dict[str, Any]:
        return self._safe("orchestrator.urls", self.urls)

    def tool_doctor(self) -> dict[str, Any]:
        return self._safe("orchestrator.doctor", self.doctor)

    def tool_state(self) -> dict[str, Any]:
        """Get complete system state for the unified dashboard."""
        return self._safe("orchestrator.state", self.get_system_state)

    def tool_repos(self) -> dict[str, Any]:
        """List all repos with status."""
        return self._safe("orchestrator.repos", self.list_repos)

    def tool_repos_start(self, repo_path: str, config_name: str = "default.yaml") -> dict[str, Any]:
        """Start orchestrator for a specific repo."""
        return self._safe("orchestrator.repos.start", lambda: self.start_repo(repo_path, config_name))

    def tool_repos_stop(self, repo_path: str, force: bool = False) -> dict[str, Any]:
        """Stop orchestrator for a specific repo."""
        return self._safe("orchestrator.repos.stop", lambda: self.stop_repo(repo_path, force))

    def get_system_state(self) -> dict[str, Any]:
        """Get complete system state."""
        from ..observation.instance_detector import detect_system_state

        state = detect_system_state()
        return state.to_dict()

    def list_repos(self) -> dict[str, Any]:
        """List all repos with status."""
        from ..observation.instance_detector import detect_system_state

        state = detect_system_state()
        return {"repos": [r.to_dict() for r in state.repos]}

    def start_repo(self, repo_path: str, config_name: str = "default.yaml") -> dict[str, Any]:
        """Start orchestrator for a specific repo."""
        path = Path(repo_path)
        if not path.exists():
            return {"error": f"Repository not found: {repo_path}"}

        try:
            info = supervisor.start(path, config_name)
            return {
                "status": "started",
                "pid": info.pid,
                "port": info.http_port,
            }
        except Exception as e:
            logger.exception("Failed to start orchestrator for %s", repo_path)
            return {"error": str(e)}

    def stop_repo(self, repo_path: str, force: bool = False) -> dict[str, Any]:
        """Stop orchestrator for a specific repo."""
        path = Path(repo_path)
        stopped = supervisor.stop(path, force=force)
        return {"status": "stopped" if stopped else "failed"}

    def status(self) -> dict[str, Any]:
        status = self._client.status()
        result: dict[str, Any] = {"supervisor": status.to_dict()}
        if status.state == "running" and status.port is not None:
            result["status"] = self._api.status()
            result["info"] = self._api.info()
        return result

    def start(self) -> dict[str, Any]:
        status = self._client.status()
        if status.state != "running":
            from ..infra.launcher import launch_subprocess

            config = Config.load(self._settings.config_path)
            launch_result = launch_subprocess(
                repo_root=self._settings.repo_root,
                config=config,
                config_name=self._settings.config_path.name,
                instance_id=self._settings.instance_id,
            )
            result: dict[str, Any] = {"launch": launch_result.to_dict()}
            # Update cached port from supervisor data
            if launch_result.supervisor and "port" in launch_result.supervisor:
                self._client.update_port(launch_result.supervisor["port"])
            return result
        return {"supervisor": status.to_dict()}

    def stop(self, force: bool = False) -> dict[str, Any]:
        stopped = supervisor.stop(
            self._settings.repo_root,
            instance_id=self._settings.instance_id,
            force=force,
        )
        return {"stopped": stopped}

    def pause(self) -> dict[str, Any]:
        return self._api.pause()

    def resume(self) -> dict[str, Any]:
        return self._api.resume()

    def refresh(self, inflight_stable_ids: list[str] | None) -> dict[str, Any]:
        return self._api.refresh(inflight_stable_ids or [])

    def shutdown(self, force: bool = False) -> dict[str, Any]:
        return self._api.shutdown(force=force)

    def snapshot(self) -> dict[str, Any]:
        return {
            "status": self._api.status(),
            "info": self._api.info(),
            "blocked": self._api.blocked_issues(),
            "stale": self._api.stale_issues(),
            "dependency_problems": self._api.dependency_problems(),
            "excluded": self._api.excluded_issues(),
            "publish_jobs": self._api.publish_jobs(),
            "history": self._api.history(),
        }

    def session_worktree(self, issue_number: int) -> dict[str, Any]:
        return self._api.session_worktree(issue_number)

    def session_manifest(self, issue_number: int) -> dict[str, Any]:
        return self._api.session_manifest(issue_number)

    def session_phases(self, issue_number: int) -> dict[str, Any]:
        return self._api.session_phases(issue_number)

    def session_claude_log(self, issue_number: int, limit: int = 200) -> dict[str, Any]:
        return self._api.session_claude_log(issue_number, limit)

    def session_orchestrator_log(self, issue_number: int) -> dict[str, Any]:
        return self._api.session_orchestrator_log(issue_number)

    def session_send(self, issue_number: int, text: str) -> dict[str, Any]:
        return self._api.send(issue_number, text)

    def session_kill(self, issue_number: int) -> dict[str, Any]:
        return self._api.kill(issue_number)

    def session_focus(self, issue_number: int) -> dict[str, Any]:
        return self._api.focus(issue_number)

    def urls(self) -> dict[str, Any]:
        base = self._client.base_url()
        return {
            "base_url": base,
            "dashboard_url": f"{base}/",
            "events_url": f"{base}/api/events",
            "config_url": f"{base}/api/config",
        }

    def doctor(self) -> dict[str, Any]:
        from ..infra.doctor import run_doctor
        from ..execution.command_runner import LocalCommandRunner

        config = None
        try:
            config = Config.load(self._settings.config_path)
        except Exception:
            config = None
        result = run_doctor(config=config, config_path=self._settings.config_path, runner=LocalCommandRunner())
        return result.to_dict()


mcp = FastMCP("Issue Orchestrator", json_response=True)


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

    if args.config_path and not args.repo_root:
        repo_root = _resolve_repo_root(Path(args.config_path))
        args.repo_root = str(repo_root)

    settings = _resolve_settings(args)
    app = McpApp(settings)
    app.register(mcp)

    try:
        mcp.run()
    finally:
        app.close()


if __name__ == "__main__":
    main()
