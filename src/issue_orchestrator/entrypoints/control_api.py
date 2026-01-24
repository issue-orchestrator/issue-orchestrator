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
- GET /control/e2e/logs/{run_id} - Get run logs
- GET /control/e2e/failed/{run_id} - Get failed tests from a run
"""

import asyncio
import json
import logging
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional

from fastapi import FastAPI, Request, Query
from fastapi.responses import JSONResponse, HTMLResponse
from sse_starlette.sse import EventSourceResponse

from ..infra import gh_audit

# Path to templates
_TEMPLATES_DIR = Path(__file__).parent.parent / "templates"

if TYPE_CHECKING:
    from ..infra.orchestrator import Orchestrator

logger = logging.getLogger(__name__)

# Default E2E config (used when orchestrator not available)
from ..infra.config import E2EConfig
_DEFAULT_E2E_CONFIG = E2EConfig()

# Create minimal control API app
control_app = FastAPI(title="Issue Orchestrator Control API")

# Global reference to orchestrator (set at startup)
_orchestrator: "Orchestrator | None" = None


def set_orchestrator(orchestrator: "Orchestrator") -> None:
    """Set the orchestrator instance for the control API."""
    global _orchestrator
    _orchestrator = orchestrator


def get_orchestrator() -> "Orchestrator | None":
    """Get the orchestrator instance."""
    return _orchestrator


@control_app.post("/api/refresh")
async def refresh(request: Request) -> JSONResponse:
    """Request an immediate refresh of issues from GitHub.

    This triggers the orchestrator to fetch issues on the next loop iteration,
    bypassing the queue_refresh_seconds interval.

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
async def shutdown() -> JSONResponse:
    """Request graceful shutdown of the orchestrator."""
    if _orchestrator is None:
        return JSONResponse({"error": "Orchestrator not initialized"}, status_code=503)

    _orchestrator.request_shutdown()
    return JSONResponse({"status": "shutdown_requested"})


@control_app.post("/api/preflight-push")
async def preflight_push(request: Request) -> JSONResponse:
    """Check if a git push would succeed (dry-run).

    This endpoint allows agent-done to verify a push would work before
    completing, while the agent is still active and can fix any issues.

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

    This endpoint is called by `agent-done --resume` after writing a completion
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

    from ..control.worktree_manager import get_worktree_path

    # Get worktree path for this issue
    worktree = get_worktree_path(_orchestrator.config, issue_number)

    if not worktree.exists():
        return JSONResponse({
            "success": False,
            "error": f"Worktree not found: {worktree}",
            "hint": "The worktree may have been cleaned up. Check if the issue is still blocked.",
        }, status_code=404)

    # Check for completion.json
    completion_path = worktree / ".issue-orchestrator" / "completion.json"
    if not completion_path.exists():
        return JSONResponse({
            "success": False,
            "error": "No completion record found",
            "hint": "Run 'agent-done completed --implementation ... --problems ...' first.",
        }, status_code=404)

    # Get issue title - try cache first, then fetch
    issue_title = f"Issue #{issue_number}"  # Default fallback
    try:
        # Check if issue is in cached queue
        for issue in _orchestrator.state.cached_queue_issues:
            if issue.number == issue_number:
                issue_title = issue.title
                break
        else:
            # Fetch from GitHub
            issue_data = _orchestrator.deps.repository_host.get_issue(issue_number)
            if issue_data:
                issue_title = issue_data.title
    except Exception as e:
        logger.warning("Could not fetch issue title for #%d: %s", issue_number, e)
        # Continue with default title

    # Process the completion
    try:
        result = _orchestrator.deps.completion_processor.process(
            worktree=worktree,
            issue_number=issue_number,
            issue_title=issue_title,
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
    with environment variables set so `agent-done --resume` can signal completion
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

    from ..control.worktree_manager import get_worktree_path

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
    issue = None
    for cached_issue in state.cached_queue_issues:
        if cached_issue.number == issue_number:
            issue = cached_issue
            break

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
        "'agent-done --resume' to continue the orchestrator flow."
    )
    base_command = agent_config.get_command(
        issue_number=issue_number,
        issue_title=issue.title,
        worktree=worktree,
        existing_work=debug_context,
    )

    # Set env vars for agent-done --resume
    env_exports = f"export ORCHESTRATOR_ISSUE_NUMBER='{issue_number}'"
    env_exports += f" ORCHESTRATOR_API_PORT='{config.web_port}'"
    env_exports += f" ORCHESTRATOR_AGENT_LABEL='{agent_type}'"
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
        "hint": f"Debug session launched. When done, run 'agent-done --resume' to process completion.",
    })


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
        from ..infra import labels as label_module

        # Remove blocked-related labels
        labels_to_remove = [
            label_module.BLOCKED,
            label_module.BLOCKED_NEEDS_HUMAN,
            label_module.BLOCKED_FAILED,
        ]

        removed = []
        for label in labels_to_remove:
            try:
                _orchestrator.repository_host.remove_label(issue_number, label)
                removed.append(label)
            except Exception:
                pass  # Label might not exist, that's fine

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
        from ..infra import labels as label_module

        # Remove all orchestrator-managed labels to fully dismiss
        labels_to_remove = [
            label_module.BLOCKED,
            label_module.BLOCKED_NEEDS_HUMAN,
            label_module.BLOCKED_FAILED,
            label_module.IN_PROGRESS,
        ]

        removed = []
        for label in labels_to_remove:
            try:
                _orchestrator.repository_host.remove_label(issue_number, label)
                removed.append(label)
            except Exception:
                pass  # Label might not exist, that's fine

        # Remove from session history if present
        _orchestrator.state.session_history = [
            entry for entry in _orchestrator.state.session_history
            if entry.issue_number != issue_number
        ]

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
    """
    import os
    import signal
    import threading

    from ..infra import supervisor
    from ..infra.repo_registry import list_repos

    # Parse optional body
    stop_orchestrators = False
    try:
        body = await request.json()
        stop_orchestrators = body.get("stop_orchestrators", False)
    except json.JSONDecodeError:
        pass  # No body is fine, default to not stopping orchestrators

    # Stop orchestrators if requested
    stopped_repos = []
    if stop_orchestrators:
        repos = list_repos()
        for repo in repos:
            path = Path(repo.path)
            if path.exists():
                status_info = supervisor.status(path)
                if status_info.state == "running":
                    logger.info("Stopping orchestrator for %s before shutdown", repo.path)
                    if supervisor.stop(path):
                        stopped_repos.append(repo.path)

    def delayed_shutdown():
        import time
        time.sleep(0.5)  # Give time for response to be sent
        os.kill(os.getpid(), signal.SIGTERM)
        time.sleep(2)  # Give SIGTERM time to work
        # Force kill if still alive
        os.kill(os.getpid(), signal.SIGKILL)

    # daemon=False so thread survives to send SIGKILL if needed
    threading.Thread(target=delayed_shutdown, daemon=False).start()
    return JSONResponse({
        "status": "shutting_down",
        "stopped_orchestrators": stopped_repos,
    })


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

    content = template_path.read_text()
    return HTMLResponse(content)


# =============================================================================
# Supervisor Control API - Process Management Endpoints
# =============================================================================
# These endpoints manage orchestrator processes via the Supervisor.
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


def _detect_orchestrator_by_port(repo_root: Path, config_name: str) -> dict[str, Any] | None:
    """Detect an orchestrator by probing the configured port.

    Returns info dict with port and metadata if an orchestrator responds
    and matches repo_root.
    """
    import httpx
    import time

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
    try:
        status_resp = httpx.get(f"{base_url}/api/status", timeout=0.6)
        if status_resp.status_code == 200:
            status_data = status_resp.json()
            details["status"] = status_data
            last_tick = status_data.get("last_tick_time")
            if isinstance(last_tick, (int, float)) and last_tick > 0:
                tick_age = time.time() - last_tick
                details["tick_age_seconds"] = tick_age
                if tick_age > 120:
                    details["health"] = "stale"
                else:
                    details["health"] = "ok"
    except Exception:
        details.setdefault("health", "unknown")

    return details


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
    from ..infra import supervisor
    from ..infra.repo_lock import AlreadyRunning
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

    # Validate port if provided
    port = body.get("port")
    if port is not None:
        if not isinstance(port, int) or port < 1 or port > 65535:
            return JSONResponse({"error": "Invalid port"}, status_code=400)

    config_name = body.get("config_name", "default.yaml")
    if not config_name.endswith(".yaml"):
        config_name += ".yaml"
    force_restart = bool(body.get("force_restart", False))

    try:
        detected = _detect_orchestrator_by_port(repo_root, config_name)
        if detected and not force_restart:
            return JSONResponse({
                "error": "orphaned_running",
                "status": "running",
                "port": detected["port"],
                "repo_root": str(repo_root),
                "health": detected.get("health", "unknown"),
                "tick_age_seconds": detected.get("tick_age_seconds"),
            }, status_code=409)
        if detected and force_restart:
            stopped = supervisor.stop_by_port(detected["port"], force=True)
            if not stopped:
                return JSONResponse({
                    "error": "stop_failed",
                    "detail": "Unable to stop existing orchestrator process.",
                }, status_code=500)

        # Update selected config in registry
        set_selected_config(repo_root, config_name)

        # Load config to check if multi-instance mode
        from ..infra.config import Config, get_config_path
        config_path = get_config_path(repo_root, config_name)
        config = Config.load(config_path)

        if config.instances > 1:
            # Multi-instance mode: start all instances
            infos = supervisor.start_instances(repo_root, config_name=config_name)
            return JSONResponse({
                "status": "started",
                "instances": [
                    {
                        "pid": info.pid,
                        "port": info.http_port,
                        "instance_id": info.instance_id,
                    }
                    for info in infos
                ],
                "repo_root": str(repo_root),
                "config_name": config_name,
            })
        else:
            # Single instance mode
            info = supervisor.start(repo_root, config_name=config_name)
            return JSONResponse({
                "status": "started",
                "pid": info.pid,
                "port": info.http_port,
                "repo_root": str(repo_root),
                "config_name": config_name,
            })
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
                supervisor.stop(repo_root)
                # Brief pause to allow cleanup
                import time
                time.sleep(0.5)
                # Try starting again
                info = supervisor.start(repo_root, config_name=config_name)
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
    from ..infra import supervisor

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
    port_override = body.get("port")
    if port_override is not None and (not isinstance(port_override, int) or port_override < 1 or port_override > 65535):
        return JSONResponse({"error": "Invalid port"}, status_code=400)

    logger.info("[control_stop] Calling supervisor.stop(%s, force=%s)", repo_root, force)

    status_info = supervisor.status(repo_root)
    if status_info.state != "running" and port_override:
        if not _confirm_orchestrator_at_port(repo_root, port_override):
            return JSONResponse({
                "error": "port_mismatch",
                "detail": "No matching orchestrator found on the provided port.",
            }, status_code=409)
        stopped = supervisor.stop_by_port(port_override, force=force)
        stopped_count = 1 if stopped else 0
    else:
        # Stop all instances (single and multi-instance)
        stopped_count = supervisor.stop_all_instances(repo_root, force=force)
        stopped = stopped_count > 0
    logger.info("[control_stop] supervisor.stop_all_instances returned: %d", stopped_count)

    if stopped:
        return JSONResponse({
            "status": "stopped",
            "repo_root": str(repo_root),
            "stopped_count": stopped_count,
        })
    else:
        return JSONResponse({
            "status": "not_running",
            "repo_root": str(repo_root),
        })


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
    from ..infra import supervisor

    path = _validate_repo_root(repo_root)
    if path is None:
        return JSONResponse(
            {"error": "Invalid or missing repo_root"},
            status_code=400,
        )

    # Get status of all instances
    selected = config_name or _get_selected_config(path) or "default.yaml"
    multi_status = supervisor.status_all_instances(path, config_name=selected)

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
        return JSONResponse(multi_status.instances[0].to_dict())

    # No running instances - check for orphaned process
    status_info = supervisor.status(path)
    if status_info.state != "running":
        detected = _detect_orchestrator_by_port(path, selected)
        if detected:
            status_data = detected.get("status", {})
            return JSONResponse({
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
            })

    return JSONResponse(status_info.to_dict())


@control_app.post("/control/orchestrator/pause")
async def control_pause(request: Request) -> JSONResponse:
    """Pause the orchestrator for a repository (passthrough to running instance).

    JSON body:
        repo_root: str - Repository root path
    """
    from ..infra import supervisor
    import httpx

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

    status_info = supervisor.status(repo_root)
    if status_info.state != "running" or status_info.port is None:
        return JSONResponse({
            "error": "not_running",
            "state": status_info.state,
        }, status_code=400)

    # Forward to running orchestrator
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"http://127.0.0.1:{status_info.port}/api/pause",
                timeout=10.0,
            )
            return JSONResponse(response.json())
    except Exception as e:
        return JSONResponse({
            "error": "passthrough_failed",
            "detail": str(e),
        }, status_code=502)


@control_app.post("/control/orchestrator/resume")
async def control_resume(request: Request) -> JSONResponse:
    """Resume the orchestrator for a repository (passthrough to running instance).

    JSON body:
        repo_root: str - Repository root path
    """
    from ..infra import supervisor
    import httpx

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

    status_info = supervisor.status(repo_root)
    if status_info.state != "running" or status_info.port is None:
        return JSONResponse({
            "error": "not_running",
            "state": status_info.state,
        }, status_code=400)

    # Forward to running orchestrator
    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"http://127.0.0.1:{status_info.port}/api/resume",
                timeout=10.0,
            )
            return JSONResponse(response.json())
    except Exception as e:
        return JSONResponse({
            "error": "passthrough_failed",
            "detail": str(e),
        }, status_code=502)


@control_app.post("/control/orchestrator/refresh")
async def control_refresh(request: Request) -> JSONResponse:
    """Trigger refresh on the orchestrator for a repository (passthrough).

    JSON body:
        repo_root: str - Repository root path
        inflight_stable_ids: list[str] (optional) - Expected issue IDs
    """
    from ..infra import supervisor
    import httpx

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

    status_info = supervisor.status(repo_root)
    if status_info.state != "running" or status_info.port is None:
        return JSONResponse({
            "error": "not_running",
            "state": status_info.state,
        }, status_code=400)

    # Forward to running orchestrator with optional inflight_stable_ids
    forward_body = {}
    if "inflight_stable_ids" in body:
        forward_body["inflight_stable_ids"] = body["inflight_stable_ids"]

    try:
        async with httpx.AsyncClient() as client:
            response = await client.post(
                f"http://127.0.0.1:{status_info.port}/api/refresh",
                json=forward_body if forward_body else None,
                timeout=10.0,
            )
            return JSONResponse(response.json())
    except Exception as e:
        return JSONResponse({
            "error": "passthrough_failed",
            "detail": str(e),
        }, status_code=502)


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
    from ..infra.config import Config
    from ..infra.doctor import run_doctor
    from ..execution.command_runner import LocalCommandRunner

    path = _validate_repo_root(repo_root)
    if path is None:
        return JSONResponse(
            {"error": "Invalid or missing repo_root"},
            status_code=400,
        )

    # Try to load config from repo (new location)
    from ..infra.config import get_config_path, list_configs

    config = None
    config_path = None
    available = list_configs(path)
    if available:
        config_path = get_config_path(path, available[0])
        try:
            config = Config.load(config_path)
        except Exception:
            pass

    result = run_doctor(config=config, config_path=config_path, runner=LocalCommandRunner())
    return JSONResponse(result.to_dict())


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


# =============================================================================
# Multi-Repo Registry API Endpoints
# =============================================================================
# These endpoints manage the repo registry for multi-repo supervision.


def _build_repos_status() -> list[dict[str, Any]]:  # noqa: C901, PLR0912 - multi-repo status with state aggregation
    """Build status data for all registered repos.

    Shared by both the REST endpoint and SSE stream.
    """
    import httpx

    from ..infra import supervisor
    from ..infra.repo_registry import list_repos
    from ..infra.config import list_configs, get_config_path, Config

    repos = list_repos()
    result = []

    for repo in repos:
        # Get status for each repo
        path = Path(repo.path)

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
            "configs": available_configs,
            "selected_config": repo.selected_config,
            "expected_instances": expected_instances,
        }

        if expected_instances > 1 and path.exists():
            # Multi-instance mode: get status for all instances
            multi_status = supervisor.status_all_instances(path)
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

                repo_data["instances"].append(inst_data)

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
            status_info = supervisor.status(path) if path.exists() else None
            repo_data["status"] = status_info.to_dict() if status_info else None

            if status_info and status_info.state != "running" and path.exists():
                detected = _detect_orchestrator_by_port(path, repo.selected_config)
                if detected:
                    status_data = detected.get("status", {})
                    repo_data["status"] = {
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
    from ..infra.repo_identity import get_repo_head_sha
    repo_root = Path.cwd()
    commit_sha = get_repo_head_sha(repo_root)
    return JSONResponse({
        "repo_root": str(repo_root),
        "commit_sha": commit_sha,
        "commit_short": commit_sha[:7] if commit_sha else None,
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
    from ..infra import supervisor
    from ..infra.repo_registry import remove_repo

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
            supervisor.stop(path)

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
    max_depth: int = Query(default=2, description="Max directory depth to search"),
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
        paths_to_search = [
            home / "dev",
            home / "projects",
            home / "code",
            home / "repos",
            home / "src",
            home / "work",
            home / "github",
        ]

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


# =============================================================================
# Setup Wizard API Endpoints
# =============================================================================
# These endpoints support the GUI setup wizard for configuring new repositories.


@control_app.get("/control/setup/prereqs")
async def setup_prereqs() -> JSONResponse:
    """Check prerequisites for setting up a repository.

    Returns status of git, GitHub auth, and Claude CLI.
    """
    import subprocess

    from ..execution.git_tools import run_git

    checks = {}

    # git
    ok, output = run_git(["--version"], timeout_s=5)
    checks["git"] = {
        "ok": ok,
        "version": output if ok else None,
    }

    # GitHub auth
    try:
        from ..execution.providers import resolve_github_token
        resolve_github_token(configured_token=None, configured_env=None)
        checks["github_auth"] = {"ok": True}
    except Exception as e:
        checks["github_auth"] = {"ok": False, "error": str(e)}

    # Claude CLI
    try:
        result = subprocess.run(
            ["claude", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        checks["claude"] = {
            "ok": result.returncode == 0,
            "version": result.stdout.strip() if result.returncode == 0 else None,
        }
    except (subprocess.TimeoutExpired, FileNotFoundError):
        checks["claude"] = {"ok": False, "version": None}

    all_ok = all(c.get("ok", False) for c in checks.values())

    return JSONResponse({
        "all_ok": all_ok,
        "checks": checks,
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
5. When complete, use `agent-done` to create a PR

## Important
- Always use `agent-done` when finished (not `git push` directly)
- If blocked, use `agent-done --blocked "reason"`
- If you need human input, use `agent-done --needs-human "question"`
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

Then: `agent-done completed --implementation "Reviewed PR #{{{{pr_number}}}}. Approved."`
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

`agent-done completed --implementation "Audited N PRs. Summary of findings."`
"""


# =============================================================================
# E2E Test Runner API
# =============================================================================
# These endpoints manage async E2E test execution per repository.


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
        result = runner.start(
            repo_root=repo_root,
            orchestrator_id=orchestrator_id,
            pytest_args=pytest_args,
            allow_retry_once=allow_retry,
            quarantine_file=config.e2e.quarantine_file,
        )
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

    if db_path.exists():
        try:
            db = E2EDB(db_path)
            run_obj = db.latest_run(config.orchestrator_id)
            if run_obj:
                last_run = run_obj.to_dict()
                # Get progress for running tests
                if run_obj.status == "running":
                    progress = db.get_progress(run_obj.id)
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
) -> JSONResponse:
    """Get details of a specific E2E run.

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
    from ..infra.config import Config

    validated_root = _validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse(
            {"error": "Invalid repo_root"},
            status_code=400,
        )

    # Get quarantine file path from config
    try:
        config = Config.find_and_load(validated_root)
        quarantine_file = config.e2e.quarantine_file
    except FileNotFoundError:
        quarantine_file = "tests/e2e/quarantine.txt"

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
    from ..infra.config import Config

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

    try:
        config = Config.find_and_load(validated_root)
        quarantine_file = config.e2e.quarantine_file
    except FileNotFoundError:
        quarantine_file = "tests/e2e/quarantine.txt"

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


@control_app.get("/control/e2e/flaky-tests")
async def e2e_flaky_tests(
    repo_root: str = Query(...),
    threshold: int = Query(default=3),
    window: int = Query(default=10),
) -> JSONResponse:
    """Get tests that exhibit flaky behavior above the threshold.

    Query params:
        repo_root: str - Repository root path
        threshold: int - Number of flakes to consider problematic (default: 3)
        window: int - Number of recent runs to check (default: 10)

    Returns:
        {flaky_tests: [{nodeid, flake_count}, ...], threshold, window}
    """
    from ..infra.e2e_db import E2EDB, load_quarantine_list
    from ..infra.config import Config

    validated_root = _validate_repo_root(repo_root)
    if validated_root is None:
        return JSONResponse({"error": "Invalid repo_root"}, status_code=400)

    db_path = validated_root / ".issue-orchestrator" / "e2e.db"
    if not db_path.exists():
        return JSONResponse({"error": "not_found", "detail": "E2E database not found"}, status_code=404)

    try:
        config = Config.find_and_load(validated_root)
        quarantine_file = config.e2e.quarantine_file
    except FileNotFoundError:
        quarantine_file = "tests/e2e/quarantine.txt"

    quarantine_path = validated_root / quarantine_file
    quarantined = load_quarantine_list(quarantine_path)

    db = E2EDB(db_path)
    flaky_tests = db.get_flaky_tests(threshold=threshold, window_runs=window)

    # Mark which ones are already quarantined
    for test in flaky_tests:
        test["is_quarantined"] = test["nodeid"] in quarantined

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

        # Build triage data for each failure
        failures = []
        for result in failed_results:
            # Check for existing open issue
            existing = db.find_open_failure_issue(result.nodeid)

            # Get flake history
            flake_count = db.get_flake_count(result.nodeid, window_runs=flake_window)
            is_likely_flaky = flake_count >= flake_threshold

            failures.append({
                "nodeid": result.nodeid,
                "longrepr": result.longrepr,
                "duration_seconds": result.duration_seconds,
                "existing_issue": existing.to_dict() if existing else None,
                "flake_count": flake_count,
                "is_likely_flaky": is_likely_flaky,
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
    from ..execution.e2e_issue_tracker_adapter import GitHubE2EIssueTracker

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

        # Get the GitHub client via the public http_client property
        github_client = _orchestrator.repository_host.http_client  # type: ignore[union-attr]
        tracker = GitHubE2EIssueTracker(github_client)

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
    from ..execution.e2e_issue_tracker_adapter import GitHubE2EIssueTracker

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
        github_client = _orchestrator.repository_host.http_client  # type: ignore[union-attr]
        tracker = GitHubE2EIssueTracker(github_client)

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
        """Start the control API server."""
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
