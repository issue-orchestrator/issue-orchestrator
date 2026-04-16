"""Lightweight control API for the orchestrator.

This API is always available regardless of UI mode, providing programmatic
control over the orchestrator. The web dashboard (in web mode) adds additional
routes on top of this.

Control API endpoints (in-process):
- POST /api/refresh - Trigger immediate issue refresh
- POST /api/pause - Pause orchestrator
- POST /api/resume - Resume orchestrator
- GET /api/status - Get orchestrator status
- GET /api/events - Stream structured events (SSE)
- GET /api/events_since - Fetch buffered events since an event id
- POST /api/gh_audit_report - Emit GH audit report to disk
- GET /api/snapshot - Fetch snapshot for test resync
- POST /api/shutdown - Request graceful shutdown
- POST /api/issues/{issue_number}/resume - Resume processing for a debug session
- POST /api/issues/{issue_number}/debug-session - Launch interactive debug session

Supervisor Control API endpoints (process management):
- POST /control/orchestrator/start - Start orchestrator for a repo
- POST /control/orchestrator/stop - Stop orchestrator for a repo
- POST /control/orchestrator/pause - Pause orchestrator (passthrough)
- POST /control/orchestrator/resume - Resume orchestrator (passthrough)
- POST /control/orchestrator/refresh - Trigger refresh (passthrough)
- GET /control/orchestrator/status - Get orchestrator process status
- GET /control/orchestrator/last_failure - Get last startup failure
- GET /control/orchestrator/log_tail - Get recent log lines

Multi-repo Registry API endpoints:
- GET /control/repos - List all registered repos with status
- POST /control/repos - Add a repo to the registry
- DELETE /control/repos - Remove a repo from the registry

E2E Test Runner API endpoints:
- POST /control/e2e/start - Start E2E test run
- POST /control/e2e/stop - Stop running E2E test
- GET /control/e2e/status - Get E2E runner status
- GET /control/e2e/runs - List recent E2E runs
- GET /control/e2e/run/{run_id} - Get run details with test results
- GET /control/e2e/run/{run_id}/timeline - Get timeline events for shared rendering
- GET /control/e2e/logs/{run_id} - Get run logs
- GET /control/e2e/failed/{run_id} - Get failed tests from a run
"""

import asyncio
import json
import logging
import os
import sys
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Optional

from fastapi import FastAPI, Request, Query
from fastapi.responses import JSONResponse, HTMLResponse
from sse_starlette.sse import EventSourceResponse

from ..control.worktree_manager import get_worktree_path
from ..control.queue_cache import QueueCache
from ..domain.models import get_completion_path
from ..infra.env import ENV_PREFIX
from ..infra import gh_audit
from ..infra.supervisor import DefaultSupervisorOps, SupervisorOps
from ..control.goal_pilot import GoalPilot
from ..execution.control_center_actions import (
    AuditActionRequest,
    ControlCenterActions,
    RepoActionRequest,
    TraceActionRequest,
)
from .control_api_e2e_runs import control_e2e_runs_router
from .control_api_orchestrator_routes import control_orchestrator_router
from .control_api_orchestrator_support import (
    ControlApiOrchestratorDependencies,
    install_control_api_orchestrator_dependencies,
)
from .control_api_e2e_support import (
    ControlApiE2EDependencies,
    install_control_api_e2e_dependencies,
)
from .control_api_repo_routes import control_repo_router
from .control_api_repo_support import (
    ControlApiRepoDependencies,
    install_control_api_repo_dependencies,
)
from .control_api_setup_routes import control_setup_router
from .control_api_setup_support import (
    ControlApiSetupDependencies,
    install_control_api_setup_dependencies,
)
from .control_api_shutdown_routes import control_shutdown_router
from .control_api_shutdown_state import (
    begin_engine_shutdown_operation,
    coerce_graceful_timeout_seconds,
    finish_engine_shutdown_operation,
    global_shutdown_in_progress,
)
from .control_api_shutdown_support import (
    ControlApiShutdownDependencies,
    install_control_api_shutdown_dependencies,
)
from .control_api_e2e_triage import control_e2e_triage_router
from .timeline_presentation import (
    _build_phase_toc,
    _build_timeline_cycles,
    _decorate_timeline_events,
    _filter_timeline_events,
)

# Path to templates
_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"

if TYPE_CHECKING:
    from ..infra.orchestrator import Orchestrator
    from ..infra.config import Config

logger = logging.getLogger(__name__)
_PREFERRED_REPO_ROOT_ENV = "ISSUE_ORCHESTRATOR_CC_REPO_ROOT"

def _load_config_by_name(repo_root: Path, config_name: str) -> "Config":
    """Load orchestrator config by repo root and config file name.

    Raises FileNotFoundError if the config file does not exist.
    """
    from ..infra.config import Config
    return Config.find_and_load(repo_root, config_name=config_name)


# Create minimal control API app
control_app = FastAPI(title="Issue Orchestrator Control API")

# Global reference to orchestrator (set at startup)
_orchestrator: "Orchestrator | None" = None

# Supervisor operations (injectable for testing)
_supervisor: SupervisorOps = DefaultSupervisorOps()
_control_actions = ControlCenterActions(supervisor=_supervisor)


def set_orchestrator(orchestrator: "Orchestrator") -> None:
    """Set the orchestrator instance for the control API."""
    global _orchestrator
    _orchestrator = orchestrator


def get_orchestrator() -> "Orchestrator | None":
    """Get the orchestrator instance."""
    return _orchestrator


def _with_state_lock(fn):
    if _orchestrator is None:
        return fn()
    lock = getattr(_orchestrator, "state_lock", None)
    if lock is None:
        return fn()
    with lock:
        return fn()

def _get_goal_pilot() -> GoalPilot:
    """Create a GoalPilot instance from the running orchestrator."""
    if _orchestrator is None:
        raise RuntimeError("Orchestrator not initialized")
    return GoalPilot(
        store=_orchestrator.deps.goal_pilot_store,
        events=_orchestrator.deps.events,
        action_applier=_orchestrator.deps.action_applier,
        repo_root=str(_orchestrator.config.repo_root),
        ctx=_orchestrator.event_context,
    )


def set_supervisor(supervisor: SupervisorOps) -> None:
    """Set the supervisor operations instance (for testing)."""
    global _supervisor, _control_actions
    _supervisor = supervisor
    _control_actions = ControlCenterActions(supervisor=_supervisor)


def get_supervisor() -> SupervisorOps:
    """Get the supervisor operations instance."""
    return _supervisor


def set_control_actions(actions: ControlCenterActions) -> None:
    """Inject control-center action service (for testing)."""
    global _control_actions
    _control_actions = actions


def get_control_actions() -> ControlCenterActions:
    """Get the control-center action service."""
    return _control_actions


def _preferred_repo_root() -> Path | None:
    """Resolve preferred repo root for this Control Center process."""
    raw = os.environ.get(_PREFERRED_REPO_ROOT_ENV, "").strip()
    if not raw:
        return None
    try:
        root = Path(raw).resolve()
    except (OSError, ValueError):
        return None
    if not root.exists() or not root.is_dir():
        return None
    return root


# Track orchestrator child PIDs for zombie reaping (used by control_center).
# This avoids racing with subprocess.run() for unrelated children.
#
# Only control_start (and the restart path at line ~1139) spawn orchestrators
# as children of the control center process. Other entry points (CLI, MCP server)
# are separate processes that manage their own children independently.
import threading as _threading

_tracked_pids: set[int] = set()
_tracked_pids_lock = _threading.Lock()


def _schedule_control_center_exit(delay_seconds: float = 0.5) -> None:
    """Terminate Control Center process after a short delay."""
    import signal
    import threading

    def delayed_shutdown() -> None:
        time.sleep(delay_seconds)
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(2)
        os.kill(os.getpid(), signal.SIGKILL)

    threading.Thread(target=delayed_shutdown, daemon=False).start()


def track_child_pid(pid: int) -> None:
    """Register an orchestrator child PID for zombie reaping."""
    with _tracked_pids_lock:
        _tracked_pids.add(pid)
        logger.debug("Tracking orchestrator PID %d for reaping", pid)


def untrack_child_pid(pid: int) -> None:
    """Unregister an orchestrator child PID."""
    with _tracked_pids_lock:
        _tracked_pids.discard(pid)


def get_tracked_pids() -> list[int]:
    """Get copy of tracked PIDs for reaping."""
    with _tracked_pids_lock:
        return list(_tracked_pids)


def _track_launched_pids(supervisor_data: Mapping[str, object]) -> None:
    """Register launched orchestrator PIDs for zombie reaping.

    Called by control_start after successfully launching orchestrators.
    """
    # Handle multi-instance launches
    instances = supervisor_data.get("instances")
    if isinstance(instances, list):
        for instance in instances:
            if not isinstance(instance, dict):
                continue
            pid = instance.get("pid")
            if isinstance(pid, int):
                track_child_pid(pid)
    # Handle single-instance launches
    else:
        pid = supervisor_data.get("pid")
        if isinstance(pid, int):
            track_child_pid(pid)


# ======================================================================# Unified Dashboard API Endpoints
# ======================================================================# These endpoints support the unified dashboard entry point.


@control_app.get("/api/state")
async def get_system_state() -> JSONResponse:
    """Get complete system state for the unified dashboard.

    Returns dashboard status, all repos with orchestrator status, and context info.
    This is the primary endpoint for the unified dashboard to understand current state.
    """
    from ..observation.instance_detector import detect_system_state

    state = detect_system_state()
    return JSONResponse(state.to_dict())


@control_app.get("/api/repos")
async def get_repos() -> JSONResponse:
    """List all known repos with status.

    Returns registered repos plus current directory (if it's a repo).
    Each repo includes config status and orchestrator state.
    """
    from ..observation.instance_detector import detect_system_state

    state = detect_system_state()
    return JSONResponse({"repos": [r.to_dict() for r in state.repos]})


@control_app.post("/api/repos/{repo_id}/start")
async def start_repo_orchestrator(repo_id: str, request: Request) -> JSONResponse:
    """Start orchestrator for a specific repo.

    The repo_id is the URL-encoded absolute path to the repo.

    JSON body (optional):
        config_name: str - Config file to use (default: default.yaml)
    """
    from urllib.parse import unquote

    repo_path = unquote(repo_id)
    path = Path(repo_path)

    if not path.exists():
        return JSONResponse({"error": f"Repository not found: {repo_path}"}, status_code=404)

    # Parse optional config_name from body
    config_name = "default.yaml"
    try:
        body = await request.json()
        if isinstance(body, dict) and "config_name" in body:
            config_name = body["config_name"]
    except Exception:
        pass

    # Use supervisor to start
    try:
        info = _supervisor.start(path, config_name)
        _track_launched_pids({"pid": info.pid})
        return JSONResponse({
            "status": "started",
            "pid": info.pid,
            "port": info.http_port,
        })
    except Exception as e:
        logger.exception("Failed to start orchestrator for %s", repo_path)
        return JSONResponse({"error": str(e)}, status_code=500)


@control_app.post("/api/repos/{repo_id}/stop")
async def stop_repo_orchestrator(repo_id: str, request: Request) -> JSONResponse:
    """Stop orchestrator for a specific repo.

    The repo_id is the URL-encoded absolute path to the repo.

    JSON body (optional):
        force: bool - Force kill if graceful shutdown fails (default: false)
    """
    from urllib.parse import unquote

    repo_path = unquote(repo_id)
    path = Path(repo_path)

    # Parse optional force from body
    force = False
    try:
        body = await request.json()
        if isinstance(body, dict) and "force" in body:
            force = bool(body["force"])
    except Exception:
        pass

    # Use supervisor to stop
    stopped = _supervisor.stop(path, force=force)
    return JSONResponse({"status": "stopped" if stopped else "failed"})


@control_app.get("/api/repos/{repo_id}/status")
async def get_repo_status(repo_id: str) -> JSONResponse:
    """Get detailed status for a specific repo.

    The repo_id is the URL-encoded absolute path to the repo.
    """
    from urllib.parse import unquote
    from ..observation.instance_detector import _get_config_status, _get_orchestrator_state

    repo_path = unquote(repo_id)
    path = Path(repo_path)

    if not path.exists():
        return JSONResponse({"error": f"Repository not found: {repo_path}"}, status_code=404)

    config_status, configs = _get_config_status(path)
    orch_state, orch_pid, orch_port = _get_orchestrator_state(path)

    return JSONResponse({
        "path": repo_path,
        "name": path.name,
        "config_status": config_status,
        "configs": configs,
        "orchestrator_state": orch_state,
        "orchestrator_pid": orch_pid,
        "orchestrator_port": orch_port,
    })


@control_app.get("/api/discover")
async def discover_repos_api(
    search_paths: str = Query(
        default="",
        description="Comma-separated paths to search",
    ),
    max_depth: int = Query(default=2, description="Max directory depth"),
) -> JSONResponse:
    """Discover git repositories that could be configured.

    Scans common development directories for git repos.
    """
    from ..observation.instance_detector import discover_repos

    paths = None
    if search_paths:
        paths = [Path(p.strip()).expanduser() for p in search_paths.split(",")]

    discovered = discover_repos(search_paths=paths, max_depth=max_depth)
    return JSONResponse({"discovered": discovered})


@control_app.post("/api/refresh")
async def refresh(request: Request) -> JSONResponse:
    """Request an immediate refresh of issues from GitHub.

    This triggers the orchestrator to fetch issues on the next loop iteration,
    bypassing the fetch-layer network sync interval.

    Optional JSON body:
        inflight_stable_ids: list[str] - Issue IDs that tests expect to discover.
            If provided and these issues are not found after a cached refresh,
            the orchestrator will retry without cache to handle GitHub's
            eventual consistency.
    """
    if _orchestrator is None:
        return JSONResponse({"error": "Orchestrator not initialized"}, status_code=503)

    # Parse optional inflight_stable_ids from request body
    inflight_stable_ids: set[str] = set()
    try:
        body = await request.body()
        if body:
            data = json.loads(body)
            if isinstance(data, dict) and "inflight_stable_ids" in data:
                ids = data["inflight_stable_ids"]
                if isinstance(ids, list):
                    inflight_stable_ids = set(str(i) for i in ids)
    except (json.JSONDecodeError, ValueError):
        pass  # Ignore malformed body, proceed with empty set

    _orchestrator.request_refresh(inflight_stable_ids=inflight_stable_ids)
    return JSONResponse({"status": "refresh_requested"})


@control_app.post("/api/pause")
async def pause() -> JSONResponse:
    """Pause the orchestrator - stop launching new sessions."""
    if _orchestrator is None:
        return JSONResponse({"error": "Orchestrator not initialized"}, status_code=503)

    _orchestrator.pause()
    return JSONResponse({"status": "paused"})


@control_app.post("/api/resume")
async def resume() -> JSONResponse:
    """Resume the orchestrator - allow launching new sessions."""
    if _orchestrator is None:
        return JSONResponse({"error": "Orchestrator not initialized"}, status_code=503)

    _orchestrator.resume()
    return JSONResponse({"status": "resumed"})


@control_app.get("/api/status")
async def status() -> JSONResponse:
    """Get current orchestrator status."""
    if _orchestrator is None:
        return JSONResponse({"error": "Orchestrator not initialized"}, status_code=503)

    state = _orchestrator.state
    return JSONResponse({
        "paused": state.paused,
        "active_sessions": len(state.active_sessions),
        "pending_reviews": len(state.pending_reviews),
        "pending_reworks": len(state.pending_reworks),
        "completed_today": len(state.completed_today),
        "issues_in_queue": len(state.cached_queue_issues),
        "instance_id": _orchestrator.deps.services.instance_id,
    })


@control_app.get("/api/events")
async def events(request: Request):
    """Server-Sent Events endpoint for test automation."""
    if _orchestrator is None or _orchestrator.event_hub is None:
        return JSONResponse({"error": "Event hub not initialized"}, status_code=503)

    event_hub = _orchestrator.event_hub
    logger.info("[SSE] Client connected (subscribers=%d, last_event_id=%s)",
                event_hub.stats().get("subscribers"),
                event_hub.last_event_id)

    async def event_generator():
        subscription = event_hub.subscribe()
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(subscription.queue.get(), timeout=30.0)
                    yield {
                        "event": event.type,
                        "data": json.dumps({
                            "event_id": event.event_id,
                            "type": event.type,
                            "issue_key": event.issue_key,
                            "payload": event.payload,
                        }),
                    }
                except asyncio.TimeoutError:
                    yield {"comment": "keepalive"}
        finally:
            event_hub.unsubscribe(subscription)
            logger.info("[SSE] Client disconnected (subscribers=%d)", event_hub.stats().get("subscribers"))

    return EventSourceResponse(event_generator())


@control_app.get("/api/events_since")
async def events_since(after: int = Query(0, alias="after")) -> JSONResponse:
    """Return buffered events since the provided event id."""
    if _orchestrator is None or _orchestrator.event_hub is None:
        return JSONResponse({"error": "Event hub not initialized"}, status_code=503)

    event_hub = _orchestrator.event_hub
    events = event_hub.get_since(after)
    stats = event_hub.stats()
    logger.info(
        "[SSE] Replay request after=%d events=%d oldest=%s newest=%s",
        after,
        len(events),
        stats.get("oldest_event_id"),
        stats.get("newest_event_id"),
    )
    payload = [
        {
            "event_id": event.event_id,
            "type": event.type,
            "issue_key": event.issue_key,
            "payload": event.payload,
        }
        for event in events
    ]
    return JSONResponse({
        "events": payload,
        "last_event_id": event_hub.last_event_id,
        "stats": stats,
    })


@control_app.get("/api/events_stats")
async def events_stats() -> JSONResponse:
    """Return event buffer and replay statistics."""
    if _orchestrator is None or _orchestrator.event_hub is None:
        return JSONResponse({"error": "Event hub not initialized"}, status_code=503)

    return JSONResponse({"stats": _orchestrator.event_hub.stats()})


@control_app.post("/api/gh_audit_report")
async def gh_audit_report() -> JSONResponse:
    """Emit the GH audit report to disk and return the path."""
    if not gh_audit.enabled():
        return JSONResponse({"error": "GH audit not enabled"}, status_code=400)
    path = gh_audit.emit_report()
    return JSONResponse({"status": "ok", "path": path})


@control_app.get("/api/snapshot")
async def snapshot() -> JSONResponse:
    """Fetch a snapshot of orchestrator state for test resync."""
    if _orchestrator is None:
        return JSONResponse({"error": "Orchestrator not initialized"}, status_code=503)

    if _orchestrator.event_hub is None:
        return JSONResponse({"error": "Event hub not initialized"}, status_code=503)

    from ..control.snapshot_builder import SnapshotBuilder

    builder = SnapshotBuilder(config=_orchestrator.config, repository_host=_orchestrator.deps.repository_host)
    snapshot_id = _orchestrator.event_hub.last_event_id
    last_tick_id = _orchestrator.event_context.tick_id

    try:
        data = await asyncio.to_thread(
            builder.build_snapshot,
            _orchestrator.state,
            snapshot_id,
            last_tick_id,
        )
        return JSONResponse(data)
    except Exception as exc:
        logger.exception("Control API snapshot failed: %s", exc)
        return JSONResponse({"error": "snapshot_failed", "detail": str(exc)}, status_code=500)


@control_app.get("/api/health")
async def health() -> JSONResponse:
    """Get health status of orchestrator components.

    Returns status of:
    - orchestrator: running/not initialized
    - terminal: tmux server and session health
    """
    health_data: dict = {
        "orchestrator": {"status": "not_initialized"},
        "terminal": {"status": "unknown"},
    }

    if _orchestrator is None:
        return JSONResponse(health_data, status_code=503)

    health_data["orchestrator"] = {
        "status": "running",
        "paused": _orchestrator.state.paused,
        "active_sessions": len(_orchestrator.state.active_sessions),
    }

    # Get terminal health via hook
    try:
        terminal_health = _orchestrator.deps.runner.terminal_health_check()
        if terminal_health:
            health_data["terminal"] = terminal_health
        else:
            health_data["terminal"] = {"status": "no_plugin"}
    except Exception as e:
        health_data["terminal"] = {"status": "error", "error": str(e)}

    # Overall health
    terminal_ok = health_data["terminal"].get("healthy", False)
    health_data["overall"] = "healthy" if terminal_ok else "degraded"

    status_code = 200 if terminal_ok else 503
    return JSONResponse(health_data, status_code=status_code)


@control_app.post("/api/shutdown")
async def shutdown(request: Request) -> JSONResponse:
    """Request graceful shutdown of the orchestrator (stops new work, waits for agents)."""
    if _orchestrator is None:
        return JSONResponse({"error": "Orchestrator not initialized"}, status_code=503)

    # Log shutdown request with context
    active_sessions = _orchestrator.state.active_sessions if _orchestrator.state else []
    client_host = request.client.host if request.client else "unknown"
    logger.info(
        "Shutdown requested (graceful): source=web_ui, client=%s, active_sessions=%d",
        client_host,
        len(active_sessions),
    )

    _orchestrator.request_shutdown()
    return JSONResponse({"status": "shutdown_requested", "active_sessions": len(active_sessions)})


@control_app.post("/api/preflight-push")
async def preflight_push(request: Request) -> JSONResponse:
    """Check if a git push would succeed (dry-run).

    This endpoint allows coding-done/reviewer-done to verify a push would work
    before completing, while the agent is still active and can fix any issues.

    The agent environment has credentials scrubbed, so it cannot do this
    check itself. The orchestrator has credentials and performs the check.

    JSON body:
        worktree: str - Path to the worktree

    Returns:
        would_succeed: bool - Whether push would succeed
        error: str | null - Error message if push would fail
        fix_hint: str | null - Suggestion for how to fix the issue
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    worktree_path = body.get("worktree")
    if not worktree_path:
        return JSONResponse({"error": "worktree is required"}, status_code=400)

    worktree = Path(worktree_path)
    if not worktree.exists():
        return JSONResponse({"error": f"Worktree does not exist: {worktree}"}, status_code=400)

    # Use the GitWorkingCopy adapter (port implementation)
    from ..execution import GitWorkingCopy

    git = GitWorkingCopy()
    result = git.push_preflight(worktree)

    return JSONResponse({
        "would_succeed": result.would_succeed,
        "error": result.error,
        "fix_hint": result.fix_hint,
    })


@control_app.post("/api/issues/{issue_number}/resume")
async def resume_issue(issue_number: int) -> JSONResponse:
    """Resume orchestrator processing for a blocked/debug issue.

    This endpoint is called by `coding-done --resume` after writing a completion
    record in a debug session. It triggers the orchestrator to process the
    completion.json and continue the normal flow (create PR, run review, etc.).

    Can also be called from the web UI "Process Completion" button.

    Args:
        issue_number: The issue number to resume processing for

    Returns:
        JSON with:
        - success: bool - Whether processing succeeded
        - message: str - Status message
        - pr_url: str | null - PR URL if one was created
        - actions_taken: list[str] | null - Actions performed
        - errors: list[str] | null - Any errors encountered
    """
    if _orchestrator is None:
        return JSONResponse(
            {"success": False, "error": "Orchestrator not initialized"},
            status_code=503
        )

    # Get worktree path for this issue
    worktree = get_worktree_path(_orchestrator.config, issue_number)

    if not worktree.exists():
        return JSONResponse({
            "success": False,
            "error": f"Worktree not found: {worktree}",
            "hint": "The worktree may have been cleaned up. Check if the issue is still blocked.",
        }, status_code=404)

    # Check for completion.json
    completion_path: str | None = None
    run_dir = _orchestrator.deps.session_output.find_run_dir(worktree)
    if isinstance(run_dir, Path):
        manifest = _orchestrator.deps.session_output.read_manifest(run_dir)
        if manifest and manifest.get("completion_path"):
            completion_path = manifest["completion_path"]

    legacy_completion = worktree / ".issue-orchestrator" / "completion.json"
    completion_record = worktree / completion_path if completion_path else legacy_completion
    if completion_path and not completion_record.exists() and legacy_completion.exists():
        completion_path = None
        completion_record = legacy_completion
    if not completion_record.exists():
        return JSONResponse({
            "success": False,
            "error": "No completion record found",
            "hint": "Run 'coding-done completed --implementation ... --problems ...' first.",
        }, status_code=404)

    issue_title = _get_issue_title(_orchestrator, issue_number)

    # Process the completion
    try:
        result = _orchestrator.deps.completion_processor.process(
            worktree=worktree,
            issue_number=issue_number,
            issue_title=issue_title,
            completion_path=completion_path,
        )

        return JSONResponse({
            "success": result.success,
            "message": result.message,
            "pr_url": result.pr_url,
            "actions_taken": result.actions_taken,
            "errors": result.errors,
        })
    except Exception as e:
        logger.exception("Error processing completion for issue #%d: %s", issue_number, e)
        return JSONResponse({
            "success": False,
            "error": f"Processing failed: {e}",
        }, status_code=500)


@control_app.post("/api/issues/{issue_number}/debug-session")
async def launch_debug_session(issue_number: int) -> JSONResponse:  # noqa: C901 - debug session with validation and setup phases
    """Launch an interactive debug session for a blocked issue.

    This endpoint creates a terminal session in the issue's existing worktree,
    with environment variables set so `coding-done --resume` can signal completion
    back to the orchestrator.

    The session runs the issue's configured agent in interactive mode (without
    the -p flag for Claude, etc.) so users can debug and fix issues manually.

    Args:
        issue_number: The issue number to debug

    Returns:
        JSON with:
        - success: bool - Whether session launched
        - session_name: str - Terminal session name
        - worktree_path: str - Path to the worktree
        - agent: str - Agent type being used
        - hint: str - Instructions for the user
    """
    if _orchestrator is None:
        return JSONResponse(
            {"success": False, "error": "Orchestrator not initialized"},
            status_code=503
        )

    config = _orchestrator.config
    state = _orchestrator.state

    # Get worktree path for this issue
    worktree = get_worktree_path(config, issue_number)

    if not worktree.exists():
        return JSONResponse({
            "success": False,
            "error": f"Worktree not found: {worktree}",
            "hint": "The worktree may have been cleaned up. The issue needs to be re-run first.",
        }, status_code=404)

    # Find the issue in cached queue to get its agent type
    orchestrator = _orchestrator

    def _cached_issue():
        assert orchestrator is not None
        for cached_issue in state.cached_queue_issues:
            if cached_issue.number == issue_number:
                return cached_issue
        return None

    issue = _with_state_lock(_cached_issue)

    if not issue:
        # Try to fetch from GitHub
        try:
            issue = _orchestrator.deps.repository_host.get_issue(issue_number)
        except Exception as e:
            logger.warning("Could not fetch issue #%d: %s", issue_number, e)

    if not issue:
        return JSONResponse({
            "success": False,
            "error": f"Issue #{issue_number} not found",
            "hint": "The issue may have been closed or doesn't exist.",
        }, status_code=404)

    agent_type = issue.agent_type
    if not agent_type:
        return JSONResponse({
            "success": False,
            "error": "Issue has no agent type label",
            "hint": "Add an agent label (e.g., 'agent:claude') to the issue.",
        }, status_code=400)

    agent_config = config.agents.get(agent_type)
    if not agent_config:
        return JSONResponse({
            "success": False,
            "error": f"No agent config for {agent_type}",
            "hint": "Check your orchestrator configuration.",
        }, status_code=400)

    # Check if a session already exists for this issue
    session_name = f"debug-{issue_number}"
    if _orchestrator.deps.runner.session_exists(issue_number, session_name):
        return JSONResponse({
            "success": False,
            "error": f"Debug session already exists: {session_name}",
            "hint": "A debug session is already running. Focus on it or kill it first.",
        }, status_code=409)

    # Build command using agent_config.get_command()
    # Add context that this is a debug session with existing work to evaluate
    debug_context = (
        "This is an INTERACTIVE DEBUG SESSION. A previous automated run failed or was blocked. "
        "Work with the user to investigate and fix the issue. When done, the user will run "
        "'coding-done --resume' to continue the orchestrator flow."
    )
    base_command = agent_config.get_command(
        issue_number=issue_number,
        issue_title=issue.title,
        worktree=worktree,
        existing_work=debug_context,
        task_kind="code",
    )

    completion_path = get_completion_path(agent_type, session_name=session_name)
    run_dir = _orchestrator.deps.session_output.ensure_run_dir(worktree, session_name)
    _orchestrator.deps.session_output.update_manifest(
        run_dir,
        {
            "completion_path": completion_path,
            "issue_number": issue_number,
            "agent_label": agent_type,
        },
    )

    # Set env vars for coding-done --resume
    env_exports = f"export ORCHESTRATOR_ISSUE_NUMBER='{issue_number}'"
    env_exports += f" ORCHESTRATOR_API_PORT='{config.control_api_port}'"
    env_exports += f" ORCHESTRATOR_AGENT_LABEL='{agent_type}'"
    env_exports += f" ORCHESTRATOR_SESSION_ID='{session_name}'"
    env_exports += f" {ENV_PREFIX}COMPLETION_PATH='{completion_path}'"
    # Ensure orchestrator tools (coding-done, reviewer-done) are on PATH for all backends.
    orch_bin = Path(sys.executable).parent
    env_exports += f' PATH="{orch_bin}:$PATH"'
    command = f"{env_exports} && {base_command}"

    logger.info(
        "[debug-session] Launching for issue #%d: session=%s worktree=%s agent=%s",
        issue_number, session_name, worktree, agent_type,
    )

    # Create the terminal session
    session_created = _orchestrator.deps.runner.create_session(
        session_id=issue_number,
        command=command,
        working_dir=str(worktree),
        title=f"Debug #{issue_number}",
        session_name=session_name,
    )

    if not session_created:
        return JSONResponse({
            "success": False,
            "error": "Failed to create terminal session",
            "hint": "Check if tmux is running and accessible.",
        }, status_code=500)

    return JSONResponse({
        "success": True,
        "session_name": session_name,
        "worktree_path": str(worktree),
        "agent": agent_type.replace("agent:", ""),
        "hint": f"Debug session launched. When done, run 'coding-done --resume' to process completion.",
    })


def _update_cached_issue_labels(issue_number: int, labels_to_remove: list[str]) -> None:
    """Update the cached issue to remove specified labels (avoids full queue refresh).

    Since GitHubIssue is frozen/immutable, we create a new instance with updated labels
    and replace it in the cached_queue_issues list.
    """
    if _orchestrator is None:
        return

    orchestrator = _orchestrator
    from dataclasses import is_dataclass, replace

    def _update() -> None:
        state = orchestrator.state
        for issue in state.cached_queue_issues:
            if issue.number == issue_number:
                # Remove the specified labels from the issue
                new_labels = tuple(
                    label for label in issue.labels
                    if label not in labels_to_remove
                )
                # Create updated issue with new labels (only works for dataclass implementations)
                if is_dataclass(issue) and not isinstance(issue, type):
                    updated_issue = replace(issue, labels=new_labels)
                    queue_cache = QueueCache(orchestrator.config, state)
                    queue_cache.upsert_refreshed_issue(updated_issue)
                    logger.debug(
                        "[cache] Updated issue #%d labels: removed %s",
                        issue_number,
                        labels_to_remove,
                    )
                break

    _with_state_lock(_update)


def _get_issue_title(orchestrator: "Orchestrator", issue_number: int) -> str:
    """Resolve issue title from cache, falling back to GitHub."""
    issue_title = f"Issue #{issue_number}"
    try:
        def _cached_title() -> str | None:
            for issue in orchestrator.state.cached_queue_issues:
                if issue.number == issue_number:
                    return issue.title
            return None

        cached_title = _with_state_lock(_cached_title)
        if cached_title:
            return cached_title

        issue_data = orchestrator.deps.repository_host.get_issue(issue_number)
        if issue_data:
            return issue_data.title
    except Exception as e:
        logger.warning("Could not fetch issue title for #%d: %s", issue_number, e)

    return issue_title


@control_app.post("/api/issues/{issue_number}/retry")
async def retry_issue(issue_number: int) -> JSONResponse:
    """Retry a blocked issue by removing the blocked label and re-queueing.

    This removes the 'blocked' and 'needs-human' labels from the issue,
    allowing it to be picked up by the orchestrator again.

    Args:
        issue_number: The issue number to retry

    Returns:
        JSON with:
        - success: bool - Whether the operation succeeded
        - message: str - Status message
    """
    if _orchestrator is None:
        return JSONResponse(
            {"success": False, "error": "Orchestrator not initialized"},
            status_code=503
        )

    try:
        lm = _orchestrator.deps.label_manager
        from ..control.retry_policy import labels_to_remove_for_retry

        current_labels = _orchestrator.repository_host.get_issue_labels(issue_number)
        labels_to_remove = labels_to_remove_for_retry(current_labels, lm)

        removed = []
        for label in labels_to_remove:
            try:
                _orchestrator.repository_host.remove_label(issue_number, label)
                removed.append(label)
            except Exception:
                pass  # Label might not exist, that's fine

        # Update cache locally to avoid full queue refresh
        _update_cached_issue_labels(issue_number, labels_to_remove)

        logger.info("[retry] Issue #%d retried, removed labels: %s", issue_number, removed)
        return JSONResponse({
            "success": True,
            "message": f"Issue #{issue_number} queued for retry",
            "removed_labels": removed,
        })

    except Exception as e:
        logger.exception("Error retrying issue #%d: %s", issue_number, e)
        return JSONResponse({
            "success": False,
            "error": str(e),
        }, status_code=500)


@control_app.post("/api/issues/{issue_number}/dismiss")
async def dismiss_issue(issue_number: int) -> JSONResponse:
    """Dismiss a blocked issue without retrying.

    This removes the issue from the blocked list by removing blocking labels,
    but also removes in-progress labels so the issue won't be picked up again
    unless it has the agent label restored.

    Args:
        issue_number: The issue number to dismiss

    Returns:
        JSON with:
        - success: bool - Whether the operation succeeded
        - message: str - Status message
    """
    if _orchestrator is None:
        return JSONResponse(
            {"success": False, "error": "Orchestrator not initialized"},
            status_code=503
        )

    try:
        lm = _orchestrator.deps.label_manager

        # Remove all orchestrator-managed labels to fully dismiss
        labels_to_remove = [
            lm.blocked,
            lm.needs_human,
            lm.blocked_failed,
            lm.in_progress,
        ]

        removed = []
        for label in labels_to_remove:
            try:
                _orchestrator.repository_host.remove_label(issue_number, label)
                removed.append(label)
            except Exception:
                pass  # Label might not exist, that's fine

        orchestrator = _orchestrator

        def _prune_state() -> None:
            assert orchestrator is not None
            # Remove from session history if present
            orchestrator.state.session_history = [
                entry for entry in orchestrator.state.session_history
                if entry.issue_number != issue_number
            ]

            QueueCache(orchestrator.config, orchestrator.state).remove_issue(issue_number)

        _with_state_lock(_prune_state)

        logger.info("[dismiss] Issue #%d dismissed, removed labels: %s", issue_number, removed)
        return JSONResponse({
            "success": True,
            "message": f"Issue #{issue_number} dismissed",
            "removed_labels": removed,
        })

    except Exception as e:
        logger.exception("Error dismissing issue #%d: %s", issue_number, e)
        return JSONResponse({
            "success": False,
            "error": str(e),
        }, status_code=500)


@control_app.get("/favicon.ico")
async def favicon():
    """Serve the logo as favicon."""
    from fastapi.responses import Response

    logo_path = Path(__file__).parent.parent.parent.parent / "assets" / "logo.svg"
    if logo_path.exists():
        return Response(
            content=logo_path.read_bytes(),
            media_type="image/svg+xml",
        )
    return Response(status_code=204)


@control_app.get("/", response_class=HTMLResponse)
async def control_center_ui() -> HTMLResponse:
    """Serve the control center UI.

    This UI is served by the control API and works even when no orchestrator
    is running. It allows starting/stopping orchestrators for any registered repo.
    """
    template_path = _TEMPLATES_DIR / "control_center.html"
    if not template_path.exists():
        return HTMLResponse(
            "<html><body><h1>Control Center</h1><p>Template not found</p></body></html>",
            status_code=500,
        )

    from .. import __version__
    from ..infra.repo_identity import get_repo_head_sha

    commit_sha = get_repo_head_sha(Path.cwd())
    commit_short = commit_sha[:7] if commit_sha else "unknown"
    content = template_path.read_text()
    content = content.replace("{{ version }}", __version__)
    content = content.replace("{{ commit_sha }}", commit_short)
    return HTMLResponse(content)


# ======================================================================# Supervisor Control API - Process Management Endpoints
# ======================================================================# These endpoints manage orchestrator processes via the Supervisor.
# They work with repo_root paths rather than in-process state.


def _validate_repo_root(repo_root: str | None) -> Path | None:
    """Validate and normalize a repo_root parameter.

    Security: Only allow local paths that exist.

    Returns:
        Normalized Path if valid, None if invalid
    """
    if not repo_root:
        return None

    try:
        path = Path(repo_root).resolve()
        # Security: only allow existing local directories
        if not path.exists() or not path.is_dir():
            return None
        return path
    except (ValueError, OSError):
        return None


install_control_api_e2e_dependencies(
    control_app,
    ControlApiE2EDependencies(
        get_orchestrator=get_orchestrator,
        load_config_by_name=_load_config_by_name,
        validate_repo_root=_validate_repo_root,
    ),
)
install_control_api_orchestrator_dependencies(
    control_app,
    ControlApiOrchestratorDependencies(
        get_supervisor=get_supervisor,
        get_control_actions=get_control_actions,
        validate_repo_root=_validate_repo_root,
        track_launched_pids=_track_launched_pids,
        coerce_graceful_timeout_seconds=coerce_graceful_timeout_seconds,
        global_shutdown_in_progress=global_shutdown_in_progress,
        begin_engine_shutdown_operation=begin_engine_shutdown_operation,
        finish_engine_shutdown_operation=finish_engine_shutdown_operation,
    ),
)
install_control_api_shutdown_dependencies(
    control_app,
    ControlApiShutdownDependencies(
        get_supervisor=get_supervisor,
        schedule_control_center_exit=lambda: _schedule_control_center_exit(),
    ),
)
install_control_api_repo_dependencies(
    control_app,
    ControlApiRepoDependencies(
        get_supervisor=get_supervisor,
        validate_repo_root=_validate_repo_root,
        get_preferred_repo_root=_preferred_repo_root,
        get_expected_engine_identity_raw=lambda: os.environ.get(
            "ISSUE_ORCHESTRATOR_EXPECTED_IDENTITY",
            "",
        ).strip() or None,
    ),
)
install_control_api_setup_dependencies(
    control_app,
    ControlApiSetupDependencies(
        validate_repo_root=_validate_repo_root,
    ),
)
control_app.include_router(control_orchestrator_router)
control_app.include_router(control_shutdown_router)
control_app.include_router(control_repo_router)
control_app.include_router(control_setup_router)
control_app.include_router(control_e2e_runs_router)
control_app.include_router(control_e2e_triage_router)


# ======================================================================# Goal Pilot API Endpoints
# ======================================================================# These endpoints expose Goal Pilot planning and skill-management operations.


@control_app.post("/control/goal_pilot/runs")
async def goal_pilot_create(request: Request) -> JSONResponse:
    if _orchestrator is None:
        return JSONResponse({"error": "orchestrator_not_initialized"}, status_code=503)
    body = await request.json()
    goals = body.get("goals") or []
    done_criteria = body.get("done_criteria") or {}
    name = body.get("name")
    milestones = body.get("milestones")
    if not name or not str(name).strip():
        return JSONResponse({"error": "name_required"}, status_code=400)
    pilot = _get_goal_pilot()
    run_id = pilot.create(goals=goals, done_criteria=done_criteria, name=name)
    if milestones:
        pilot.update_goals(run_id, goals, note=f"milestones={milestones}")
    return JSONResponse({"run_id": run_id})


@control_app.get("/control/goal_pilot/runs")
async def goal_pilot_runs() -> JSONResponse:
    if _orchestrator is None:
        return JSONResponse({"error": "orchestrator_not_initialized"}, status_code=503)
    pilot = _get_goal_pilot()
    return JSONResponse({"runs": pilot.list_runs()})


@control_app.get("/control/goal_pilot/config")
async def goal_pilot_config() -> JSONResponse:
    if _orchestrator is None:
        return JSONResponse({"error": "orchestrator_not_initialized"}, status_code=503)
    gp_config = _orchestrator.config.goal_pilot
    configured = bool(gp_config.enabled and gp_config.agent)
    return JSONResponse({
        "enabled": gp_config.enabled,
        "agent": gp_config.agent,
        "approval_policy": gp_config.approval_policy,
        "approval_batch_size": gp_config.approval_batch_size,
        "approval_batch_window_minutes": gp_config.approval_batch_window_minutes,
        "configured": configured,
    })


@control_app.get("/control/goal_pilot/runs/{run_id}")
async def goal_pilot_status(run_id: str) -> JSONResponse:
    if _orchestrator is None:
        return JSONResponse({"error": "orchestrator_not_initialized"}, status_code=503)
    pilot = _get_goal_pilot()
    status = pilot.status(run_id)
    return JSONResponse({"status": status})


@control_app.post("/control/goal_pilot/runs/{run_id}/phase")
async def goal_pilot_phase(run_id: str, request: Request) -> JSONResponse:
    if _orchestrator is None:
        return JSONResponse({"error": "orchestrator_not_initialized"}, status_code=503)
    body = await request.json()
    phase = body.get("phase")
    reason = body.get("reason")
    changes = body.get("changes") or {}
    if not phase or not reason:
        return JSONResponse({"error": "phase_and_reason_required"}, status_code=400)
    pilot = _get_goal_pilot()
    result = pilot.set_phase(run_id, phase=phase, reason=reason, changes=changes)
    return JSONResponse(result)


@control_app.get("/control/goal_pilot/runs/{run_id}/journeys")
async def goal_pilot_journeys(run_id: str) -> JSONResponse:
    if _orchestrator is None:
        return JSONResponse({"error": "orchestrator_not_initialized"}, status_code=503)
    pilot = _get_goal_pilot()
    return JSONResponse({"journeys": pilot.list_journeys(run_id)})


@control_app.post("/control/goal_pilot/runs/{run_id}/journeys")
async def goal_pilot_journey_create(run_id: str, request: Request) -> JSONResponse:
    if _orchestrator is None:
        return JSONResponse({"error": "orchestrator_not_initialized"}, status_code=503)
    body = await request.json()
    pilot = _get_goal_pilot()
    try:
        journey = pilot.create_journey(run_id, body)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"journey": journey})


@control_app.patch("/control/goal_pilot/journeys/{journey_id}")
async def goal_pilot_journey_update(journey_id: str, request: Request) -> JSONResponse:
    if _orchestrator is None:
        return JSONResponse({"error": "orchestrator_not_initialized"}, status_code=503)
    body = await request.json()
    pilot = _get_goal_pilot()
    try:
        journey = pilot.update_journey(journey_id, body)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return JSONResponse({"journey": journey})


@control_app.post("/control/goal_pilot/runs/{run_id}/journeys/reorder")
async def goal_pilot_journey_reorder(run_id: str, request: Request) -> JSONResponse:
    if _orchestrator is None:
        return JSONResponse({"error": "orchestrator_not_initialized"}, status_code=503)
    body = await request.json()
    order = body.get("order")
    if not isinstance(order, list):
        return JSONResponse({"error": "order_list_required"}, status_code=400)
    pilot = _get_goal_pilot()
    try:
        result = pilot.reorder_journeys(run_id, order)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    result = pilot.reorder_journeys(run_id, order)
    return JSONResponse(result)


@control_app.patch("/control/goal_pilot/runs/{run_id}")
async def goal_pilot_update(run_id: str, request: Request) -> JSONResponse:
    if _orchestrator is None:
        return JSONResponse({"error": "orchestrator_not_initialized"}, status_code=503)
    body = await request.json()
    goals = body.get("goals")
    note = body.get("note")
    if goals is None or not isinstance(goals, list):
        return JSONResponse({"error": "goals_required"}, status_code=400)
    pilot = _get_goal_pilot()
    result = pilot.update_goals(run_id, goals, note=note)
    return JSONResponse(result)


@control_app.post("/control/goal_pilot/runs/{run_id}/actions")
async def goal_pilot_action(run_id: str, request: Request) -> JSONResponse:
    if _orchestrator is None:
        return JSONResponse({"error": "orchestrator_not_initialized"}, status_code=503)
    body = await request.json()
    action = body.get("action")
    if not isinstance(action, dict):
        return JSONResponse({"error": "action_required"}, status_code=400)
    pilot = _get_goal_pilot()
    result = pilot.execute_action(run_id, action, _orchestrator.deps.repository_host)
    return JSONResponse(result)


@control_app.get("/control/goal_pilot/skills")
async def goal_pilot_skills(request: Request) -> JSONResponse:
    if _orchestrator is None:
        return JSONResponse({"error": "orchestrator_not_initialized"}, status_code=503)
    status = request.query_params.get("status")
    pilot = _get_goal_pilot()
    skills = pilot.list_skills(status=status)
    return JSONResponse({"skills": skills})


@control_app.post("/control/goal_pilot/skills")
async def goal_pilot_upsert_skill(request: Request) -> JSONResponse:
    if _orchestrator is None:
        return JSONResponse({"error": "orchestrator_not_initialized"}, status_code=503)
    body = await request.json()
    pilot = _get_goal_pilot()
    skill = pilot.upsert_skill(body)
    return JSONResponse({"skill": skill})


@control_app.post("/control/goal_pilot/skills/export")
async def goal_pilot_export_skills(request: Request) -> JSONResponse:
    if _orchestrator is None:
        return JSONResponse({"error": "orchestrator_not_initialized"}, status_code=503)
    body = await request.json()
    status = body.get("status", "active")
    pilot = _get_goal_pilot()
    result = pilot.export_skills(status=status)
    return JSONResponse(result)


# ======================================================================# Tools API
# ======================================================================# These endpoints provide utilities accessible from the unified dashboard.


@control_app.get("/control/tools/audit")
async def tools_audit(
    repo_root: str = Query(...),
    issue_number: int | None = Query(default=None),
) -> JSONResponse:
    """Audit why issues are queued or blocked.

    Query params:
        repo_root: str - Repository root path
        issue_number: int (optional) - Specific issue to audit

    Returns:
        List of audit entries with issue status and reasons.
    """
    repo_path = _validate_repo_root(repo_root)
    if repo_path is None:
        return JSONResponse({"error": "Invalid repo_root"}, status_code=400)
    result = await _control_actions.audit_cmd.execute(
        AuditActionRequest(repo_root=repo_path, issue_number=issue_number),
    )
    return JSONResponse(result.payload, status_code=result.status_code)


@control_app.get("/control/tools/trace")
async def tools_trace(
    repo_root: str = Query(...),
    issue_number: int = Query(...),
    limit: int = Query(default=100),
) -> JSONResponse:
    """Get trace log entries for a specific issue.

    Query params:
        repo_root: str - Repository root path
        issue_number: int - Issue number to trace
        limit: int - Max lines to return (default: 100)

    Returns:
        List of log entries related to the issue.
    """
    repo_path = _validate_repo_root(repo_root)
    if repo_path is None:
        return JSONResponse({"error": "Invalid repo_root"}, status_code=400)
    result = await _control_actions.trace_cmd.execute(
        TraceActionRequest(repo_root=repo_path, issue_number=issue_number, limit=limit),
    )
    return JSONResponse(result.payload, status_code=result.status_code)


@control_app.post("/control/tools/labels/init")
async def tools_labels_init(request: Request) -> JSONResponse:
    """Initialize or refresh GitHub labels for a repository.

    JSON body:
        repo_root: str - Repository root path

    Returns:
        Summary of created/updated labels.
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_path = _validate_repo_root(body.get("repo_root"))
    if repo_path is None:
        return JSONResponse({"error": "Invalid repo_root"}, status_code=400)

    result = await _control_actions.labels_cmd.execute(RepoActionRequest(repo_root=repo_path))
    return JSONResponse(result.payload, status_code=result.status_code)


@control_app.post("/control/tools/worktrees/cleanup")
async def tools_worktrees_cleanup(request: Request) -> JSONResponse:
    """List stale worktrees (read-only, no deletion).

    This endpoint only LISTS stale worktrees. It does not delete them.
    Users should run `git worktree prune` manually to clean up.

    JSON body:
        repo_root: str - Repository root path

    Returns:
        List of stale worktrees and instructions for cleanup.
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_path = _validate_repo_root(body.get("repo_root"))
    if repo_path is None:
        return JSONResponse({"error": "Invalid repo_root"}, status_code=400)

    result = await _control_actions.stale_worktrees_cmd.execute(
        RepoActionRequest(repo_root=repo_path),
    )
    return JSONResponse(result.payload, status_code=result.status_code)


@control_app.get("/api/session/terminal-recording/{issue_number}")
async def control_terminal_recording(
    issue_number: int,
    offset: int = 0,
    limit: int = 200,
    run_dir: str | None = None,
    round_index: int | None = None,
    session_role: str | None = None,
) -> JSONResponse:
    """Terminal recording endpoint on control center — delegates to shared implementation."""
    from ..entrypoints.web import serve_terminal_recording
    return serve_terminal_recording(
        issue_number, run_dir, offset, limit, round_index, session_role,
    )


@control_app.get("/api/issue-detail/{issue_number}")
async def control_issue_detail(
    issue_number: int,
    repo_root: str = Query(...),
    view: str = Query("user"),
) -> JSONResponse:
    """Issue detail endpoint on control center.

    Reads timeline events from the E2E worktree's timeline.sqlite for E2E
    test issues, then runs them through the same view model pipeline as
    the dashboard's issue-detail endpoint. Returns the same payload shape
    so the existing renderJourneyTimeline JS works without changes.
    """
    from ..execution.timeline_store import SqliteTimelineStore
    from ..infra.e2e_worktree import get_e2e_worktree_path
    from ..timeline import TimelineStream
    from ..view_models.issue_detail import build_issue_detail_view_model

    validated_root = _validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse({"error": "Invalid repo_root"}, status_code=400)

    valid_views = {"user", "ops", "debug"}
    if view not in valid_views:
        view = "user"

    # Try base repo timeline first, then E2E worktree timeline
    candidates = [
        validated_root / ".issue-orchestrator" / "state" / "timeline.sqlite",
        get_e2e_worktree_path(validated_root) / ".issue-orchestrator" / "state" / "timeline.sqlite",
    ]
    records: list = []
    for db_path in candidates:
        if not db_path.exists():
            continue
        try:
            store = SqliteTimelineStore(db_path=db_path)
            found = store.read(issue_number, limit=5000)
            if found:
                records = found
                break
        except Exception:
            logger.debug("Could not read timeline from %s", db_path, exc_info=True)

    if not records:
        return JSONResponse(
            {"error": "not_found", "detail": f"No timeline events for issue {issue_number}"},
            status_code=404,
        )

    stream = TimelineStream.from_records(issue_number, records)
    raw_events = [evt.to_dict() for evt in stream.events]
    filtered_events = _filter_timeline_events(raw_events)
    decorated = _decorate_timeline_events(filtered_events, issue_number)
    phase_toc = _build_phase_toc(decorated)
    cycles = _build_timeline_cycles(decorated)

    payload = build_issue_detail_view_model(
        issue_number=issue_number,
        title=f"Issue #{issue_number}",
        issue_url="",
        events=decorated,
        phase_toc=phase_toc,
        cycles=cycles,
        context=None,
        view=view,
    )
    return JSONResponse(payload)


class ControlAPIServer:
    """Manages the control API server lifecycle."""

    def __init__(self, orchestrator: "Orchestrator", port: int = 19080):
        """Initialize the control API server.

        Args:
            orchestrator: The orchestrator instance to control
            port: Port to listen on (default: 19080 to avoid conflict with web dashboard)
        """
        self.orchestrator = orchestrator
        self.port = port
        self._server: Optional[Any] = None  # uvicorn.Server (imported inside start())
        self._task: Optional[asyncio.Task] = None

    async def start(self) -> None:
        """Start the control API server.

        When self.port is 0, uvicorn binds to an OS-assigned free port.
        After startup, self.port is updated to the actual bound port.
        """
        import uvicorn

        set_orchestrator(self.orchestrator)

        config = uvicorn.Config(
            control_app,
            host="127.0.0.1",
            port=self.port,
            log_level="warning",  # Quiet logging
            access_log=False,
        )
        self._server = uvicorn.Server(config)

        # Run server in background task
        self._task = asyncio.create_task(self._server.serve())

        # Wait for server to be ready (up to 5 seconds)
        for _ in range(50):
            if self._server.started:
                break
            await asyncio.sleep(0.1)

        # Read back the actual bound port (important when port=0)
        if self.port == 0 and self._server.started:
            for s in self._server.servers:
                for sock in s.sockets:
                    addr = sock.getsockname()
                    if isinstance(addr, tuple) and len(addr) >= 2:
                        self.port = addr[1]
                        break

        logger.info(f"Control API started on http://127.0.0.1:{self.port}")

    async def stop(self) -> None:
        """Stop the control API server."""
        if self._server:
            self._server.should_exit = True
        if self._task:
            try:
                await asyncio.wait_for(self._task, timeout=5.0)
            except (asyncio.CancelledError, asyncio.TimeoutError):
                pass
            logger.info("Control API stopped")
