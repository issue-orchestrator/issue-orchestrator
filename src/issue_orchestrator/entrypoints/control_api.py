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
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from fastapi import FastAPI, Request, Query
from fastapi.responses import JSONResponse, HTMLResponse
from sse_starlette.sse import EventSourceResponse

from ..control.worktree_manager import get_worktree_path
from ..control.queue_cache import QueueCache
from ..domain.models import get_completion_path
from ..execution.git_working_copy import GitWorkingCopy
from ..infra.env import ENV_PREFIX
from ..infra import gh_audit
from ..infra.supervisor import DefaultSupervisorOps, MultiInstanceStatus, SupervisorOps
from ..infra.repo_identity import (
    RepoIdentity,
    build_repo_identity_with_status,
    deserialize_repo_identity,
    diff_repo_identity,
)
from ..control.goal_pilot import GoalPilot
from ..execution.control_center_actions import (
    AuditActionRequest,
    ControlCenterActions,
    DoctorActionRequest,
    RefreshActionRequest,
    RepoActionRequest,
    TraceActionRequest,
)

# Path to templates
_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"

if TYPE_CHECKING:
    from ..infra.orchestrator import Orchestrator
    from ..infra.config import Config

logger = logging.getLogger(__name__)
_PREFERRED_REPO_ROOT_ENV = "ISSUE_ORCHESTRATOR_CC_REPO_ROOT"
_EXPECTED_IDENTITY_ENV = "ISSUE_ORCHESTRATOR_EXPECTED_IDENTITY"

# Default E2E config (used when orchestrator not available)
from ..infra.config import E2EConfig
_DEFAULT_E2E_CONFIG = E2EConfig()


def _build_repo_identity(repo_root: Path) -> RepoIdentity:
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


def _get_e2e_config(repo_root: Path | None = None) -> E2EConfig:
    """Get E2E config from orchestrator, falling back to defaults.

    Args:
        repo_root: Optional repo root path (currently unused but kept for API compat)

    Returns:
        E2EConfig instance (from orchestrator or defaults)
    """
    if _orchestrator is not None:
        return _orchestrator.config.e2e
    return _DEFAULT_E2E_CONFIG


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
_shutdown_ops_lock = _threading.Lock()
_global_shutdown_operation: dict[str, Any] | None = None
_engine_shutdown_operations: dict[str, dict[str, Any]] = {}


def _coerce_graceful_timeout_seconds(raw: Any, default: int = 2) -> int:
    """Parse graceful timeout from API payload with safe bounds."""
    if raw is None:
        return default
    try:
        parsed = int(float(raw))
    except (TypeError, ValueError):
        return default
    return min(max(parsed, 2), 3600)


def _global_shutdown_in_progress() -> bool:
    with _shutdown_ops_lock:
        op = _global_shutdown_operation
        if op is None:
            return False
        return op.get("state") == "in_progress"


def _snapshot_shutdown_ops() -> dict[str, Any]:
    with _shutdown_ops_lock:
        return {
            "global_shutdown": dict(_global_shutdown_operation) if _global_shutdown_operation else None,
            "engine_shutdowns": [dict(op) for op in _engine_shutdown_operations.values()],
        }


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


def _track_launched_pids(supervisor_data: dict) -> None:
    """Register launched orchestrator PIDs for zombie reaping.

    Called by control_start after successfully launching orchestrators.
    """
    # Handle multi-instance launches
    if "instances" in supervisor_data:
        for instance in supervisor_data["instances"]:
            if "pid" in instance:
                track_child_pid(instance["pid"])
    # Handle single-instance launches
    elif "pid" in supervisor_data:
        track_child_pid(supervisor_data["pid"])


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


@control_app.post("/control/shutdown")
async def shutdown_control_center(request: Request) -> JSONResponse:
    """Shutdown the control center server.

    This stops the supervisor/control center process itself.
    Optionally stops all running orchestrators first.

    JSON body (optional):
        stop_orchestrators: bool - If True, stop all running orchestrators first
        force_orchestrators: bool - If True, force stop orchestrators when stopping first
    """
    from ..infra.repo_registry import list_repos

    sv = get_supervisor()
    client_host = request.client.host if request.client else "unknown"
    stop_orchestrators, force_orchestrators, graceful_timeout_seconds = await _parse_shutdown_request_body(request)
    begin_result = _begin_global_shutdown_operation(
        stop_orchestrators=stop_orchestrators,
        force_orchestrators=force_orchestrators,
        graceful_timeout_seconds=graceful_timeout_seconds,
    )
    if isinstance(begin_result, JSONResponse):
        return begin_result
    global_op_id, superseded_engine_shutdowns = begin_result

    logger.info(
        "Shutdown requested (force): source=web_ui, client=%s, stop_orchestrators=%s, force_orchestrators=%s, pid=%d",
        client_host,
        stop_orchestrators,
        force_orchestrators,
        os.getpid(),
    )

    if not stop_orchestrators:
        _complete_global_shutdown_without_orchestrators()
        return _shutdown_started_response(
            operation_id=global_op_id,
            superseded_engine_shutdowns=superseded_engine_shutdowns,
            graceful_timeout_seconds=graceful_timeout_seconds,
        )

    _start_global_shutdown_worker(
        operation_id=global_op_id,
        supervisor=sv,
        list_repos_fn=list_repos,
    )
    return _shutdown_started_response(
        operation_id=global_op_id,
        superseded_engine_shutdowns=superseded_engine_shutdowns,
        graceful_timeout_seconds=graceful_timeout_seconds,
    )


async def _parse_shutdown_request_body(request: Request) -> tuple[bool, bool, int]:
    stop_orchestrators = False
    force_orchestrators = False
    graceful_timeout_seconds = 2
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return stop_orchestrators, force_orchestrators, graceful_timeout_seconds

    stop_orchestrators = bool(body.get("stop_orchestrators", False))
    force_orchestrators = bool(body.get("force_orchestrators", False))
    graceful_timeout_seconds = _coerce_graceful_timeout_seconds(
        body.get("graceful_timeout_seconds"),
        default=2,
    )
    return stop_orchestrators, force_orchestrators, graceful_timeout_seconds


def _begin_global_shutdown_operation(
    *,
    stop_orchestrators: bool,
    force_orchestrators: bool,
    graceful_timeout_seconds: int,
) -> tuple[str, list[str]] | JSONResponse:
    global _global_shutdown_operation

    superseded_engine_shutdowns: list[str] = []
    global_op_id = f"shutdown-{int(time.time() * 1000)}"
    with _shutdown_ops_lock:
        if _global_shutdown_operation and _global_shutdown_operation.get("state") == "in_progress":
            return JSONResponse(
                {
                    "error": "shutdown_in_progress",
                    "detail": "Global shutdown is already in progress.",
                    "operation_id": _global_shutdown_operation.get("operation_id"),
                },
                status_code=409,
            )
        if stop_orchestrators and _engine_shutdown_operations:
            superseded_engine_shutdowns = sorted(_engine_shutdown_operations.keys())
            _engine_shutdown_operations.clear()
        _global_shutdown_operation = {
            "operation_id": global_op_id,
            "state": "in_progress",
            "started_at_epoch": time.time(),
            "stop_orchestrators": bool(stop_orchestrators),
            "force_orchestrators": bool(force_orchestrators),
            "graceful_timeout_seconds": graceful_timeout_seconds,
            "superseded_engine_shutdowns": superseded_engine_shutdowns,
            "current_repo": None,
            "total_repos": 0,
            "completed_repos": 0,
            "stopped_orchestrators": [],
            "failed_orchestrators": [],
            "abort_requested": False,
            "force_now_requested": False,
        }
    return global_op_id, superseded_engine_shutdowns


def _complete_global_shutdown_without_orchestrators() -> None:
    global _global_shutdown_operation
    with _shutdown_ops_lock:
        if _global_shutdown_operation:
            _global_shutdown_operation["state"] = "completed"
    _schedule_control_center_exit()


def _shutdown_started_response(
    *,
    operation_id: str,
    superseded_engine_shutdowns: list[str],
    graceful_timeout_seconds: int,
) -> JSONResponse:
    return JSONResponse({
        "status": "shutting_down",
        "stopped_orchestrators": [],
        "superseded_engine_shutdowns": superseded_engine_shutdowns,
        "graceful_timeout_seconds": graceful_timeout_seconds,
        "operation_id": operation_id,
    })


def _start_global_shutdown_worker(*, operation_id: str, supervisor: Any, list_repos_fn: Any) -> None:
    import threading

    def _worker() -> None:
        _run_global_shutdown_worker(
            operation_id=operation_id,
            supervisor=supervisor,
            list_repos_fn=list_repos_fn,
        )

    threading.Thread(target=_worker, daemon=True).start()


def _run_global_shutdown_worker(*, operation_id: str, supervisor: Any, list_repos_fn: Any) -> None:
    stopped_repos: list[str] = []
    failed_repos: list[str] = []
    try:
        repos = list_repos_fn()
        _set_shutdown_total_repos(operation_id=operation_id, total_repos=len(repos))
        for repo in repos:
            result = _process_shutdown_repo(
                operation_id=operation_id,
                repo_path=repo.path,
                supervisor=supervisor,
            )
            if result == "aborted":
                _record_shutdown_abort(
                    operation_id=operation_id,
                    stopped_repos=stopped_repos,
                    failed_repos=failed_repos,
                )
                return
            if result == "stopped":
                stopped_repos.append(repo.path)
            elif result == "failed":
                failed_repos.append(repo.path)
            _increment_shutdown_completed_repos(operation_id=operation_id)
        _record_shutdown_completion(
            operation_id=operation_id,
            stopped_repos=stopped_repos,
            failed_repos=failed_repos,
        )
    except Exception:
        logger.exception("Global shutdown worker failed")
        _record_shutdown_failure(operation_id=operation_id)


def _process_shutdown_repo(*, operation_id: str, repo_path: str, supervisor: Any) -> str:
    path = Path(repo_path)
    if not path.exists():
        return "skipped"

    runtime = _resolve_shutdown_runtime(operation_id=operation_id, repo_path=repo_path)
    if runtime is None:
        return "aborted"
    timeout_seconds, force_now = runtime

    status_info = supervisor.status(path)
    if status_info.state != "running":
        return "skipped"
    logger.info("Stopping orchestrator for %s before shutdown", repo_path)
    stopped_count = supervisor.stop_all_instances(
        path,
        force=force_now,
        graceful_timeout_seconds=timeout_seconds,
        force_if_graceful_fails=True,
    )
    return "stopped" if stopped_count > 0 else "failed"


def _resolve_shutdown_runtime(*, operation_id: str, repo_path: str) -> tuple[int, bool] | None:
    with _shutdown_ops_lock:
        op = _global_shutdown_operation
        if not op or op.get("operation_id") != operation_id:
            return None
        if op.get("abort_requested"):
            return None
        op["current_repo"] = repo_path
        timeout_seconds = _coerce_graceful_timeout_seconds(op.get("graceful_timeout_seconds"), default=2)
        force_now = bool(op.get("force_orchestrators") or op.get("force_now_requested"))
    return timeout_seconds, force_now


def _set_shutdown_total_repos(*, operation_id: str, total_repos: int) -> None:
    with _shutdown_ops_lock:
        op = _global_shutdown_operation
        if op and op.get("operation_id") == operation_id:
            op["total_repos"] = total_repos


def _increment_shutdown_completed_repos(*, operation_id: str) -> None:
    with _shutdown_ops_lock:
        op = _global_shutdown_operation
        if not op or op.get("operation_id") != operation_id:
            return
        op["completed_repos"] = int(op.get("completed_repos", 0)) + 1


def _record_shutdown_abort(*, operation_id: str, stopped_repos: list[str], failed_repos: list[str]) -> None:
    with _shutdown_ops_lock:
        op = _global_shutdown_operation
        if not op or op.get("operation_id") != operation_id:
            return
        op["state"] = "aborted"
        op["current_repo"] = None
        op["stopped_orchestrators"] = stopped_repos
        op["failed_orchestrators"] = failed_repos


def _record_shutdown_completion(*, operation_id: str, stopped_repos: list[str], failed_repos: list[str]) -> None:
    with _shutdown_ops_lock:
        op = _global_shutdown_operation
        if not op or op.get("operation_id") != operation_id:
            return
        op["stopped_orchestrators"] = stopped_repos
        op["failed_orchestrators"] = failed_repos
        op["current_repo"] = None
        if op.get("state") == "in_progress":
            op["state"] = "failed" if failed_repos else "completed"
    if not failed_repos:
        _schedule_control_center_exit()


def _record_shutdown_failure(*, operation_id: str) -> None:
    with _shutdown_ops_lock:
        op = _global_shutdown_operation
        if not op or op.get("operation_id") != operation_id:
            return
        op["state"] = "failed"
        op["current_repo"] = None


@control_app.get("/control/shutdown/state")
async def shutdown_state() -> JSONResponse:
    """Return current shutdown operation state for UI feedback."""
    return JSONResponse(_snapshot_shutdown_ops())


@control_app.post("/control/shutdown/abort")
async def shutdown_abort() -> JSONResponse:
    """Request abort of an in-progress global shutdown operation."""
    with _shutdown_ops_lock:
        op = _global_shutdown_operation
        if not op or op.get("state") != "in_progress":
            return JSONResponse(
                {"error": "no_shutdown_in_progress", "detail": "No global shutdown is in progress."},
                status_code=409,
            )
        op["abort_requested"] = True
    return JSONResponse({"status": "abort_requested"})


@control_app.post("/control/shutdown/update")
async def shutdown_update(request: Request) -> JSONResponse:
    """Update timeout/force policy for an in-progress global shutdown."""
    body: dict[str, Any] = {}
    try:
        payload = await request.json()
        if isinstance(payload, dict):
            body = payload
    except json.JSONDecodeError:
        body = {}

    timeout_seconds = _coerce_graceful_timeout_seconds(body.get("graceful_timeout_seconds"), default=2)
    has_force_override = "force_orchestrators" in body
    requested_force = bool(body.get("force_orchestrators", False))
    with _shutdown_ops_lock:
        op = _global_shutdown_operation
        if not op or op.get("state") != "in_progress":
            return JSONResponse(
                {"error": "no_shutdown_in_progress", "detail": "No global shutdown is in progress."},
                status_code=409,
            )
        op["graceful_timeout_seconds"] = timeout_seconds
        if has_force_override:
            op["force_orchestrators"] = requested_force

    return JSONResponse(
        {
            "status": "updated",
            "graceful_timeout_seconds": timeout_seconds,
            "force_orchestrators": bool(op.get("force_orchestrators", False)),
        }
    )


@control_app.post("/control/shutdown/force")
async def shutdown_force_now() -> JSONResponse:
    """Request force escalation for an in-progress global shutdown."""
    with _shutdown_ops_lock:
        op = _global_shutdown_operation
        if not op or op.get("state") != "in_progress":
            return JSONResponse(
                {"error": "no_shutdown_in_progress", "detail": "No global shutdown is in progress."},
                status_code=409,
            )
        op["force_now_requested"] = True
        op["force_orchestrators"] = True
    return JSONResponse({"status": "force_requested"})


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


def _get_selected_config(repo_root: Path) -> str:
    """Return the selected config name for a repo, defaulting to default.yaml."""
    from ..infra.repo_registry import load_registry

    registry = load_registry()
    normalized = str(repo_root.resolve())
    for repo in registry.repos:
        if repo.path == normalized:
            return repo.selected_config or "default.yaml"
    return "default.yaml"


def _load_config_port(repo_root: Path, config_name: str) -> int | None:
    """Load the web port from a repo config."""
    from ..infra.config import Config, get_config_path

    config_path = get_config_path(repo_root, config_name)
    if not config_path.exists():
        return None
    try:
        config = Config.load(config_path)
    except Exception:
        return None
    return config.web_port


def _detect_orchestrator_by_port(
    repo_root: Path,
    config_name: str,
    *,
    expected_identity: RepoIdentity | None = None,
) -> dict[str, Any] | None:
    """Detect an orchestrator by probing the configured port.

    Returns info dict with port and metadata if an orchestrator responds
    and matches repo_root.
    """
    import httpx

    port = _load_config_port(repo_root, config_name)
    if not port:
        return None

    base_url = f"http://127.0.0.1:{port}"
    try:
        resp = httpx.get(f"{base_url}/api/info", timeout=0.6)
        if resp.status_code != 200:
            return None
        info = resp.json()
        if info.get("repo_root") != str(repo_root):
            return None
    except Exception:
        return None

    details: dict[str, Any] = {"port": port, "info": info}
    _annotate_identity_mismatch(details, info, expected_identity)
    _annotate_orchestrator_health(details, base_url)

    return details


def _annotate_identity_mismatch(
    details: dict[str, Any],
    info: dict[str, Any],
    expected_identity: RepoIdentity | None,
) -> None:
    if expected_identity is None:
        return
    observed_identity_payload = info.get("repo_identity")
    if not isinstance(observed_identity_payload, dict):
        return
    observed_identity = RepoIdentity(
        repo_root=str(observed_identity_payload.get("repo_root", "")),
        commit_sha=(str(observed_identity_payload["commit_sha"]) if observed_identity_payload.get("commit_sha") else None),
        branch=(str(observed_identity_payload["branch"]) if observed_identity_payload.get("branch") else None),
        working_tree_dirty=bool(observed_identity_payload.get("working_tree_dirty", False)),
        dirty_fingerprint=(str(observed_identity_payload["dirty_fingerprint"]) if observed_identity_payload.get("dirty_fingerprint") else None),
        source_root=(str(observed_identity_payload["source_root"]) if observed_identity_payload.get("source_root") else None),
    )
    identity_mismatch = diff_repo_identity(expected_identity, observed_identity)
    for volatile_field in ("working_tree_dirty", "dirty_fingerprint"):
        identity_mismatch.pop(volatile_field, None)
    if identity_mismatch:
        details["identity_mismatch"] = identity_mismatch
        details["observed_identity"] = observed_identity.to_dict()
        details["expected_identity"] = expected_identity.to_dict()


def _annotate_orchestrator_health(details: dict[str, Any], base_url: str) -> None:
    import httpx
    import time

    try:
        status_resp = httpx.get(f"{base_url}/api/status", timeout=0.6)
        if status_resp.status_code != 200:
            return
        status_data = status_resp.json()
        details["status"] = status_data
        last_tick = status_data.get("last_tick_time")
        if not isinstance(last_tick, (int, float)) or last_tick <= 0:
            return
        tick_age = time.time() - last_tick
        details["tick_age_seconds"] = tick_age
        details["health"] = "stale" if tick_age > 120 else "ok"
    except Exception:
        details.setdefault("health", "unknown")


def _confirm_orchestrator_at_port(repo_root: Path, port: int) -> bool:
    """Confirm the orchestrator at a port belongs to the repo_root."""
    import httpx

    try:
        resp = httpx.get(f"http://127.0.0.1:{port}/api/info", timeout=0.6)
        if resp.status_code != 200:
            return False
        info = resp.json()
        return info.get("repo_root") == str(repo_root)
    except Exception:
        return False


def _is_shutdown_complete(port: int | None) -> bool:
    """Check if an orchestrator is in shutdown-complete state.

    Returns True if shutdown_requested=True and no active sessions.
    """
    if not port:
        return False
    try:
        import httpx
        resp = httpx.get(f"http://127.0.0.1:{port}/api/status", timeout=2.0)
        if resp.status_code == 200:
            data = resp.json()
            shutdown_requested = data.get("shutdown_requested", False)
            active_sessions = data.get("active_sessions", [])
            return shutdown_requested and len(active_sessions) == 0
    except Exception:
        pass
    return False


@control_app.post("/control/orchestrator/start")
async def control_start(request: Request) -> JSONResponse:  # noqa: C901, PLR0912 - orchestrator startup with config validation and initialization
    """Start an orchestrator for a repository.

    JSON body:
        repo_root: str - Repository root path
        config_name: str (optional) - Config file name (default: default.yaml)
        force_restart: bool (optional) - Force restart if an untracked orchestrator is detected

    If the orchestrator is in shutdown-complete state (shutdown requested,
    no active sessions), it will be automatically restarted.
    """
    from ..infra.repo_lock import AlreadyRunning
    from ..infra.repo_registry import set_selected_config

    sv = get_supervisor()

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = _validate_repo_root(body.get("repo_root"))
    if repo_root is None:
        return JSONResponse(
            {"error": "Invalid or missing repo_root"},
            status_code=400,
        )

    # Validate port if provided
    port = body.get("port")
    if port is not None:
        if not isinstance(port, int) or port < 1 or port > 65535:
            return JSONResponse({"error": "Invalid port"}, status_code=400)

    config_name = body.get("config_name", "default.yaml")
    if not config_name.endswith(".yaml"):
        config_name += ".yaml"
    force_restart = bool(body.get("force_restart", False))
    expected_identity = _build_repo_identity(repo_root)

    try:
        detected = _detect_orchestrator_by_port(
            repo_root,
            config_name,
            expected_identity=expected_identity,
        )
        if detected and detected.get("identity_mismatch"):
            stopped = sv.stop_by_port(detected["port"], force=True)
            if not stopped:
                return JSONResponse({
                    "error": "engine_identity_mismatch",
                    "detail": "Mismatched engine detected and could not be stopped",
                    "port": detected["port"],
                    "expected_identity": detected.get("expected_identity"),
                    "observed_identity": detected.get("observed_identity"),
                    "identity_mismatch": detected.get("identity_mismatch"),
                }, status_code=409)
        elif detected and not force_restart:
            return JSONResponse({
                "error": "orphaned_running",
                "status": "running",
                "port": detected["port"],
                "repo_root": str(repo_root),
                "health": detected.get("health", "unknown"),
                "tick_age_seconds": detected.get("tick_age_seconds"),
            }, status_code=409)
        if detected and force_restart:
            stopped = sv.stop_by_port(detected["port"], force=True)
            if not stopped:
                return JSONResponse({
                    "error": "stop_failed",
                    "detail": "Unable to stop existing orchestrator process.",
                }, status_code=500)

        # Update selected config in registry
        set_selected_config(repo_root, config_name)

        # Load config and run unified launcher (doctor + supervisor.start)
        from ..infra.config import Config, get_config_path
        from ..infra.launcher import launch_subprocess

        config_path = get_config_path(repo_root, config_name)
        config = Config.load(config_path)

        launch_result = launch_subprocess(
            repo_root=repo_root,
            config=config,
            config_name=config_name,
            supervisor_ops=sv,
            expected_identity=expected_identity.to_dict(),
        )

        if launch_result.status == "doctor_error":
            return JSONResponse({
                "error": "doctor_failed",
                "detail": "Pre-flight checks failed",
                "doctor": launch_result.doctor.to_dict(),
            }, status_code=422)

        if launch_result.status == "already_running":
            response = {
                "error": "already_running",
                "detail": launch_result.error or "Orchestrator already running",
                "doctor": launch_result.doctor.to_dict(),
            }
            if launch_result.supervisor:
                response.update(launch_result.supervisor)
            return JSONResponse(response, status_code=409)

        if not launch_result.launched:
            return JSONResponse({
                "error": "launch_failed",
                "detail": launch_result.error or "Unknown launch error",
                "doctor": launch_result.doctor.to_dict(),
            }, status_code=500)

        response_data: dict = {
            "status": "started",
            "repo_root": str(repo_root),
            "config_name": config_name,
            "repo_identity": expected_identity.to_dict(),
            "doctor": launch_result.doctor.to_dict(),
        }
        if launch_result.supervisor:
            response_data.update(launch_result.supervisor)
            # Track PIDs for zombie reaping (control center only)
            _track_launched_pids(launch_result.supervisor)
        return JSONResponse(response_data)
    except FileNotFoundError as e:
        return JSONResponse({
            "error": "config_not_found",
            "detail": str(e),
        }, status_code=404)
    except AlreadyRunning as e:
        # Check if the running orchestrator is in shutdown-complete state
        if _is_shutdown_complete(e.port):
            # Stop the old instance and restart
            logger.info("Orchestrator in shutdown-complete state, restarting: %s", repo_root)
            try:
                sv.stop(repo_root)
                # Brief pause to allow cleanup
                import time
                time.sleep(0.5)
                # Try starting again
                info = sv.start(
                    repo_root,
                    config_name=config_name,
                    expected_identity=expected_identity.to_dict(),
                )
                # Track PID for zombie reaping
                _track_launched_pids({"pid": info.pid})
                return JSONResponse({
                    "status": "restarted",
                    "pid": info.pid,
                    "port": info.http_port,
                    "repo_root": str(repo_root),
                    "config_name": config_name,
                })
            except Exception as restart_err:
                logger.exception("Failed to restart orchestrator for %s", repo_root)
                return JSONResponse({
                    "error": "restart_failed",
                    "detail": str(restart_err),
                }, status_code=500)
        # Not in shutdown-complete state, return already_running error
        return JSONResponse({
            "error": "already_running",
            "pid": e.pid,
            "port": e.port,
            "repo_root": str(e.repo_root),
        }, status_code=409)
    except Exception as e:
        logger.exception("Failed to start orchestrator for %s", repo_root)
        return JSONResponse({
            "error": "start_failed",
            "detail": str(e),
        }, status_code=500)


@control_app.post("/control/orchestrator/stop")
async def control_stop(request: Request) -> JSONResponse:
    """Stop the orchestrator for a repository.

    JSON body:
        repo_root: str - Repository root path
        force: bool (optional) - Force kill (SIGKILL) instead of graceful (SIGTERM)
        port: int (optional) - Port to stop when no lock exists (untracked process)
    """
    sv = get_supervisor()

    logger.info("[control_stop] Received stop request")

    try:
        body = await request.json()
        logger.info("[control_stop] Body: %s", body)
    except json.JSONDecodeError:
        logger.error("[control_stop] Invalid JSON")
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = _validate_repo_root(body.get("repo_root"))
    if repo_root is None:
        logger.error("[control_stop] Invalid repo_root: %s", body.get("repo_root"))
        return JSONResponse(
            {"error": "Invalid or missing repo_root"},
            status_code=400,
        )

    force = body.get("force", False)
    force_if_timeout = bool(body.get("force_if_timeout", True))
    graceful_timeout_seconds = _coerce_graceful_timeout_seconds(
        body.get("graceful_timeout_seconds"),
        default=2,
    )
    port_override = body.get("port")
    if port_override is not None and (not isinstance(port_override, int) or port_override < 1 or port_override > 65535):
        return JSONResponse({"error": "Invalid port"}, status_code=400)

    if _global_shutdown_in_progress():
        return JSONResponse({
            "error": "global_shutdown_in_progress",
            "detail": "Global shutdown is in progress and already controls engine shutdown behavior.",
            "actions": [
                "View global shutdown status",
                "Change global shutdown",
                "Abort global shutdown",
            ],
        }, status_code=409)

    repo_key = str(repo_root)
    with _shutdown_ops_lock:
        _engine_shutdown_operations[repo_key] = {
            "repo_root": repo_key,
            "state": "in_progress",
            "started_at_epoch": time.time(),
            "force": bool(force),
            "force_if_timeout": force_if_timeout,
            "graceful_timeout_seconds": graceful_timeout_seconds,
        }

    logger.info("[control_stop] Calling supervisor.stop(%s, force=%s)", repo_root, force)

    try:
        status_info = sv.status(repo_root)
        if status_info.state != "running" and port_override:
            if not _confirm_orchestrator_at_port(repo_root, port_override):
                return JSONResponse({
                    "error": "port_mismatch",
                    "detail": "No matching orchestrator found on the provided port.",
                }, status_code=409)
            stopped = sv.stop_by_port(port_override, force=force)
            stopped_count = 1 if stopped else 0
        else:
            # Stop all instances (single and multi-instance)
            stopped_count = sv.stop_all_instances(
                repo_root,
                force=force,
                graceful_timeout_seconds=graceful_timeout_seconds,
                force_if_graceful_fails=force_if_timeout or force,
            )
            stopped = stopped_count > 0
        logger.info("[control_stop] supervisor.stop_all_instances returned: %d", stopped_count)

        if stopped:
            return JSONResponse({
                "status": "stopped",
                "repo_root": str(repo_root),
                "stopped_count": stopped_count,
            })
        return JSONResponse({
            "status": "not_running",
            "repo_root": str(repo_root),
        })
    finally:
        with _shutdown_ops_lock:
            _engine_shutdown_operations.pop(repo_key, None)


@control_app.post("/control/orchestrator/reconcile")
async def control_reconcile(request: Request) -> JSONResponse:
    """Reconcile stale runtime metadata and optionally stop orphaned/unresponsive engines.

    JSON body:
        stop_orphaned: bool (optional, default false)
        stop_unresponsive: bool (optional, default false)
        force: bool (optional, default false)
    """
    sv = get_supervisor()
    from ..infra.repo_registry import list_repos

    stop_orphaned, stop_unresponsive, force = await _parse_reconcile_options(request)

    reconciled_stale_locks: list[str] = []
    orphaned_detected: list[dict[str, Any]] = []
    stopped_orphaned: list[str] = []
    unresponsive_detected: list[dict[str, Any]] = []
    stopped_unresponsive: list[str] = []

    for repo in list_repos():
        reconciliation = _reconcile_repo_runtime(
            sv=sv,
            repo_path=Path(repo.path),
            selected_config=repo.selected_config or "default.yaml",
            stop_orphaned=stop_orphaned,
            stop_unresponsive=stop_unresponsive,
            force=force,
        )
        if reconciliation is None:
            continue

        if reconciliation["reconciled_stale_lock"]:
            reconciled_stale_locks.append(repo.path)
        orphaned_detected.extend(reconciliation["orphaned_detected"])
        if reconciliation["stopped_orphaned"]:
            stopped_orphaned.append(repo.path)
        unresponsive_detected.extend(reconciliation["unresponsive_detected"])
        if reconciliation["stopped_unresponsive"]:
            stopped_unresponsive.append(repo.path)

    return JSONResponse({
        "status": "ok",
        "reconciled_stale_locks": reconciled_stale_locks,
        "orphaned_detected": orphaned_detected,
        "stopped_orphaned": stopped_orphaned,
        "unresponsive_detected": unresponsive_detected,
        "stopped_unresponsive": stopped_unresponsive,
    })


async def _parse_reconcile_options(request: Request) -> tuple[bool, bool, bool]:
    stop_orphaned = False
    stop_unresponsive = False
    force = False
    try:
        body = await request.json()
        if isinstance(body, dict):
            stop_orphaned = bool(body.get("stop_orphaned", False))
            stop_unresponsive = bool(body.get("stop_unresponsive", False))
            force = bool(body.get("force", False))
    except Exception:
        pass
    return stop_orphaned, stop_unresponsive, force


def _reconcile_repo_runtime(
    *,
    sv: SupervisorOps,
    repo_path: Path,
    selected_config: str,
    stop_orphaned: bool,
    stop_unresponsive: bool,
    force: bool,
) -> dict[str, Any] | None:
    """Reconcile one repository and return aggregated reconciliation outcomes."""
    if not repo_path.exists():
        return None

    multi_status = sv.status_all_instances(repo_path, config_name=selected_config)
    if _is_multi_instance_repo(multi_status):
        return _reconcile_multi_instance_repo_runtime(
            sv=sv,
            repo_path=repo_path,
            multi_status=multi_status,
            stop_unresponsive=stop_unresponsive,
            force=force,
        )

    status_info = sv.status(repo_path)

    if status_info.state == "failed":
        return {
            "reconciled_stale_lock": sv.stop(repo_path, force=False),
            "orphaned_detected": [],
            "stopped_orphaned": False,
            "unresponsive_detected": [],
            "stopped_unresponsive": False,
        }

    if status_info.state != "running":
        detected = _detect_orchestrator_by_port(repo_path, selected_config)
        if not detected:
            return {
                "reconciled_stale_lock": False,
                "orphaned_detected": [],
                "stopped_orphaned": False,
                "unresponsive_detected": [],
                "stopped_unresponsive": False,
            }
        orphaned_entry = {"repo_root": str(repo_path), "port": detected.get("port")}
        if stop_orphaned and detected.get("port"):
            stopped = sv.stop_by_port(int(detected["port"]), force=force)
            return {
                "reconciled_stale_lock": False,
                "orphaned_detected": [orphaned_entry],
                "stopped_orphaned": stopped,
                "unresponsive_detected": [],
                "stopped_unresponsive": False,
            }
        return {
            "reconciled_stale_lock": False,
            "orphaned_detected": [orphaned_entry],
            "stopped_orphaned": False,
            "unresponsive_detected": [],
            "stopped_unresponsive": False,
        }

    payload = _enrich_runtime_health(repo_path, status_info.to_dict())
    if payload is None or payload.get("runtime_health") != "unresponsive":
        return {
            "reconciled_stale_lock": False,
            "orphaned_detected": [],
            "stopped_orphaned": False,
            "unresponsive_detected": [],
            "stopped_unresponsive": False,
        }

    unresponsive_entry = {
        "repo_root": str(repo_path),
        "instance_id": None,
        "heartbeat_age_seconds": payload.get("heartbeat_age_seconds"),
        "pid": payload.get("pid"),
        "port": payload.get("port"),
    }
    if stop_unresponsive:
        return {
            "reconciled_stale_lock": False,
            "orphaned_detected": [],
            "stopped_orphaned": False,
            "unresponsive_detected": [unresponsive_entry],
            "stopped_unresponsive": sv.stop(repo_path, force=force),
        }
    return {
        "reconciled_stale_lock": False,
        "orphaned_detected": [],
        "stopped_orphaned": False,
        "unresponsive_detected": [unresponsive_entry],
        "stopped_unresponsive": False,
    }


def _is_multi_instance_repo(multi_status: MultiInstanceStatus) -> bool:
    return multi_status.expected_count > 1 or any(inst.instance_id is not None for inst in multi_status.instances)


def _reconcile_multi_instance_repo_runtime(
    *,
    sv: SupervisorOps,
    repo_path: Path,
    multi_status: MultiInstanceStatus,
    stop_unresponsive: bool,
    force: bool,
) -> dict[str, Any]:
    """Reconcile a multi-instance repository.

    Orphaned process probing is skipped in multi-instance mode because a single
    config-level port probe is ambiguous across N orchestrator instances.
    """
    reconciled_stale_lock = False
    unresponsive_detected: list[dict[str, Any]] = []
    stopped_unresponsive = False

    instance_ids: list[str | None] = [None]
    instance_ids.extend(f"orchestrator-{i}" for i in range(1, multi_status.expected_count + 1))
    instance_ids.extend(
        inst.instance_id
        for inst in multi_status.instances
        if inst.instance_id is not None
    )

    # Deduplicate while preserving order.
    deduped_ids: list[str | None] = []
    for instance_id in instance_ids:
        if instance_id not in deduped_ids:
            deduped_ids.append(instance_id)

    for instance_id in deduped_ids:
        status_info = sv.status(repo_path, instance_id=instance_id)

        if status_info.state == "failed":
            if sv.stop(repo_path, force=False, instance_id=instance_id):
                reconciled_stale_lock = True
            continue

        if status_info.state != "running":
            continue

        payload = _enrich_runtime_health(repo_path, status_info.to_dict(), instance_id=instance_id)
        if payload is None or payload.get("runtime_health") != "unresponsive":
            continue

        unresponsive_detected.append({
            "repo_root": str(repo_path),
            "instance_id": instance_id,
            "heartbeat_age_seconds": payload.get("heartbeat_age_seconds"),
            "pid": payload.get("pid"),
            "port": payload.get("port"),
        })

        if not stop_unresponsive:
            continue

        port = payload.get("port")
        stopped = sv.stop_by_port(port, force=force) if isinstance(port, int) else sv.stop(
            repo_path,
            force=force,
            instance_id=instance_id,
        )
        if stopped:
            stopped_unresponsive = True

    return {
        "reconciled_stale_lock": reconciled_stale_lock,
        "orphaned_detected": [],
        "stopped_orphaned": False,
        "unresponsive_detected": unresponsive_detected,
        "stopped_unresponsive": stopped_unresponsive,
    }


@control_app.get("/control/orchestrator/status")
async def control_status(
    repo_root: str = Query(...),
    config_name: str | None = Query(None),
) -> JSONResponse:
    """Get the status of the orchestrator for a repository.

    Query params:
        repo_root: str - Repository root path
        config_name: str (optional) - Config name to probe for untracked processes

    Returns either a single status dict (legacy) or multi-instance status when
    instances > 1 in config.
    """
    sv = get_supervisor()

    path = _validate_repo_root(repo_root)
    if path is None:
        return JSONResponse(
            {"error": "Invalid or missing repo_root"},
            status_code=400,
        )

    # Get status of all instances
    selected = config_name or _get_selected_config(path) or "default.yaml"
    multi_status = sv.status_all_instances(path, config_name=selected)

    # If multiple instances expected or running, return multi-instance format
    if multi_status.expected_count > 1 or len(multi_status.instances) > 1:
        return JSONResponse({
            "multi_instance": True,
            "repo_root": str(path),
            "expected_count": multi_status.expected_count,
            "running_count": sum(1 for s in multi_status.instances if s.state == "running"),
            "instances": [s.to_dict() for s in multi_status.instances],
        })

    # Single instance mode - return backward-compatible format
    # Check if we have exactly one running instance
    if multi_status.instances and len(multi_status.instances) == 1:
        payload = _enrich_runtime_health(path, multi_status.instances[0].to_dict())
        return JSONResponse(payload or multi_status.instances[0].to_dict())

    # No running instances - check for orphaned process
    status_info = sv.status(path)
    if status_info.state != "running":
        detected = _detect_orchestrator_by_port(path, selected)
        if detected:
            status_data = detected.get("status", {})
            orphaned_payload = {
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
            return JSONResponse(_enrich_runtime_health(path, orphaned_payload, orphaned=True) or orphaned_payload)

    payload = _enrich_runtime_health(path, status_info.to_dict())
    return JSONResponse(payload or status_info.to_dict())


@control_app.post("/control/orchestrator/pause")
async def control_pause(request: Request) -> JSONResponse:
    """Pause the orchestrator for a repository (passthrough to running instance).

    JSON body:
        repo_root: str - Repository root path
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = _validate_repo_root(body.get("repo_root"))
    if repo_root is None:
        return JSONResponse(
            {"error": "Invalid or missing repo_root"},
            status_code=400,
        )

    result = await _control_actions.pause_cmd.execute(RepoActionRequest(repo_root=repo_root))
    return JSONResponse(result.payload, status_code=result.status_code)


@control_app.post("/control/orchestrator/resume")
async def control_resume(request: Request) -> JSONResponse:
    """Resume the orchestrator for a repository (passthrough to running instance).

    JSON body:
        repo_root: str - Repository root path
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = _validate_repo_root(body.get("repo_root"))
    if repo_root is None:
        return JSONResponse(
            {"error": "Invalid or missing repo_root"},
            status_code=400,
        )

    result = await _control_actions.resume_cmd.execute(RepoActionRequest(repo_root=repo_root))
    return JSONResponse(result.payload, status_code=result.status_code)


@control_app.post("/control/orchestrator/refresh")
async def control_refresh(request: Request) -> JSONResponse:
    """Trigger refresh on the orchestrator for a repository (passthrough).

    JSON body:
        repo_root: str - Repository root path
        inflight_stable_ids: list[str] (optional) - Expected issue IDs
    """
    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = _validate_repo_root(body.get("repo_root"))
    if repo_root is None:
        return JSONResponse(
            {"error": "Invalid or missing repo_root"},
            status_code=400,
        )

    result = await _control_actions.refresh_cmd.execute(
        RefreshActionRequest(
            repo_root=repo_root,
            inflight_stable_ids=body.get("inflight_stable_ids"),
        ),
    )
    return JSONResponse(result.payload, status_code=result.status_code)


@control_app.get("/control/orchestrator/last_failure")
async def control_last_failure(repo_root: str = Query(...)) -> JSONResponse:
    """Get the last startup failure for a repository.

    Query params:
        repo_root: str - Repository root path
    """
    from ..infra.repo_identity import state_dir

    path = _validate_repo_root(repo_root)
    if path is None:
        return JSONResponse(
            {"error": "Invalid or missing repo_root"},
            status_code=400,
        )

    failure_path = state_dir(path) / "last_failure.json"
    if not failure_path.exists():
        return JSONResponse({"last_failure": None})

    try:
        with open(failure_path) as f:
            data = json.load(f)
        return JSONResponse({"last_failure": data})
    except (json.JSONDecodeError, OSError) as e:
        return JSONResponse({
            "error": "read_failed",
            "detail": str(e),
        }, status_code=500)


@control_app.get("/control/orchestrator/doctor")
async def control_doctor(repo_root: str = Query(...)) -> JSONResponse:
    """Run diagnostics for a repository.

    Query params:
        repo_root: str - Repository root path
    """
    path = _validate_repo_root(repo_root)
    if path is None:
        return JSONResponse(
            {"error": "Invalid or missing repo_root"},
            status_code=400,
        )
    result = await _control_actions.doctor_cmd.execute(DoctorActionRequest(repo_root=path))
    return JSONResponse(result.payload, status_code=result.status_code)


@control_app.post("/control/orchestrator/ai_diagnose")
async def control_ai_diagnose(request: Request) -> JSONResponse:
    """Run AI-powered diagnostics for a repository.

    JSON body:
        repo_root: str - Repository root path
        timeout: int (optional) - Timeout in seconds (default: 120)
    """
    from ..infra.ai_diagnose import run_ai_diagnose

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = _validate_repo_root(body.get("repo_root"))
    if repo_root is None:
        return JSONResponse(
            {"error": "Invalid or missing repo_root"},
            status_code=400,
        )

    timeout = body.get("timeout", 120)
    if not isinstance(timeout, int) or timeout < 10 or timeout > 600:
        timeout = 120

    result = run_ai_diagnose(repo_root, timeout_seconds=timeout)
    return JSONResponse(result.to_dict())


@control_app.get("/control/orchestrator/log_tail")
async def control_log_tail(
    repo_root: str = Query(...),
    n: int = Query(200, ge=1, le=10000),
) -> JSONResponse:
    """Get the last N lines of the orchestrator log.

    Query params:
        repo_root: str - Repository root path
        n: int - Number of lines (default: 200, max: 10000)
    """
    from ..infra.repo_identity import state_dir

    path = _validate_repo_root(repo_root)
    if path is None:
        return JSONResponse(
            {"error": "Invalid or missing repo_root"},
            status_code=400,
        )

    log_path = state_dir(path) / "logs" / "orchestrator.log"
    if not log_path.exists():
        return JSONResponse({"lines": [], "total_lines": 0})

    try:
        # Read last N lines efficiently
        with open(log_path, "rb") as f:
            # Seek to end
            f.seek(0, 2)
            file_size = f.tell()

            # Read in chunks from the end
            lines = []
            chunk_size = 8192
            remaining = file_size

            while len(lines) < n + 1 and remaining > 0:
                read_size = min(chunk_size, remaining)
                remaining -= read_size
                f.seek(remaining)
                chunk = f.read(read_size).decode("utf-8", errors="replace")
                chunk_lines = chunk.split("\n")

                if lines:
                    # Merge with previous chunk
                    lines[0] = chunk_lines[-1] + lines[0]
                    chunk_lines = chunk_lines[:-1]

                lines = chunk_lines + lines

            # Trim to N lines
            lines = lines[-n:] if len(lines) > n else lines

            # Count total lines (approximate)
            f.seek(0)
            total_lines = sum(1 for _ in f)

        return JSONResponse({
            "lines": lines,
            "total_lines": total_lines,
            "returned_lines": len(lines),
        })
    except OSError as e:
        return JSONResponse({
            "error": "read_failed",
            "detail": str(e),
        }, status_code=500)


# ======================================================================# Multi-Repo Registry API Endpoints
# ======================================================================# These endpoints manage the repo registry for multi-repo supervision.

LOCK_HEARTBEAT_UNRESPONSIVE_SECONDS = 45


def _heartbeat_age_seconds(iso_timestamp: str | None) -> int | None:
    if not iso_timestamp:
        return None
    try:
        parsed = datetime.fromisoformat(iso_timestamp)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return max(0, int((datetime.now(timezone.utc) - parsed).total_seconds()))


def _enrich_runtime_health(
    repo_path: Path,
    status_payload: dict[str, Any] | None,
    *,
    orphaned: bool = False,
    instance_id: str | None = None,
) -> dict[str, Any] | None:
    if status_payload is None:
        return None

    from ..infra.repo_lock import read_lock

    lock_info = read_lock(repo_path, instance_id=instance_id)
    last_heartbeat_at = lock_info.last_heartbeat_at if lock_info is not None else None
    heartbeat_age = _heartbeat_age_seconds(last_heartbeat_at)
    status_payload["last_heartbeat_at"] = last_heartbeat_at
    status_payload["heartbeat_age_seconds"] = heartbeat_age

    if orphaned:
        status_payload["runtime_health"] = "orphaned"
        return status_payload

    state = status_payload.get("state")
    if state == "failed":
        status_payload["runtime_health"] = "stale_lock"
        return status_payload
    if state == "running" and heartbeat_age is not None and heartbeat_age > LOCK_HEARTBEAT_UNRESPONSIVE_SECONDS:
        status_payload["runtime_health"] = "unresponsive"
        status_payload["unresponsive"] = True
        return status_payload
    if state == "running":
        status_payload["runtime_health"] = "healthy"
        status_payload["unresponsive"] = False
        return status_payload
    status_payload["runtime_health"] = "not_running"
    return status_payload


def _build_repos_status() -> list[dict[str, Any]]:  # noqa: C901, PLR0912 - multi-repo status with state aggregation
    """Build status data for all registered repos.

    Shared by both the REST endpoint and SSE stream.
    """
    import httpx

    from ..infra.repo_registry import add_repo, list_repos
    from ..infra.config import list_configs, get_config_path, Config

    sv = get_supervisor()
    preferred_root = _preferred_repo_root()
    preferred_repo = str(preferred_root) if preferred_root else None

    repos = list_repos()
    if preferred_repo and all(repo.path != preferred_repo for repo in repos):
        try:
            add_repo(preferred_repo)
            repos = list_repos()
        except ValueError:
            # Another process/request may have registered it first.
            repos = list_repos()

    if preferred_repo:
        repos = sorted(
            repos,
            key=lambda repo: 0 if repo.path == preferred_repo else 1,
        )

    cwd = Path.cwd().resolve()
    result = []

    for repo in repos:
        # Get status for each repo
        path = Path(repo.path)
        path_resolved = path.resolve() if path.exists() else path

        # Check if multi-instance mode is configured
        expected_instances = 1
        if path.exists() and repo.selected_config:
            try:
                config_path = get_config_path(path, repo.selected_config)
                if config_path.exists():
                    config = Config.load(config_path)
                    expected_instances = config.instances
            except Exception:
                pass  # Fall back to single instance

        # Get available configs
        available_configs = list_configs(path) if path.exists() else []

        # Build base repo data
        repo_data: dict[str, Any] = {
            "path": repo.path,
            "name": repo.name,
            "added_at": repo.added_at,
            "exists": path.exists(),
            "is_current_dir": (path_resolved == cwd),
            "configs": available_configs,
            "selected_config": repo.selected_config,
            "expected_instances": expected_instances,
        }

        if expected_instances > 1 and path.exists():
            # Multi-instance mode: get status for all instances
            multi_status = sv.status_all_instances(path)
            repo_data["instances"] = []

            for inst_status in multi_status.instances:
                inst_data = inst_status.to_dict()

                # Fetch internal state from each running instance
                if inst_status.state == "running" and inst_status.port:
                    try:
                        resp = httpx.get(
                            f"http://127.0.0.1:{inst_status.port}/api/status",
                            timeout=2.0,
                        )
                        if resp.status_code == 200:
                            internal = resp.json()
                            inst_data["paused"] = internal.get("paused", False)
                            inst_data["shutdown_requested"] = internal.get("shutdown_requested", False)
                            active_sessions = internal.get("active_sessions", [])
                            inst_data["active_session_count"] = len(active_sessions)
                            inst_data["e2e_role"] = internal.get("e2e_role")
                    except Exception:
                        pass

                enriched_instance = _enrich_runtime_health(path, inst_data, instance_id=inst_data.get("instance_id"))
                repo_data["instances"].append(enriched_instance or inst_data)

            # Compute aggregate status for the repo
            running_count = sum(1 for i in multi_status.instances if i.state == "running")
            if running_count == expected_instances:
                repo_data["status"] = {"state": "running", "running_count": running_count}
            elif running_count > 0:
                repo_data["status"] = {"state": "partial", "running_count": running_count}
            else:
                repo_data["status"] = {"state": "stopped", "running_count": 0}

        else:
            # Single instance mode (existing behavior)
            status_info = sv.status(path) if path.exists() else None
            repo_data["status"] = _enrich_runtime_health(path, status_info.to_dict() if status_info else None)

            if status_info and status_info.state != "running" and path.exists():
                detected = _detect_orchestrator_by_port(path, repo.selected_config)
                if detected:
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
                    repo_data["status"] = _enrich_runtime_health(path, orphaned_status, orphaned=True)

            # If running, fetch internal state from the orchestrator
            if status_info and status_info.state == "running" and status_info.port:
                try:
                    resp = httpx.get(
                        f"http://127.0.0.1:{status_info.port}/api/status",
                        timeout=2.0,
                    )
                    if resp.status_code == 200 and repo_data["status"]:
                        internal = resp.json()
                        repo_data["status"]["paused"] = internal.get("paused", False)
                        repo_data["status"]["shutdown_requested"] = internal.get("shutdown_requested", False)
                        # Include active session count for shutdown state determination
                        active_sessions = internal.get("active_sessions", [])
                        repo_data["status"]["active_session_count"] = len(active_sessions)
                        repo_data["status"]["e2e_role"] = internal.get("e2e_role")
                except Exception:
                    pass  # Keep supervisor status only

        # Include cached health status if available
        if repo.health:
            repo_data["health"] = repo.health.to_dict()
        else:
            repo_data["health"] = None

        result.append(repo_data)

    return result


@control_app.get("/control/repos")
async def list_repos_endpoint() -> JSONResponse:
    """List all registered repositories with their status.

    Returns a list of registered repos with their current orchestrator status
    and cached health information.
    """
    return JSONResponse({"repos": _build_repos_status()})


@control_app.get("/control/info")
async def control_info() -> JSONResponse:
    """Get control center build info."""
    repo_root = Path.cwd()
    identity = _build_repo_identity(repo_root)
    preferred_root = _preferred_repo_root()
    expected_identity_raw = os.environ.get(_EXPECTED_IDENTITY_ENV, "").strip()
    expected_identity = None
    if expected_identity_raw:
        try:
            expected_identity = deserialize_repo_identity(expected_identity_raw).to_dict()
        except Exception:
            expected_identity = None
    return JSONResponse({
        "repo_root": str(repo_root),
        "preferred_repo_root": str(preferred_root) if preferred_root else None,
        "commit_sha": identity.commit_sha,
        "commit_short": identity.commit_sha[:7] if identity.commit_sha else None,
        "repo_identity": identity.to_dict(),
        "expected_engine_identity": expected_identity,
    })


@control_app.get("/control/events")
async def control_events(request: Request):
    """Server-Sent Events endpoint for Control Center status updates.

    Pushes repository status updates every 10 seconds, eliminating the need
    for client-side polling.

    Events:
        - status: Full status snapshot of all repos
        - heartbeat: Keep-alive ping (every 30s between status updates)
    """
    logger.info("[Control SSE] Client connected")

    async def event_generator():
        try:
            while True:
                if await request.is_disconnected():
                    logger.info("[Control SSE] Client disconnected")
                    break

                # Build and send status snapshot
                repos = _build_repos_status()
                yield {
                    "event": "status",
                    "data": json.dumps({"repos": repos}),
                }

                # Wait 3 seconds before next update (faster for responsive shutdown feedback)
                await asyncio.sleep(3)
        except asyncio.CancelledError:
            logger.info("[Control SSE] Stream cancelled")
            raise

    return EventSourceResponse(event_generator())


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


@control_app.post("/control/repos")
async def add_repo_endpoint(request: Request) -> JSONResponse:
    """Add a repository to the registry.

    JSON body:
        repo_root: str - Repository root path
        name: str (optional) - Display name for the repo
    """
    from ..infra.repo_registry import add_repo, RegisteredRepo

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = _validate_repo_root(body.get("repo_root"))
    if repo_root is None:
        return JSONResponse(
            {"error": "Invalid or missing repo_root"},
            status_code=400,
        )

    try:
        repo = add_repo(repo_root)

        # If a custom name was provided, update it
        if "name" in body and body["name"]:
            from ..infra.repo_registry import load_registry, save_registry

            registry = load_registry()
            for r in registry.repos:
                if r.path == str(repo_root):
                    r.name = body["name"]
                    break
            save_registry(registry)
            repo = RegisteredRepo(
                path=str(repo_root),
                name=body["name"],
                added_at=repo.added_at,
            )

        return JSONResponse({
            "status": "added",
            "repo": repo.to_dict(),
        })
    except ValueError as e:
        return JSONResponse({
            "error": "already_registered",
            "detail": str(e),
        }, status_code=409)


@control_app.delete("/control/repos")
async def remove_repo_endpoint(request: Request) -> JSONResponse:
    """Remove a repository from the registry.

    JSON body:
        repo_root: str - Repository root path
        stop_orchestrator: bool (optional) - Stop running orchestrator first (default: true)
    """
    from ..infra.repo_registry import remove_repo

    sv = get_supervisor()

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = body.get("repo_root")
    if not repo_root:
        return JSONResponse(
            {"error": "Missing repo_root"},
            status_code=400,
        )

    # Normalize the path (but don't require it to exist for removal)
    try:
        normalized = str(Path(repo_root).resolve())
    except (ValueError, OSError):
        return JSONResponse(
            {"error": "Invalid repo_root path"},
            status_code=400,
        )

    # Optionally stop running orchestrator first
    stop_orchestrator = body.get("stop_orchestrator", True)
    if stop_orchestrator:
        path = Path(normalized)
        if path.exists():
            sv.stop(path)

    removed = remove_repo(normalized)
    if removed:
        return JSONResponse({
            "status": "removed",
            "repo_root": normalized,
        })
    else:
        return JSONResponse({
            "error": "not_found",
            "repo_root": normalized,
        }, status_code=404)


@control_app.post("/control/repos/select-config")
async def select_config_endpoint(request: Request) -> JSONResponse:
    """Set the selected config for a repository.

    Called when the user changes the config dropdown. Persists the selection
    so it survives page re-renders from SSE updates.

    JSON body:
        repo_root: str - Repository root path
        config_name: str - Config file name to select
    """
    from ..infra.repo_registry import set_selected_config

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = _validate_repo_root(body.get("repo_root"))
    if repo_root is None:
        return JSONResponse(
            {"error": "Invalid or missing repo_root"},
            status_code=400,
        )

    config_name = body.get("config_name")
    if not config_name:
        return JSONResponse(
            {"error": "Missing config_name"},
            status_code=400,
        )

    if not config_name.endswith(".yaml"):
        config_name += ".yaml"

    if set_selected_config(repo_root, config_name):
        return JSONResponse({"status": "ok", "config_name": config_name})
    else:
        return JSONResponse({"error": "Repo not found"}, status_code=404)


@control_app.post("/control/repos/validate")
async def validate_repo_config(request: Request) -> JSONResponse:
    """Validate a repository's configuration without starting the orchestrator.

    JSON body:
        repo_root: str - Repository root path
        config_name: str (optional) - Config file name (default: default.yaml)

    Returns validation results with errors and warnings.
    """
    from ..infra.config import Config, get_config_path, list_configs, CONFIG_DIR

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = _validate_repo_root(body.get("repo_root"))
    if repo_root is None:
        return JSONResponse(
            {"error": "Invalid or missing repo_root"},
            status_code=400,
        )

    config_name = body.get("config_name", "default.yaml")
    if not config_name.endswith(".yaml"):
        config_name += ".yaml"

    # Check if any configs exist
    available = list_configs(repo_root)
    if not available:
        return JSONResponse({
            "valid": False,
            "has_config": False,
            "config_path": None,
            "errors": [f"No configs found in {CONFIG_DIR}/"],
            "warnings": [],
        })

    # Get the specific config path
    config_path = get_config_path(repo_root, config_name)
    if not config_path.exists():
        return JSONResponse({
            "valid": False,
            "has_config": True,
            "config_path": None,
            "available_configs": available,
            "errors": [f"Config '{config_name}' not found. Available: {', '.join(available)}"],
            "warnings": [],
        })

    # Try to load and validate
    try:
        config = Config.load(config_path)
        errors = config.validate()

        warnings = []
        # Add helpful warnings
        if not config.code_review_agent:
            warnings.append("No code review agent configured - PRs won't be auto-reviewed")
        if not config.triage_review_agent:
            warnings.append("No triage review agent configured - no batch reviews")

        return JSONResponse({
            "valid": len(errors) == 0,
            "has_config": True,
            "config_path": str(config_path),
            "errors": errors,
            "warnings": warnings,
            "config_summary": {
                "repo": config.repo,
                "agents": list(config.agents.keys()),
                "ui_mode": config.ui_mode,
                "review_enabled": config.review_enabled,
            },
        })
    except Exception as e:
        return JSONResponse({
            "valid": False,
            "has_config": True,
            "config_path": str(config_path),
            "errors": [f"Failed to load config: {e}"],
            "warnings": [],
        })


@control_app.post("/control/repos/doctor")
async def doctor_repo(request: Request) -> JSONResponse:
    """Run full doctor checks for a repository including guardrails.

    This runs the same checks as `issue-orchestrator doctor` and updates
    the repo's health status in the registry.

    JSON body:
        repo_root: str - Repository root path
        config_name: str (optional) - Config file name to check

    Returns health status with errors and warnings.
    """
    from ..infra.repo_registry import update_repo_health

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = _validate_repo_root(body.get("repo_root"))
    if repo_root is None:
        return JSONResponse(
            {"error": "Invalid or missing repo_root"},
            status_code=400,
        )

    config_name = body.get("config_name")
    if config_name and not config_name.endswith(".yaml"):
        config_name += ".yaml"

    try:
        # Run doctor and update registry
        health = update_repo_health(repo_root, config_name=config_name)
        return JSONResponse({
            "status": health.status,
            "checked_at": health.checked_at,
            "errors": health.errors,
            "warnings": health.warnings,
            "can_start": health.status == "valid",
        })
    except Exception as e:
        logger.exception("Doctor check failed for %s", repo_root)
        return JSONResponse({
            "status": "error",
            "checked_at": "",
            "errors": [f"Doctor check failed: {e}"],
            "warnings": [],
            "can_start": False,
        }, status_code=500)


@control_app.get("/control/repos/config")
async def get_repo_config(
    repo_root: str = Query(..., description="Repository root path"),
    config_name: str = Query(default="default.yaml", description="Config file name"),
) -> JSONResponse:
    """Get the contents of a config file.

    Query params:
        repo_root: str - Repository root path
        config_name: str - Config file name (default: default.yaml)

    Returns the config file contents as YAML text.
    """
    from ..infra.config import get_config_path

    path = _validate_repo_root(repo_root)
    if path is None:
        return JSONResponse(
            {"error": "Invalid or missing repo_root"},
            status_code=400,
        )

    if not config_name.endswith(".yaml"):
        config_name += ".yaml"

    config_path = get_config_path(path, config_name)
    if not config_path.exists():
        return JSONResponse({
            "error": "config_not_found",
            "config_name": config_name,
        }, status_code=404)

    try:
        content = config_path.read_text()
        return JSONResponse({
            "config_name": config_name,
            "config_path": str(config_path),
            "content": content,
        })
    except Exception as e:
        return JSONResponse({
            "error": "read_failed",
            "detail": str(e),
        }, status_code=500)


@control_app.get("/control/repos/discover")
async def discover_repos_endpoint(  # noqa: C901 - recursive directory scanning with filtering
    search_paths: str = Query(
        default="",
        description="Comma-separated paths to search (default: ~/dev, ~/projects, ~/code, ~/repos)",
    ),
    max_depth: int = Query(default=3, description="Max directory depth to search"),
) -> JSONResponse:
    """Discover git repositories that could be configured with the orchestrator.

    Scans common development directories for git repos and categorizes them:
    - "ready": Has .issue-orchestrator/config/*.yaml (can be added directly)
    - "legacy": Has .issue-orchestrator.yaml only (needs migration)
    - "needs_setup": Git repo without any config (needs setup wizard)

    Returns repos not yet registered in the registry.
    """
    import os
    from ..infra.repo_registry import load_registry
    from ..infra.config import list_configs

    # Default search paths
    if search_paths:
        paths_to_search = [Path(p.strip()).expanduser() for p in search_paths.split(",")]
    else:
        home = Path.home()
        cwd = Path.cwd()
        paths_to_search = [
            home / "dev",
            home / "projects",
            home / "code",
            home / "repos",
            home / "src",
            home / "work",
            home / "github",
            cwd,
            cwd.parent,
        ]

    # Keep order stable but avoid redundant scans.
    seen_paths: set[str] = set()
    deduped_paths: list[Path] = []
    for path in paths_to_search:
        key = str(path.expanduser())
        if key in seen_paths:
            continue
        seen_paths.add(key)
        deduped_paths.append(path)
    paths_to_search = deduped_paths

    # Get already registered repos
    registry = load_registry()
    registered_paths = {r.path for r in registry.repos}

    discovered = []

    def scan_directory(base: Path, depth: int) -> None:  # noqa: C901 - recursive scan with ignore patterns
        if depth > max_depth:
            return
        if not base.exists() or not base.is_dir():
            return

        try:
            for entry in os.scandir(base):
                if entry.is_dir() and not entry.name.startswith("."):
                    entry_path = Path(entry.path)
                    git_path = entry_path / ".git"

                    # Check if this is a git repository
                    if git_path.exists():
                        # Skip worktrees (.git is a file pointing elsewhere)
                        if git_path.is_file():
                            continue

                        resolved = str(entry_path.resolve())
                        if resolved in registered_paths:
                            continue

                        # Determine config status
                        configs = list_configs(entry_path)
                        legacy_config = (entry_path / ".issue-orchestrator.yaml").exists()

                        if configs:
                            status = "ready"
                        elif legacy_config:
                            status = "legacy"
                        else:
                            status = "needs_setup"

                        discovered.append({
                            "path": resolved,
                            "name": entry_path.name,
                            "configs": configs,
                            "status": status,
                        })
                    else:
                        # Not a git repo, recurse deeper
                        scan_directory(entry_path, depth + 1)
        except PermissionError:
            pass

    for search_path in paths_to_search:
        scan_directory(search_path, 0)

    # Sort by name
    discovered.sort(key=lambda x: x["name"].lower())

    return JSONResponse({"discovered": discovered})


# ======================================================================# Setup Wizard API Endpoints
# ======================================================================# These endpoints support the GUI setup wizard for configuring new repositories.


def _load_config_for_repo(repo_root: str | None) -> Optional["Config"]:
    from ..infra.config import Config, get_config_path, list_configs, DEFAULT_CONFIG_NAME

    if not repo_root:
        return None
    path = _validate_repo_root(repo_root)
    if path is None:
        return None
    available = list_configs(path)
    if not available:
        return None
    config_name = DEFAULT_CONFIG_NAME if DEFAULT_CONFIG_NAME in available else available[0]
    config_path = get_config_path(path, config_name)
    try:
        return Config.load(config_path)
    except Exception:
        return None


def _build_agent_checks(config: Optional["Config"]) -> list[dict[str, Any]]:
    import shutil
    import subprocess

    if not config:
        return [{
            "name": "Agent CLI",
            "ok": True,
            "detail": "Config not detected yet",
        }]

    checks: list[dict[str, Any]] = []
    seen_executables: set[str] = set()
    for label, agent_config in config.agents.items():
        command = getattr(agent_config, "command", None) or ""
        executable = command.strip().split()[0] if command.strip() else ""
        if not executable:
            checks.append({
                "name": f"{label} CLI",
                "ok": False,
                "detail": "No command configured",
            })
            continue
        exec_name = executable.rsplit("/", 1)[-1]
        if exec_name in seen_executables:
            continue
        seen_executables.add(exec_name)
        path = shutil.which(exec_name)
        if not path:
            checks.append({
                "name": f"{exec_name} CLI",
                "ok": False,
                "detail": "Not found on PATH",
            })
            continue
        detail = path
        try:
            result = subprocess.run(
                [exec_name, "--version"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.returncode == 0 and result.stdout.strip():
                detail = result.stdout.strip()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        checks.append({
            "name": f"{exec_name} CLI",
            "ok": True,
            "detail": detail,
        })
    return checks


@control_app.get("/control/setup/prereqs")
async def setup_prereqs(repo_root: str | None = Query(default=None)) -> JSONResponse:
    """Check prerequisites for setting up a repository.

    Returns status of git, GitHub auth, and agent CLIs (based on config if available).
    """
    import shutil

    from ..execution.git_tools import run_git

    checks: dict[str, dict[str, Any]] = {}

    ok, output = run_git(["--version"], timeout_s=5)
    checks["git"] = {
        "ok": ok,
        "detail": output if ok else "Not found",
    }

    claude_path = shutil.which("claude")
    checks["claude"] = {
        "ok": bool(claude_path),
        "detail": claude_path or "Not found on PATH",
    }

    try:
        from ..execution.providers import resolve_github_token
        resolve_github_token(configured_token=None, configured_env=None)
        checks["github_auth"] = {"ok": True, "detail": "Token found"}
    except Exception as e:
        checks["github_auth"] = {"ok": False, "detail": str(e)}

    config = _load_config_for_repo(repo_root)
    agent_checks = _build_agent_checks(config)

    all_ok = all(c.get("ok", False) for c in checks.values()) and all(c.get("ok", False) for c in agent_checks)

    return JSONResponse({
        "all_ok": all_ok,
        "checks": checks,
        "agent_checks": agent_checks,
    })


@control_app.get("/control/setup/detect")
async def setup_detect(repo_root: str = Query(...)) -> JSONResponse:  # noqa: C901, PLR0912 - repo detection with multiple heuristics
    """Detect repository state for setup wizard.

    Query params:
        repo_root: str - Repository root path

    Returns detected repo info, existing config, GitHub labels, etc.
    """
    from ..execution.git_tools import run_git

    path = _validate_repo_root(repo_root)
    if path is None:
        return JSONResponse(
            {"error": "Invalid or missing repo_root"},
            status_code=400,
        )

    result: dict[str, Any] = {
        "repo_root": str(path),
        "repo": None,
        "existing_config": None,
        "config_path": None,
        "github_labels": [],
        "agent_labels": [],
        "prompt_candidates": [],
    }

    # Detect GitHub repo from git remote
    ok, url = run_git(["remote", "get-url", "origin"], cwd=path, timeout_s=10)
    if ok and "github.com" in url:
        if url.startswith("git@"):
            parts = url.split(":")[-1]
        else:
            parts = "/".join(url.split("/")[-2:])
        result["repo"] = parts.removesuffix(".git")

    # Find existing config
    from ..infra.config import find_config_file
    config_path = find_config_file(path)
    if config_path:
        result["config_path"] = str(config_path)
        try:
            import yaml
            with open(config_path) as f:
                result["existing_config"] = yaml.safe_load(f)
        except Exception:
            pass

    # Fetch GitHub labels if we have a repo
    if result["repo"]:
        try:
            from ..execution.providers import create_repository_host
            host = create_repository_host(repo=result["repo"])
            labels = host.list_labels()
            label_names: list[str] = [name for l in labels if isinstance(l, dict) and isinstance((name := l.get("name")), str)]
            result["github_labels"] = label_names
            result["agent_labels"] = [l for l in label_names if l.startswith("agent:")]
        except Exception:
            pass

    # Find prompt candidates
    prompt_patterns = [
        ".issue-orchestrator/**/*.md",
        "**/prompts/*.md",
        "**/*orchestrator*.md",
        "**/*-agent*.md",
    ]
    candidates = []
    for pattern in prompt_patterns:
        for p in path.glob(pattern):
            if p.is_file():
                rel = str(p.relative_to(path))
                if not any(part.startswith(".") or part == "node_modules" for part in p.parts):
                    if rel not in candidates:
                        candidates.append(rel)
    result["prompt_candidates"] = sorted(candidates)[:20]  # Limit to 20

    return JSONResponse(result)


@control_app.post("/control/setup/preview")
async def setup_preview(request: Request) -> JSONResponse:
    """Generate a config preview without saving.

    JSON body:
        config: dict - The configuration to preview

    Returns the generated YAML and list of files that would be created.
    """
    import yaml

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    config = body.get("config")
    if not config:
        return JSONResponse({"error": "Missing config"}, status_code=400)

    # Generate YAML
    class NoAliasDumper(yaml.SafeDumper):
        def ignore_aliases(self, data: Any) -> bool:
            return True

    yaml_content = yaml.dump(
        config,
        Dumper=NoAliasDumper,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )

    # List files that would be created
    files_to_create = []
    repo_root = body.get("repo_root", "")

    # Config file
    from ..infra.config import CONFIG_DIR, DEFAULT_CONFIG_NAME
    files_to_create.append({
        "path": f"{repo_root}/{CONFIG_DIR}/{DEFAULT_CONFIG_NAME}",
        "action": "create",
        "size": len(yaml_content),
    })

    # Prompt files for agents
    for agent_name, agent_config in config.get("agents", {}).items():
        prompt_path = agent_config.get("prompt", "")
        if prompt_path and not Path(repo_root).joinpath(prompt_path).exists() if repo_root else True:
            files_to_create.append({
                "path": f"{repo_root}/{prompt_path}" if repo_root else prompt_path,
                "action": "create",
                "type": "prompt",
                "agent": agent_name,
            })

    return JSONResponse({
        "yaml": yaml_content,
        "files": files_to_create,
    })


@control_app.post("/control/setup/save")
async def setup_save(request: Request) -> JSONResponse:  # noqa: C901, PLR0912 - config save with validation and file creation
    """Save the configuration and create necessary files.

    JSON body:
        repo_root: str - Repository root path
        config: dict - The configuration to save
        create_prompts: bool - Whether to create starter prompt files (default: true)
        create_labels: bool - Whether to create GitHub labels (default: true)

    Returns status and list of created files.
    """
    import yaml

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = _validate_repo_root(body.get("repo_root"))
    if repo_root is None:
        return JSONResponse(
            {"error": "Invalid or missing repo_root"},
            status_code=400,
        )

    config = body.get("config")
    if not config:
        return JSONResponse({"error": "Missing config"}, status_code=400)

    create_prompts = body.get("create_prompts", True)
    create_labels = body.get("create_labels", True)

    created_files = []
    created_labels = []

    # Get config name (default to default.yaml)
    config_name = body.get("config_name", "default.yaml")
    if not config_name.endswith(".yaml"):
        config_name += ".yaml"

    # Write config file to .issue-orchestrator/config/
    from ..infra.config import get_config_dir, get_config_path
    config_dir = get_config_dir(repo_root)
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = get_config_path(repo_root, config_name)

    class NoAliasDumper(yaml.SafeDumper):
        def ignore_aliases(self, data: Any) -> bool:
            return True

    yaml_content = yaml.dump(
        config,
        Dumper=NoAliasDumper,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
    )

    try:
        config_path.write_text(yaml_content)
        created_files.append(str(config_path))
    except Exception as e:
        return JSONResponse({
            "error": "Failed to write config",
            "detail": str(e),
        }, status_code=500)

    # Create prompt files
    if create_prompts:
        review_config = config.get("review", {})
        code_review_agent = review_config.get("default")
        code_review_label = review_config.get("code_review_label", "needs-code-review")
        code_reviewed_label = review_config.get("code_reviewed_label", "code-reviewed")
        triage_review_agent = review_config.get("triage_review_agent")
        triage_reviewed_label = review_config.get("triage_reviewed_label", "triage-reviewed")

        for agent_name, agent_config in config.get("agents", {}).items():
            prompt_rel = agent_config.get("prompt", "")
            if not prompt_rel:
                continue

            prompt_path = repo_root / prompt_rel
            if prompt_path.exists():
                continue  # Don't overwrite

            try:
                prompt_path.parent.mkdir(parents=True, exist_ok=True)

                # Determine prompt content based on agent type
                agent_short = agent_name.split(":")[-1]
                is_code_reviewer = agent_name == code_review_agent or agent_name.lower() == "agent:reviewer"
                is_triage_reviewer = agent_name == triage_review_agent or "triage" in agent_name.lower()

                if is_code_reviewer:
                    content = _create_code_review_prompt(code_review_label, code_reviewed_label)
                elif is_triage_reviewer:
                    content = _create_triage_review_prompt(code_reviewed_label, triage_reviewed_label)
                else:
                    content = _create_starter_prompt(agent_short)

                prompt_path.write_text(content)
                created_files.append(str(prompt_path))
            except Exception as e:
                logger.warning(f"Failed to create prompt {prompt_path}: {e}")

    # Create GitHub labels
    repo_config = config.get("repo") or {}
    repo_name = repo_config.get("name") if isinstance(repo_config, dict) else repo_config
    if create_labels and repo_name:
        try:
            from ..execution.providers import create_repository_host
            host = create_repository_host(repo=repo_name)

            # Get existing labels
            existing = {l.get("name") for l in host.list_labels() if isinstance(l, dict)}

            # Labels to create
            labels_config = config.get("labels", {})
            prefix = labels_config.get("prefix", "")

            def prefixed(label: str) -> str:
                return f"{prefix}:{label}" if prefix else label

            labels_to_create = []

            # Agent labels
            for agent_name in config.get("agents", {}).keys():
                if agent_name not in existing:
                    labels_to_create.append((agent_name, "1D76DB", f"Issues for {agent_name.split(':')[-1]} agent"))

            # Status labels
            status_labels = [
                (prefixed("in-progress"), "5319E7", "Agent is working on this"),
                (prefixed("blocked"), "B60205", "Agent is blocked"),
                (prefixed("needs-human"), "FBCA04", "Agent needs human input"),
            ]
            for name, color, desc in status_labels:
                if name not in existing:
                    labels_to_create.append((name, color, desc))

            # Review labels
            review_config = config.get("review", {})
            if review_config.get("enabled"):
                review_labels = [
                    (review_config.get("code_review_label", "needs-code-review"), "7057FF", "PR needs code review"),
                    (review_config.get("code_reviewed_label", "code-reviewed"), "0E8A16", "PR has been code reviewed"),
                ]
                for name, color, desc in review_labels:
                    if name not in existing:
                        labels_to_create.append((name, color, desc))

            if review_config.get("triage_review_agent"):
                triage_label = review_config.get("triage_reviewed_label", "triage-reviewed")
                if triage_label not in existing:
                    labels_to_create.append((triage_label, "1D76DB", "PR has been triage reviewed"))

            # Create labels
            for name, color, desc in labels_to_create:
                try:
                    host.create_label(name, color=color, description=desc, force=True)
                    created_labels.append(name)
                except Exception as e:
                    logger.warning(f"Failed to create label {name}: {e}")

        except Exception as e:
            logger.warning(f"Failed to create labels: {e}")

    return JSONResponse({
        "status": "saved",
        "config_path": str(config_path),
        "created_files": created_files,
        "created_labels": created_labels,
    })


def _create_starter_prompt(agent_short: str) -> str:
    """Create a starter prompt for a work agent."""
    return f"""# {agent_short.title()} Agent Prompt

You are working on issue #{{issue_number}}: {{issue_title}}

## Your Role
You are the {agent_short} agent responsible for implementing changes in this area.

## Working Directory
Your worktree is at: {{worktree}}

## Instructions
1. Read the issue carefully and understand the requirements
2. Implement the necessary changes
3. Write tests if applicable
4. Run existing tests to ensure nothing is broken
5. When complete, use `coding-done` to create a PR

## Important
- Always use `coding-done` when finished (not `git push` directly)
- If blocked, use `coding-done blocked --reason "reason" --attempted "what you tried"`
- If you need human input, use `coding-done needs_human --question "question"`
"""


def _create_code_review_prompt(code_review_label: str, code_reviewed_label: str) -> str:
    """Create a code review prompt."""
    return f"""# Code Review Agent

You are a code reviewer. Your job is to review PRs created by work agents.

## Your Task

You are reviewing PR #{{pr_number}} for issue #{{issue_number}}: {{issue_title}}

The PR has the `{code_review_label}` label and needs your review.

## Review Process

1. Fetch PR details: `gh pr view {{{{pr_number}}}} --json title,body,additions,deletions`
2. Review the diff: `gh pr diff {{{{pr_number}}}}`
3. Check code quality, tests, and correctness
4. Approve or request changes

## After Review

If approved:
```bash
gh pr review {{{{pr_number}}}} --approve --body "LGTM!"
gh pr edit {{{{pr_number}}}} --remove-label "{code_review_label}" --add-label "{code_reviewed_label}"
```

Then: `reviewer-done approved --summary "Reviewed PR #{{{{pr_number}}}}. Approved." --risk low`

If changes are needed:
```bash
reviewer-done changes_requested --issues "Describe what must be fixed" --risk medium
```
"""


def _create_triage_review_prompt(review_label: str, reviewed_label: str) -> str:
    """Create a triage review prompt."""
    return f"""# Triage Review Agent

You audit work done by AI agents across multiple PRs.

## Your Task

Find PRs with `{review_label}` label and audit them for patterns, issues, and quality.

## Process

1. List PRs: `gh pr list --label "{review_label}"`
2. For each PR, review code changes and test coverage
3. Comment with your findings
4. Update label: `gh pr edit <number> --remove-label "{review_label}" --add-label "{reviewed_label}"`

## Completion

`reviewer-done approved --summary "Audited N PRs. Summary of findings." --risk low`
"""


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


# ======================================================================# E2E Test Runner API
# ======================================================================# These endpoints manage async E2E test execution per repository.


@control_app.post("/control/e2e/start")
async def e2e_start(request: Request) -> JSONResponse:
    """Start an E2E test run for a repository.

    JSON body:
        repo_root: str - Repository root path
        pytest_args: list[str] (optional) - Override pytest arguments
        allow_retry_once: bool (optional) - Retry failed tests once (default: True)

    Returns:
        {status: "started", pid: int, log_path: str}
    """
    from ..infra.e2e_runner import get_e2e_runner_manager, E2EAlreadyRunning
    from ..infra.config import Config

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = _validate_repo_root(body.get("repo_root"))
    if repo_root is None:
        return JSONResponse(
            {"error": "Invalid or missing repo_root"},
            status_code=400,
        )

    # Load config to get e2e settings and orchestrator_id
    try:
        config = Config.find_and_load(repo_root)
    except FileNotFoundError:
        return JSONResponse(
            {"error": "Config not found", "detail": "No .issue-orchestrator/config found"},
            status_code=404,
        )

    if not config.e2e.enabled:
        return JSONResponse(
            {"error": "e2e_disabled", "detail": "E2E runner not enabled in config"},
            status_code=400,
        )

    # Use config values or overrides from request
    pytest_args = body.get("pytest_args") or config.e2e.pytest_args
    allow_retry = body.get("allow_retry_once", config.e2e.allow_retry_once)
    orchestrator_id = config.orchestrator_id

    runner = get_e2e_runner_manager()

    try:
        instance_id = _orchestrator.deps.services.instance_id if _orchestrator else ""
        result = runner.start(
            repo_root=repo_root,
            orchestrator_id=orchestrator_id,
            pytest_args=pytest_args,
            allow_retry_once=allow_retry,
            quarantine_file=config.e2e.quarantine_file,
            auto_quarantine=config.e2e.auto_quarantine,
            orchestrator_instance_id=instance_id,
        )

        # Broadcast E2E started event for SSE subscribers
        try:
            from .web import broadcast_event
            await broadcast_event("e2e.started", {
                "pid": result["pid"],
                "orchestrator_id": orchestrator_id,
            })
        except Exception as e:
            logger.debug("Could not broadcast e2e.started event: %s", e)

        return JSONResponse({
            "status": "started",
            "pid": result["pid"],
            "log_path": result["log_path"],
        })
    except E2EAlreadyRunning as e:
        return JSONResponse(
            {"error": "already_running", "pid": e.pid},
            status_code=409,
        )
    except Exception as e:
        logger.exception("Failed to start E2E: %s", e)
        return JSONResponse(
            {"error": "start_failed", "detail": str(e)},
            status_code=500,
        )


@control_app.post("/control/e2e/stop")
async def e2e_stop(request: Request) -> JSONResponse:
    """Stop a running E2E test.

    JSON body:
        repo_root: str - Repository root path
    """
    from ..infra.e2e_runner import get_e2e_runner_manager
    from ..infra.config import Config

    try:
        body = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    repo_root = _validate_repo_root(body.get("repo_root"))
    if repo_root is None:
        return JSONResponse(
            {"error": "Invalid or missing repo_root"},
            status_code=400,
        )

    # Get orchestrator_id from config - fail fast if not configured
    try:
        config = Config.find_and_load(repo_root)
    except FileNotFoundError:
        return JSONResponse(
            {"error": "config_not_found", "detail": "No orchestrator config found"},
            status_code=400,
        )

    runner = get_e2e_runner_manager()
    stopped = runner.stop(config.orchestrator_id, repo_root)

    # Broadcast E2E stopped event for SSE subscribers
    if stopped:
        try:
            from .web import broadcast_event
            await broadcast_event("e2e.stopped", {
                "orchestrator_id": config.orchestrator_id,
            })
        except Exception as e:
            logger.debug("Could not broadcast e2e.stopped event: %s", e)

    return JSONResponse({
        "status": "stopped" if stopped else "not_running",
    })


@control_app.get("/control/e2e/status")
async def e2e_status(repo_root: str = Query(...)) -> JSONResponse:
    """Get E2E test runner status.

    Query params:
        repo_root: str - Repository root path

    Returns:
        {running: bool, pid: int | null, last_run: {...} | null, signal_score: {...}}
    """
    from ..infra.e2e_runner import get_e2e_runner_manager, get_next_run_info
    from ..infra.e2e_db import E2EDB
    from ..infra.config import Config

    validated_root = _validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse(
            {"error": "Invalid repo_root"},
            status_code=400,
        )

    # Get orchestrator_id from config - fail fast if not configured
    try:
        config = Config.find_and_load(validated_root)
    except FileNotFoundError:
        return JSONResponse(
            {"error": "config_not_found", "detail": "No orchestrator config found"},
            status_code=400,
        )

    # Get process status
    runner = get_e2e_runner_manager()
    proc_status = runner.status(config.orchestrator_id)

    # Get DB status
    db_path = validated_root / ".issue-orchestrator" / "e2e.db"
    last_run = None
    next_run = None
    run_obj = None
    signal_score = None
    progress = None
    needs_attention = False
    untriaged_count = 0

    if db_path.exists():
        try:
            db = E2EDB(db_path)
            run_obj = db.latest_run(config.orchestrator_id)
            if run_obj:
                last_run = run_obj.to_dict()
                # Get progress for running tests
                if run_obj.status == "running":
                    progress = db.get_progress(run_obj.id)
                # Determine if attention is needed: failed run with untriaged failures
                elif run_obj.status == "failed":
                    untriaged_count = _count_untriaged_failures(db, run_obj.id)
                    needs_attention = untriaged_count > 0
                _auto_create_e2e_issues_if_needed(config, db, run_obj, proc_status)
            signal_score = db.compute_signal_score(config.orchestrator_id)
        except Exception as e:
            logger.warning("Failed to read E2E DB: %s", e)

    if config.e2e.enabled:
        next_run = get_next_run_info(config, validated_root, run_obj)

    return JSONResponse({
        "enabled": config.e2e.enabled,
        "running": proc_status["running"],
        "pid": proc_status["pid"],
        "exit_code": proc_status["exit_code"],
        "last_run": last_run,
        "signal_score": signal_score,
        "progress": progress,
        "next_run": next_run,
        "needs_attention": needs_attention,
        "untriaged_count": untriaged_count,
    })


@control_app.get("/control/e2e/runs")
async def e2e_runs(
    repo_root: str = Query(...),
    limit: int = Query(20, ge=1, le=100),
) -> JSONResponse:
    """List recent E2E runs.

    Query params:
        repo_root: str - Repository root path
        limit: int - Max runs to return (default: 20)
    """
    from ..infra.e2e_db import E2EDB
    from ..infra.config import Config

    validated_root = _validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse(
            {"error": "Invalid repo_root"},
            status_code=400,
        )

    # Get orchestrator_id from config - fail fast if not configured
    try:
        config = Config.find_and_load(validated_root)
    except FileNotFoundError:
        return JSONResponse(
            {"error": "config_not_found", "detail": "No orchestrator config found"},
            status_code=400,
        )

    db_path = validated_root / ".issue-orchestrator" / "e2e.db"
    if not db_path.exists():
        return JSONResponse({"runs": []})

    try:
        db = E2EDB(db_path)
        runs = db.list_runs(config.orchestrator_id, limit=limit)
        return JSONResponse({
            "runs": [r.to_dict() for r in runs],
        })
    except Exception as e:
        logger.exception("Failed to list E2E runs: %s", e)
        return JSONResponse(
            {"error": "db_error", "detail": str(e)},
            status_code=500,
        )


@control_app.get("/control/e2e/run/{run_id}")
async def e2e_run_details(
    run_id: int,
    repo_root: str = Query(...),
    enhanced: bool = Query(False, description="Use enhanced response with categories and history"),
) -> JSONResponse:
    """Get details of a specific E2E run.

    Path params:
        run_id: int - Run ID

    Query params:
        repo_root: str - Repository root path
        enhanced: bool - If true, returns tests grouped by category with history (default: false for backward compat)

    Enhanced response includes:
        - run: Run metadata
        - tests_by_category: Tests grouped by state (untriaged, has_issue, flaky, fixed, passed)
        - summary: Counts for each category

    Legacy response (enhanced=false) includes:
        - run: Run metadata
        - results: Flat list of test results
    """
    from ..infra.e2e_db import E2EDB

    validated_root = _validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse(
            {"error": "Invalid repo_root"},
            status_code=400,
        )

    db_path = validated_root / ".issue-orchestrator" / "e2e.db"
    if not db_path.exists():
        return JSONResponse(
            {"error": "not_found", "detail": "E2E database not found"},
            status_code=404,
        )

    try:
        db = E2EDB(db_path)
        if enhanced:
            # Get E2E config for flake threshold
            e2e_config = _get_e2e_config(validated_root)
            details = db.run_details_enhanced(
                run_id,
                history_limit=5,
                flake_threshold_percent=float(e2e_config.flake_threshold),
            )
        else:
            details = db.run_details(run_id)

        if details is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"Run {run_id} not found"},
                status_code=404,
            )
        return JSONResponse(details)
    except Exception as e:
        logger.exception("Failed to get E2E run details: %s", e)
        return JSONResponse(
            {"error": "db_error", "detail": str(e)},
            status_code=500,
        )


def _read_orchestrator_timeline_for_window(
    timeline_db_path: Path,
    started_at: str,
    finished_at: str | None,
    orchestrator_instance_id: str = "",
) -> list[dict]:
    """Read orchestrator timeline events scoped to an E2E run.

    Opens timeline.sqlite read-only.  When ``orchestrator_instance_id`` is
    provided, filters directly by that value — no guessing required.

    Falls back to timestamp-only filtering when instance_id is not
    available (older runs or pre-v4 timeline databases).
    """
    import json as _json
    import sqlite3 as _sqlite3

    try:
        uri = f"file:{timeline_db_path}?mode=ro"
        conn = _sqlite3.connect(uri, uri=True)
        conn.row_factory = _sqlite3.Row

        end_ts = finished_at or "9999-12-31T23:59:59Z"

        # Check if instance_id column exists (schema v4+)
        columns = {row[1] for row in conn.execute("PRAGMA table_info(timeline_events)")}
        has_instance_id = "instance_id" in columns

        if has_instance_id and orchestrator_instance_id:
            # Best path: filter by the exact instance_id stored in e2e_runs
            rows = conn.execute(
                """
                SELECT event_id, source_event, timestamp, event, data_json
                FROM timeline_events
                WHERE instance_id = ?
                  AND timestamp >= ? AND timestamp <= ?
                ORDER BY sequence ASC
                """,
                (orchestrator_instance_id, started_at, end_ts),
            ).fetchall()
        else:
            # Fallback for older runs without stored instance_id
            # or pre-v4 timeline schemas: timestamp-only filtering
            rows = conn.execute(
                """
                SELECT event_id, source_event, timestamp, event, data_json
                FROM timeline_events
                WHERE timestamp >= ? AND timestamp <= ?
                ORDER BY sequence ASC
                """,
                (started_at, end_ts),
            ).fetchall()

        conn.close()
    except Exception:
        logger.debug("Could not read orchestrator timeline from %s", timeline_db_path, exc_info=True)
        return []

    from ..ports.timeline_store import TimelineRecord
    from ..timeline import TimelineStream as _TimelineStream

    records = []
    for row in rows:
        data_json = row["data_json"] or "{}"
        try:
            data = _json.loads(data_json)
        except (ValueError, TypeError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        records.append(
            TimelineRecord(
                event_id=str(row["event_id"]),
                timestamp=str(row["timestamp"]),
                event=str(row["event"]),
                data=data,
                source_event=str(row["source_event"] or ""),
            )
        )

    if not records:
        return []

    stream = _TimelineStream.from_records(issue_number=0, records=records)
    return [evt.to_dict() for evt in stream.events]


@control_app.get("/control/e2e/run/{run_id}/timeline")
async def e2e_run_timeline_endpoint(
    run_id: int,
    repo_root: str = Query(...),
) -> JSONResponse:
    """Get timeline events for a specific E2E run.

    Returns events in the same shape as the main issue timeline,
    enabling shared timeline rendering between E2E and issue views.

    Path params:
        run_id: int - Run ID

    Query params:
        repo_root: str - Repository root path
    """
    from ..infra.e2e_db import E2EDB, e2e_run_timeline

    validated_root = _validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse(
            {"error": "Invalid repo_root"},
            status_code=400,
        )

    db_path = validated_root / ".issue-orchestrator" / "e2e.db"
    if not db_path.exists():
        return JSONResponse(
            {"error": "not_found", "detail": "E2E database not found"},
            status_code=404,
        )

    try:
        db = E2EDB(db_path)
        events = db.get_run_events(run_id)

        # Read orchestrator events from timeline.sqlite, scoped by instance_id
        # (written per-event since schema v4) and the run's time window.
        orchestrator_events: list[dict] = []
        run = db.get_run(run_id)
        timeline_db_path = validated_root / ".issue-orchestrator" / "state" / "timeline.sqlite"
        if run and timeline_db_path.exists():
            orchestrator_events = _read_orchestrator_timeline_for_window(
                timeline_db_path,
                started_at=run.started_at,
                finished_at=run.finished_at,
                orchestrator_instance_id=run.orchestrator_instance_id,
            )

        return JSONResponse(e2e_run_timeline(events, orchestrator_events=orchestrator_events))
    except Exception as e:
        logger.exception("Failed to get E2E run timeline: %s", e)
        return JSONResponse(
            {"error": "db_error", "detail": str(e)},
            status_code=500,
        )


@control_app.get("/control/e2e/logs/{run_id}")
async def e2e_logs(
    run_id: int,
    repo_root: str = Query(...),
    tail: int = Query(500, ge=1, le=10000),
) -> JSONResponse:
    """Get logs for a specific E2E run.

    Path params:
        run_id: int - Run ID

    Query params:
        repo_root: str - Repository root path
        tail: int - Max lines to return (default: 500)
    """
    from ..infra.e2e_db import E2EDB

    validated_root = _validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse(
            {"error": "Invalid repo_root"},
            status_code=400,
        )

    db_path = validated_root / ".issue-orchestrator" / "e2e.db"
    if not db_path.exists():
        return JSONResponse(
            {"error": "not_found", "detail": "E2E database not found"},
            status_code=404,
        )

    try:
        db = E2EDB(db_path)
        details = db.run_details(run_id)
        if details is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"Run {run_id} not found"},
                status_code=404,
            )

        log_path = details["run"].get("log_path")
        if not log_path:
            return JSONResponse(
                {"error": "no_logs", "detail": "No log file for this run"},
                status_code=404,
            )

        log_file = Path(log_path)
        if not log_file.exists():
            return JSONResponse(
                {"error": "log_missing", "detail": f"Log file not found: {log_path}"},
                status_code=404,
            )

        # Read last N lines
        with open(log_file, "r") as f:
            lines = f.readlines()
            content = "".join(lines[-tail:])

        return JSONResponse({
            "log_path": str(log_path),
            "total_lines": len(lines),
            "returned_lines": min(tail, len(lines)),
            "content": content,
        })
    except Exception as e:
        logger.exception("Failed to get E2E logs: %s", e)
        return JSONResponse(
            {"error": "read_error", "detail": str(e)},
            status_code=500,
        )


@control_app.get("/control/e2e/failed/{run_id}")
async def e2e_failed_tests(
    run_id: int,
    repo_root: str = Query(...),
) -> JSONResponse:
    """Get failed tests from a specific run.

    Path params:
        run_id: int - Run ID

    Query params:
        repo_root: str - Repository root path
    """
    from ..infra.e2e_db import E2EDB

    validated_root = _validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse(
            {"error": "Invalid repo_root"},
            status_code=400,
        )

    db_path = validated_root / ".issue-orchestrator" / "e2e.db"
    if not db_path.exists():
        return JSONResponse(
            {"error": "not_found", "detail": "E2E database not found"},
            status_code=404,
        )

    try:
        db = E2EDB(db_path)
        failed = db.get_failed_tests(run_id)
        return JSONResponse({
            "failed_tests": [t.to_dict() for t in failed],
        })
    except Exception as e:
        logger.exception("Failed to get failed tests: %s", e)
        return JSONResponse(
            {"error": "db_error", "detail": str(e)},
            status_code=500,
        )


@control_app.get("/control/e2e/quarantine")
async def e2e_quarantine_list(repo_root: str = Query(...)) -> JSONResponse:
    """Get the quarantine list for a repository.

    Query params:
        repo_root: str - Repository root path

    Returns:
        {quarantine_file: str, tests: [str], count: int}
    """
    from ..infra.e2e_db import load_quarantine_list

    validated_root = _validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse(
            {"error": "Invalid repo_root"},
            status_code=400,
        )

    # Require orchestrator to be running for config access
    if _orchestrator is None:
        return JSONResponse({"error": "Orchestrator not initialized"}, status_code=503)

    quarantine_file = _orchestrator.config.e2e.quarantine_file
    quarantine_path = validated_root / quarantine_file
    tests = load_quarantine_list(quarantine_path)

    return JSONResponse({
        "quarantine_file": quarantine_file,
        "tests": sorted(tests),
        "count": len(tests),
        "exists": quarantine_path.exists(),
    })


def _apply_quarantine_changes(action: str, nodeids: list, current_tests: set) -> tuple[list, list]:
    """Apply add or remove actions to the quarantine set."""
    added, removed = [], []
    if action == "add":
        for nodeid in nodeids:
            if nodeid not in current_tests:
                current_tests.add(nodeid)
                added.append(nodeid)
    else:  # remove
        for nodeid in nodeids:
            if nodeid in current_tests:
                current_tests.remove(nodeid)
                removed.append(nodeid)
    return added, removed


@control_app.post("/control/e2e/quarantine")
async def e2e_quarantine_modify(
    request: Request,
    repo_root: str = Query(...),
) -> JSONResponse:
    """Add or remove tests from the quarantine list.

    Query params:
        repo_root: str - Repository root path

    JSON body:
        action: "add" | "remove"
        nodeids: list[str] - Test node IDs to add/remove

    Returns:
        {quarantine_file: str, tests: [str], count: int, added: [str], removed: [str]}
    """
    from ..infra.e2e_db import load_quarantine_list, save_quarantine_list

    validated_root = _validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse({"error": "Invalid repo_root"}, status_code=400)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    action = body.get("action", "").strip()
    nodeids = body.get("nodeids", [])

    if action not in ("add", "remove"):
        return JSONResponse({"error": "action must be 'add' or 'remove'"}, status_code=400)
    if not nodeids:
        return JSONResponse({"error": "nodeids is required"}, status_code=400)

    # Require orchestrator to be running for config access
    if _orchestrator is None:
        return JSONResponse({"error": "Orchestrator not initialized"}, status_code=503)

    quarantine_file = _orchestrator.config.e2e.quarantine_file
    quarantine_path = validated_root / quarantine_file
    current_tests = load_quarantine_list(quarantine_path)

    added, removed = _apply_quarantine_changes(action, nodeids, current_tests)
    save_quarantine_list(quarantine_path, current_tests)

    logger.info(
        "[quarantine] Modified quarantine list: added=%d, removed=%d",
        len(added), len(removed))

    return JSONResponse({
        "quarantine_file": quarantine_file,
        "tests": sorted(current_tests),
        "count": len(current_tests),
        "added": added,
        "removed": removed,
    })


@control_app.get("/control/e2e/stats")
async def e2e_stats(repo_root: str = Query(...)) -> JSONResponse:
    """Get E2E statistics for the stats modal.

    Query params:
        repo_root: str - Repository root path

    Returns:
        {
            pass_rate: float (0-1),
            pass_rate_percent: int (0-100),
            runs_analyzed: int,
            flaky_count: int,
            quarantine_count: int,
            next_check: str or null,
            next_check_reason: str or null,
            flake_window_runs: int
        }
    """
    from ..infra.e2e_runner import get_next_run_info
    from ..infra.e2e_db import E2EDB, load_quarantine_list
    from ..infra.config import Config

    validated_root = _validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse({"error": "Invalid repo_root"}, status_code=400)

    # Load config
    try:
        config = Config.find_and_load(validated_root)
    except FileNotFoundError:
        return JSONResponse(
            {"error": "config_not_found", "detail": "No orchestrator config found"},
            status_code=400,
        )

    e2e_config = config.e2e
    flake_window = e2e_config.flake_window_runs
    flake_threshold = float(e2e_config.flake_threshold)

    # Initialize defaults
    pass_rate = None
    pass_rate_percent = None
    runs_analyzed = 0
    flaky_count = 0
    quarantine_count = 0
    next_check = None
    next_check_reason = None

    # Get quarantine count
    quarantine_path = validated_root / e2e_config.quarantine_file
    quarantined = load_quarantine_list(quarantine_path)
    quarantine_count = len(quarantined)

    # Get DB stats
    db_path = validated_root / ".issue-orchestrator" / "e2e.db"
    if db_path.exists():
        db = E2EDB(db_path)

        # Get signal score (pass rate)
        signal_score = db.compute_signal_score(config.orchestrator_id)
        if signal_score:
            pass_rate = signal_score.get("pass_rate")
            if pass_rate is not None:
                pass_rate_percent = int(pass_rate * 100)
            runs_analyzed = signal_score.get("runs_analyzed", 0)

        # Get flaky test count
        all_stability = db.get_all_test_stability(
            window_runs=flake_window,
            flake_threshold_percent=flake_threshold,
        )
        flaky_count = sum(1 for s in all_stability if s.is_likely_flaky)

        # Get next run info
        run_obj = db.latest_run(config.orchestrator_id)
        if config.e2e.enabled:
            next_info = get_next_run_info(config, validated_root, run_obj)
            if next_info:
                next_check = next_info.get("scheduled_time")
                next_check_reason = next_info.get("reason")

    return JSONResponse({
        "pass_rate": pass_rate,
        "pass_rate_percent": pass_rate_percent,
        "runs_analyzed": runs_analyzed,
        "flaky_count": flaky_count,
        "quarantine_count": quarantine_count,
        "next_check": next_check,
        "next_check_reason": next_check_reason,
        "flake_window_runs": flake_window,
    })


@control_app.get("/control/e2e/flaky-tests")
async def e2e_flaky_tests(
    repo_root: str = Query(...),
    threshold: int = Query(default=20),
    window: int = Query(default=10),
) -> JSONResponse:
    """Get tests that exhibit flaky behavior via flip-rate analysis.

    Query params:
        repo_root: str - Repository root path
        threshold: int - Flip rate percentage (0-100) to flag as flaky (default: 20)
        window: int - Number of recent runs to check (default: 10)

    Returns:
        {flaky_tests: [{nodeid, flip_rate, flip_rate_percent, flip_count, ...}], threshold, window}
    """
    from ..infra.e2e_db import E2EDB, load_quarantine_list

    validated_root = _validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse({"error": "Invalid repo_root"}, status_code=400)

    db_path = validated_root / ".issue-orchestrator" / "e2e.db"
    if not db_path.exists():
        return JSONResponse({"error": "not_found", "detail": "E2E database not found"}, status_code=404)

    # Require orchestrator to be running for config access
    if _orchestrator is None:
        return JSONResponse({"error": "Orchestrator not initialized"}, status_code=503)

    quarantine_file = _orchestrator.config.e2e.quarantine_file
    quarantine_path = validated_root / quarantine_file
    quarantined = load_quarantine_list(quarantine_path)

    db = E2EDB(db_path)
    all_stability = db.get_all_test_stability(
        window_runs=window,
        flake_threshold_percent=float(threshold),
    )

    # Filter to only flaky tests
    flaky_tests = []
    for stability in all_stability:
        if stability.is_likely_flaky:
            entry = stability.to_dict()
            entry["is_quarantined"] = stability.nodeid in quarantined
            # Backward-compat alias
            entry["flake_count"] = stability.flip_count
            flaky_tests.append(entry)

    return JSONResponse({
        "flaky_tests": flaky_tests,
        "threshold": threshold,
        "window": window,
        "quarantine_file": quarantine_file,
    })


@control_app.get("/control/e2e/summary/{run_id}")
async def e2e_test_summary(
    run_id: int,
    repo_root: str = Query(...),
) -> JSONResponse:
    """Get comprehensive test summary for a run.

    Includes passed, failed, passed-on-retry, quarantined, and skipped tests.

    Path params:
        run_id: int - Run ID

    Query params:
        repo_root: str - Repository root path

    Returns:
        {
            passed: [...], failed: [...], passed_on_retry: [...],
            quarantined: [...], skipped: [...],
            counts: {total, passed, failed, passed_on_retry, quarantined, skipped}
        }
    """
    from ..infra.e2e_db import E2EDB

    validated_root = _validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse(
            {"error": "Invalid repo_root"},
            status_code=400,
        )

    db_path = validated_root / ".issue-orchestrator" / "e2e.db"
    if not db_path.exists():
        return JSONResponse(
            {"error": "not_found", "detail": "E2E database not found"},
            status_code=404,
        )

    try:
        db = E2EDB(db_path)
        summary = db.get_test_summary(run_id)
        return JSONResponse(summary)
    except Exception as e:
        logger.exception("Failed to get test summary: %s", e)
        return JSONResponse(
            {"error": "db_error", "detail": str(e)},
            status_code=500,
        )


@control_app.get("/control/e2e/diagnosis/{run_id}")
async def e2e_run_diagnosis(
    run_id: int,
    repo_root: str = Query(...),
) -> JSONResponse:
    """Get comprehensive diagnosis for an E2E run failure.

    Returns full diagnostic data including logs, stack traces, and suggestions.

    Path params:
        run_id: int - Run ID

    Query params:
        repo_root: str - Repository root path

    Returns:
        E2ERunDiagnosis as JSON with full log content and test details
    """
    from ..infra.e2e_db import E2EDB
    from ..infra.e2e_run_diagnosis import create_e2e_run_diagnosis

    validated_root = _validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse(
            {"error": "Invalid repo_root"},
            status_code=400,
        )

    db_path = validated_root / ".issue-orchestrator" / "e2e.db"
    if not db_path.exists():
        return JSONResponse(
            {"error": "not_found", "detail": "E2E database not found"},
            status_code=404,
        )

    try:
        db = E2EDB(db_path)
        diagnosis = create_e2e_run_diagnosis(run_id, db)
        if diagnosis is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"Run {run_id} not found"},
                status_code=404,
            )
        return JSONResponse(diagnosis.to_dict())
    except Exception as e:
        logger.exception("Failed to create E2E diagnosis: %s", e)
        return JSONResponse(
            {"error": "diagnosis_error", "detail": str(e)},
            status_code=500,
        )


@control_app.post("/control/e2e/diagnosis/{run_id}/issue")
async def create_e2e_diagnostic_issue(
    request: Request,
    run_id: int,
    repo_root: str = Query(...),
) -> JSONResponse:
    """Create a GitHub issue for diagnosing E2E test failures.

    1. Creates comprehensive diagnosis
    2. Writes diagnostic file to .issue-orchestrator/diagnostics/
    3. Creates GitHub issue with summary and file reference

    Path params:
        run_id: int - Run ID

    Query params:
        repo_root: str - Repository root path

    JSON body:
        agent: str - Agent label to assign (e.g., "agent:developer")

    Returns:
        {status: "created", issue_number, url, diagnostic_file}
    """
    from ..infra.e2e_db import E2EDB
    from ..infra.e2e_run_diagnosis import (
        create_e2e_run_diagnosis,
        generate_diagnostic_issue_body,
        write_e2e_diagnostic,
    )

    if not _orchestrator:
        return JSONResponse(
            {"error": "Orchestrator not running"},
            status_code=503,
        )

    # Parse request body for agent
    try:
        body = await request.json()
    except Exception:
        body = {}

    agent = body.get("agent", "").strip()
    if not agent:
        return JSONResponse(
            {"error": "Agent label is required"},
            status_code=400,
        )

    validated_root = _validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse(
            {"error": "Invalid repo_root"},
            status_code=400,
        )

    db_path = validated_root / ".issue-orchestrator" / "e2e.db"
    if not db_path.exists():
        return JSONResponse(
            {"error": "not_found", "detail": "E2E database not found"},
            status_code=404,
        )

    try:
        # Create diagnosis
        db = E2EDB(db_path)
        diagnosis = create_e2e_run_diagnosis(run_id, db)
        if diagnosis is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"Run {run_id} not found"},
                status_code=404,
            )

        # Write diagnostic file
        diagnostic_ref = write_e2e_diagnostic(validated_root, diagnosis)

        # Generate issue content
        title = f"E2E Test Failures - Run #{run_id} ({diagnosis.failed_count} failures)"
        body = generate_diagnostic_issue_body(diagnosis, diagnostic_ref)
        labels = [agent, "e2e-failure", "bug"]

        # Create issue via repository_host
        result = _orchestrator.repository_host.create_issue(
            title=title,
            body=body,
            labels=labels,
        )

        if result is None:
            return JSONResponse(
                {"error": "Failed to create issue"},
                status_code=500,
            )

        return JSONResponse({
            "status": "created",
            "issue_number": result.get("number"),
            "url": result.get("html_url"),
            "diagnostic_file": diagnostic_ref.relative_path if diagnostic_ref else None,
        })

    except Exception as e:
        logger.exception("Failed to create E2E diagnostic issue: %s", e)
        return JSONResponse(
            {"error": "issue_creation_error", "detail": str(e)},
            status_code=500,
        )


# ---------------------------------------------------------------------------
# E2E Triage Endpoints (for composite issue management)
# ---------------------------------------------------------------------------


def _build_issue_status(run_issue: Any, db: Any) -> dict:
    """Build issue status dict for triage response. Extracted to reduce complexity."""
    if not run_issue:
        return {
            "parent_issue_url": None,
            "parent_issue_closed": False,
            "sub_issues": [],
            "sub_issues_summary": {"total": 0, "resolved": 0},
        }

    repo = _orchestrator.config.repo if _orchestrator else None
    parent_issue_url = f"https://github.com/{repo}/issues/{run_issue.github_issue_number}" if repo else None
    parent_issue_closed = run_issue.closed_at is not None

    sub_issues = []
    sub_issues_summary = {"total": 0, "resolved": 0}

    failure_issues = db.get_failure_issues_for_parent(run_issue.github_issue_number)
    for fi in failure_issues:
        is_resolved = fi.resolved_at is not None
        sub_issues.append({
            "issue_number": fi.github_issue_number,
            "nodeid": fi.nodeid,
            "resolved": is_resolved,
            "resolution": fi.resolution,
            "url": f"https://github.com/{repo}/issues/{fi.github_issue_number}" if repo else None,
        })
        sub_issues_summary["total"] += 1
        if is_resolved:
            sub_issues_summary["resolved"] += 1

    return {
        "parent_issue_url": parent_issue_url,
        "parent_issue_closed": parent_issue_closed,
        "sub_issues": sub_issues,
        "sub_issues_summary": sub_issues_summary,
    }


@control_app.get("/control/e2e/triage/{run_id}")
async def e2e_triage_data(
    run_id: int,
    repo_root: str = Query(...),
) -> JSONResponse:
    """Get triage data for an E2E run - failures with issue/flakiness metadata.

    Returns data needed for the triage view where user can choose to
    create issues or dismiss failures.

    Path params:
        run_id: int - Run ID

    Query params:
        repo_root: str - Repository root path

    Returns:
        {
            run: {...},
            failures: [
                {
                    nodeid: str,
                    longrepr: str | null,
                    duration_seconds: float | null,
                    existing_issue: {issue_number, created_at, resolution} | null,
                    flake_count: int (recent flakes in window),
                    is_likely_flaky: bool
                },
                ...
            ],
            has_parent_issue: bool,
            parent_issue_number: int | null,
            flake_threshold: int
        }
    """
    from ..infra.e2e_db import E2EDB

    validated_root = _validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse(
            {"error": "Invalid repo_root"},
            status_code=400,
        )

    db_path = validated_root / ".issue-orchestrator" / "e2e.db"
    if not db_path.exists():
        return JSONResponse(
            {"error": "not_found", "detail": "E2E database not found"},
            status_code=404,
        )

    try:
        db = E2EDB(db_path)

        # Get run info
        run = db.get_run(run_id)
        if run is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"Run {run_id} not found"},
                status_code=404,
            )

        # Get failed tests
        failed_results = db.get_failed_tests(run_id)

        # Check if this run already has a parent issue
        run_issue = db.get_run_issue(run_id)

        # Get flake thresholds from config, or use defaults
        e2e_config = _orchestrator.config.e2e if _orchestrator else _DEFAULT_E2E_CONFIG
        flake_threshold = e2e_config.flake_threshold
        flake_window = e2e_config.flake_window_runs

        # Build triage data for each failure using flip-rate stability
        failures = []
        for result in failed_results:
            # Check for existing open issue
            existing = db.find_open_failure_issue(result.nodeid)

            # Get flip-rate stability
            stability = db.get_test_stability(
                result.nodeid,
                window_runs=flake_window,
                flake_threshold_percent=float(flake_threshold),
            )

            failures.append({
                "nodeid": result.nodeid,
                "longrepr": result.longrepr,
                "duration_seconds": result.duration_seconds,
                "existing_issue": existing.to_dict() if existing else None,
                "flake_count": stability.flip_count,
                "flip_count": stability.flip_count,
                "flip_rate": stability.flip_rate,
                "flip_rate_percent": stability.flip_rate_percent,
                "category": stability.category,
                "is_likely_flaky": stability.is_likely_flaky,
            })

        # Build issue status info if parent issue exists
        issue_status = _build_issue_status(run_issue, db)

        return JSONResponse({
            "run": run.to_dict(),
            "failures": failures,
            "has_parent_issue": run_issue is not None,
            "parent_issue_number": run_issue.github_issue_number if run_issue else None,
            **issue_status,
            "flake_threshold": flake_threshold,
        })

    except Exception as e:
        logger.exception("Failed to get triage data: %s", e)
        return JSONResponse(
            {"error": "triage_error", "detail": str(e)},
            status_code=500,
        )


def _extract_test_log_excerpt(log_path: str | None, nodeid: str) -> str | None:
    """Extract log lines relevant to a specific test."""
    if not log_path:
        return None

    from ..infra.e2e_run_diagnosis import _read_log_content

    log_exists, log_content = _read_log_content(log_path)
    if not log_exists or not log_content:
        return None

    short_name = nodeid.split("::")[-1]
    lines = log_content.split("\n")
    relevant_lines = []
    in_test = False

    for line in lines:
        if short_name in line or nodeid in line:
            in_test = True
        if in_test:
            relevant_lines.append(line)
            if len(relevant_lines) > 100:
                break

    return "\n".join(relevant_lines) if relevant_lines else None


def _calculate_history_summary(history: list[dict]) -> dict:
    """Calculate pass/fail summary from test history."""
    if not history:
        return {"total": 0, "passed": 0, "failed": 0, "pass_rate": None}

    passed = sum(1 for h in history if h["outcome"] == "passed")
    failed = sum(1 for h in history if h["outcome"] in ("failed", "error"))
    total = len(history)
    pass_rate = passed / total if total > 0 else None

    return {"total": total, "passed": passed, "failed": failed, "pass_rate": pass_rate}


def _count_untriaged_failures(db: object, run_id: int) -> int:
    """Count failures without corresponding open issues.

    Args:
        db: E2EDB instance (typed as object to avoid circular import)
        run_id: The run ID to check
    """
    failed_tests = db.get_failed_tests(run_id)  # type: ignore[attr-defined]
    count = 0
    for result in failed_tests:
        existing = db.find_open_failure_issue(result.nodeid)  # type: ignore[attr-defined]
        if not existing:
            count += 1
    return count


@control_app.get("/control/e2e/test/{run_id}")
async def e2e_test_detail(
    run_id: int,
    nodeid: str = Query(...),
    repo_root: str = Query(...),
) -> JSONResponse:
    """Get detailed information for a single test failure."""
    from ..infra.e2e_db import E2EDB

    validated_root = _validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse({"error": "Invalid repo_root"}, status_code=400)

    db_path = validated_root / ".issue-orchestrator" / "e2e.db"
    if not db_path.exists():
        return JSONResponse(
            {"error": "not_found", "detail": "E2E database not found"},
            status_code=404,
        )

    try:
        db = E2EDB(db_path)
        run = db.get_run(run_id)
        if run is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"Run {run_id} not found"},
                status_code=404,
            )

        test_result = db.get_test_result(run_id, nodeid)
        if test_result is None:
            return JSONResponse(
                {"error": "not_found", "detail": f"Test {nodeid} not found in run {run_id}"},
                status_code=404,
            )

        # Get flip-rate stability info
        e2e_config = _orchestrator.config.e2e if _orchestrator else _DEFAULT_E2E_CONFIG
        stability = db.get_test_stability(
            nodeid,
            window_runs=e2e_config.flake_window_runs,
            flake_threshold_percent=float(e2e_config.flake_threshold),
        )

        # Get history and existing issue
        existing_issue = db.find_open_failure_issue(nodeid)
        history = db.get_test_history(nodeid, limit=10)
        history_data = [
            {"run_id": h["run_id"], "outcome": h["outcome"], "timestamp": h["started_at"]}
            for h in history
        ]

        return JSONResponse({
            "test": {
                "nodeid": test_result.nodeid,
                "outcome": test_result.outcome,
                "longrepr": test_result.longrepr,
                "duration_seconds": test_result.duration_seconds,
                "retry_outcome": test_result.retry_outcome,
            },
            "run": {
                "id": run.id,
                "status": run.status,
                "started_at": run.started_at,
                "commit_sha": run.commit_sha,
                "branch": run.branch,
            },
            "history": history_data,
            "history_summary": _calculate_history_summary(history),
            "flake_count": stability.flip_count,
            "flip_count": stability.flip_count,
            "flip_rate": stability.flip_rate,
            "flip_rate_percent": stability.flip_rate_percent,
            "category": stability.category,
            "is_likely_flaky": stability.is_likely_flaky,
            "existing_issue": existing_issue.to_dict() if existing_issue else None,
            "log_excerpt": _extract_test_log_excerpt(run.log_path, nodeid),
        })

    except Exception as e:
        logger.exception("Failed to get test detail: %s", e)
        return JSONResponse(
            {"error": "test_detail_error", "detail": str(e)},
            status_code=500,
        )


def _create_e2e_sub_issues(
    tracker: Any,
    parent_issue: Any,
    nodeids: list[str],
    results_by_nodeid: dict,
    run: Any,
    db: Any,
    run_id: int,
    agent: str,
) -> list[dict]:
    """Create sub-issues for selected test failures. Returns list of created issues."""
    sub_issues = []
    sub_labels = ["e2e:test-failure", agent]

    for nodeid in nodeids:
        test_result = results_by_nodeid.get(nodeid)
        if not test_result:
            logger.warning("[e2e-create-issues] Node ID not found: %s", nodeid)
            continue

        sub_issue = tracker.create_test_failure_issue(
            parent_issue=parent_issue,
            test_result=test_result,
            first_failing_sha=run.commit_sha or "",
            last_passing_sha=None,
            labels=sub_labels,
        )

        if not sub_issue:
            continue

        db.record_failure_issue(
            nodeid=nodeid,
            github_issue_number=sub_issue.issue_number,
            parent_issue_number=parent_issue.issue_number,
            first_failing_run_id=run_id,
            first_failing_sha=run.commit_sha or "",
        )
        sub_issues.append({
            "number": sub_issue.issue_number,
            "url": sub_issue.html_url,
            "nodeid": nodeid,
        })

    return sub_issues


def _auto_create_e2e_issues_if_needed(
    config: Any,
    db: Any,
    run: Any,
    proc_status: dict,
) -> None:
    """Auto-create E2E failure issues when enabled and not already created."""
    if not config.e2e.auto_create_issues:
        return
    if run is None or run.status != "failed":
        return
    if proc_status.get("running"):
        return
    if not _orchestrator:
        logger.warning("[e2e-auto-issues] Orchestrator not running; cannot create issues")
        return

    existing_run_issue = db.get_run_issue(run.id)
    if existing_run_issue:
        return

    failed_results = db.get_failed_tests(run.id)
    if not failed_results:
        return

    try:
        tracker = _orchestrator.deps.e2e_issue_tracker
        parent_issue = tracker.create_run_issue(
            run=run,
            failed_count=len(failed_results),
            labels=["e2e:run"],
        )
        if parent_issue is None:
            return

        db.record_run_issue(run.id, parent_issue.issue_number)

        results_by_nodeid = {r.nodeid: r for r in failed_results}
        _create_e2e_sub_issues(
            tracker,
            parent_issue,
            list(results_by_nodeid.keys()),
            results_by_nodeid,
            run,
            db,
            run.id,
            config.e2e.issue_agent_label,
        )
        logger.info(
            "[e2e-auto-issues] Created parent #%d with %d sub-issues for run #%d",
            parent_issue.issue_number,
            len(results_by_nodeid),
            run.id,
        )
    except Exception as exc:
        logger.exception("[e2e-auto-issues] Failed to auto-create issues: %s", exc)


def _validate_e2e_create_request(
    nodeids: list,
    agent: str,
    repo_root: str,
) -> JSONResponse | Path:
    """Validate request params for e2e issue creation. Returns JSONResponse on error, Path on success."""
    if not nodeids:
        return JSONResponse({"error": "No test failures selected"}, status_code=400)
    if not agent:
        return JSONResponse({"error": "Agent label is required"}, status_code=400)

    validated_root = _validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse({"error": "Invalid repo_root"}, status_code=400)

    db_path = validated_root / ".issue-orchestrator" / "e2e.db"
    if not db_path.exists():
        return JSONResponse({"error": "not_found", "detail": "E2E database not found"}, status_code=404)

    return db_path


@control_app.post("/control/e2e/create-issues/{run_id}")
async def e2e_create_issues(
    request: Request,
    run_id: int,
    repo_root: str = Query(...),
) -> JSONResponse:
    """Create GitHub issues from E2E test failures.

    Creates a parent issue for the run and sub-issues for each selected failure.
    Issues are linked using GitHub's native sub-issues feature.

    Path params:
        run_id: int - Run ID

    Query params:
        repo_root: str - Repository root path

    JSON body:
        nodeids: list[str] - Test node IDs to create issues for
        agent: str - Agent label to assign (e.g., "agent:developer")

    Returns:
        {
            status: "created",
            parent_issue: {number, url},
            sub_issues: [{number, url, nodeid}, ...]
        }
    """
    from ..infra.e2e_db import E2EDB

    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    nodeids = body.get("nodeids", [])
    agent = body.get("agent", "").strip()

    validation_result = _validate_e2e_create_request(nodeids, agent, repo_root)
    if isinstance(validation_result, JSONResponse):
        return validation_result
    db_path = validation_result

    try:
        db = E2EDB(db_path)
        run = db.get_run(run_id)
        if run is None:
            return JSONResponse({"error": "not_found", "detail": f"Run {run_id} not found"}, status_code=404)

        existing_run_issue = db.get_run_issue(run_id)
        if existing_run_issue:
            return JSONResponse(
                {"error": "Issues already created for this run",
                 "parent_issue_number": existing_run_issue.github_issue_number},
                status_code=409)

        tracker = _orchestrator.deps.e2e_issue_tracker

        parent_issue = tracker.create_run_issue(run=run, failed_count=len(nodeids), labels=["e2e:run"])
        if parent_issue is None:
            return JSONResponse({"error": "Failed to create parent issue"}, status_code=500)

        db.record_run_issue(run_id, parent_issue.issue_number)

        failed_results = db.get_failed_tests(run_id)
        results_by_nodeid = {r.nodeid: r for r in failed_results}

        sub_issues = _create_e2e_sub_issues(
            tracker, parent_issue, nodeids, results_by_nodeid, run, db, run_id, agent)

        logger.info(
            "[e2e-create-issues] Created parent #%d with %d sub-issues for run #%d",
            parent_issue.issue_number, len(sub_issues), run_id)

        return JSONResponse({
            "status": "created",
            "parent_issue": {"number": parent_issue.issue_number, "url": parent_issue.html_url},
            "sub_issues": sub_issues,
        })

    except Exception as e:
        logger.exception("Failed to create E2E issues: %s", e)
        return JSONResponse({"error": "issue_creation_error", "detail": str(e)}, status_code=500)


def _sync_close_passing_issues(tracker, open_issues, passing_nodeids, run_id, commit_sha, db):
    """Close sub-issues for tests that now pass."""
    closed_issues = []
    parent_issues_to_check = set()

    for issue in open_issues:
        if issue.nodeid in passing_nodeids:
            comment = (
                f"Test now passing as of run #{run_id} "
                f"(commit `{commit_sha[:12]}`)\n\n"
                f"_Auto-closed by orchestrator._"
            )
            if tracker.close_issue_with_comment(issue.github_issue_number, comment):
                db.resolve_failure_issue(issue.nodeid, "passed")
                closed_issues.append({
                    "number": issue.github_issue_number,
                    "nodeid": issue.nodeid,
                })
                parent_issues_to_check.add(issue.parent_issue_number)
                logger.info(
                    "[e2e-sync] Closed issue #%d for passing test: %s",
                    issue.github_issue_number, issue.nodeid)

    return closed_issues, parent_issues_to_check


def _sync_close_parent_issues(tracker, parent_issues_to_check, run_id, db):
    """Close parent issues if all their sub-issues are resolved."""
    closed_parents = []
    for parent_number in parent_issues_to_check:
        unresolved = db.get_unresolved_failure_count(parent_number)
        if unresolved == 0:
            comment = (
                f"All sub-issues resolved as of run #{run_id}\n\n"
                f"_Auto-closed by orchestrator._"
            )
            if tracker.close_issue_with_comment(parent_number, comment):
                closed_parents.append(parent_number)
                logger.info("[e2e-sync] Closed parent issue #%d", parent_number)
    return closed_parents


@control_app.post("/control/e2e/sync-issues/{run_id}")
async def e2e_sync_issues(
    run_id: int,
    repo_root: str = Query(...),
) -> JSONResponse:
    """Sync E2E issue state based on test results from a run.

    For any test that passed in this run but has an open failure issue,
    close the GitHub issue and mark it as resolved. If all sub-issues for
    a parent are resolved, close the parent issue too.

    Path params:
        run_id: int - Run ID to sync from

    Query params:
        repo_root: str - Repository root path

    Returns:
        {
            status: "synced",
            closed_issues: [{number, nodeid}, ...],
            closed_parent_issues: [number, ...],
        }
    """
    from ..infra.e2e_db import E2EDB

    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    validated_root = _validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse({"error": "Invalid repo_root"}, status_code=400)

    db_path = validated_root / ".issue-orchestrator" / "e2e.db"
    if not db_path.exists():
        return JSONResponse({"error": "not_found", "detail": "E2E database not found"}, status_code=404)

    try:
        db = E2EDB(db_path)
        run = db.get_run(run_id)
        if run is None:
            return JSONResponse({"error": "not_found", "detail": f"Run {run_id} not found"}, status_code=404)

        summary = db.get_test_summary(run_id)
        passing_nodeids = {t["nodeid"] for t in summary["passed"]}
        passing_nodeids.update(t["nodeid"] for t in summary["passed_on_retry"])

        open_issues = db.get_all_open_failure_issues()
        tracker = _orchestrator.deps.e2e_issue_tracker

        commit_sha = run.commit_sha or "unknown"
        closed_issues, parent_issues_to_check = _sync_close_passing_issues(
            tracker, open_issues, passing_nodeids, run_id, commit_sha, db)

        closed_parents = _sync_close_parent_issues(tracker, parent_issues_to_check, run_id, db)

        logger.info(
            "[e2e-sync] Run #%d: closed %d sub-issues, %d parent issues",
            run_id, len(closed_issues), len(closed_parents))

        return JSONResponse({
            "status": "synced",
            "closed_issues": closed_issues,
            "closed_parent_issues": closed_parents,
        })

    except Exception as e:
        logger.exception("Failed to sync E2E issues: %s", e)
        return JSONResponse({"error": "sync_error", "detail": str(e)}, status_code=500)


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
