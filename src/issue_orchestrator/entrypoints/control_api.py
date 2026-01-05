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
    last_tick_id = _orchestrator._event_context.tick_id

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


@control_app.post("/api/shutdown")
async def shutdown() -> JSONResponse:
    """Request graceful shutdown of the orchestrator."""
    if _orchestrator is None:
        return JSONResponse({"error": "Orchestrator not initialized"}, status_code=503)

    _orchestrator.request_shutdown()
    return JSONResponse({"status": "shutdown_requested"})


@control_app.get("/api/health")
async def health() -> JSONResponse:
    """Health check endpoint."""
    return JSONResponse({"status": "ok"})


@control_app.post("/control/shutdown")
async def shutdown_control_center() -> JSONResponse:
    """Shutdown the control center server.

    This stops the supervisor/control center process itself.
    Running orchestrators will continue running independently.
    """
    import os
    import signal
    import threading

    def delayed_shutdown():
        import time
        time.sleep(0.5)  # Give time for response to be sent
        os.kill(os.getpid(), signal.SIGTERM)

    threading.Thread(target=delayed_shutdown, daemon=True).start()
    return JSONResponse({"status": "shutting_down"})


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


@control_app.post("/control/orchestrator/start")
async def control_start(request: Request) -> JSONResponse:
    """Start an orchestrator for a repository.

    JSON body:
        repo_root: str - Repository root path
        port: int (optional) - HTTP port (default: 8080)
    """
    from ..infra import supervisor
    from ..infra.repo_lock import AlreadyRunning

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

    port = body.get("port", 8080)
    if not isinstance(port, int) or port < 1 or port > 65535:
        return JSONResponse({"error": "Invalid port"}, status_code=400)

    try:
        info = supervisor.start(repo_root, port=port)
        return JSONResponse({
            "status": "started",
            "pid": info.pid,
            "port": info.http_port,
            "repo_root": str(repo_root),
        })
    except AlreadyRunning as e:
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
    """
    from ..infra import supervisor

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

    force = body.get("force", False)

    stopped = supervisor.stop(repo_root, force=force)
    if stopped:
        return JSONResponse({"status": "stopped", "repo_root": str(repo_root)})
    else:
        return JSONResponse({
            "status": "not_running",
            "repo_root": str(repo_root),
        })


@control_app.get("/control/orchestrator/status")
async def control_status(repo_root: str = Query(...)) -> JSONResponse:
    """Get the status of the orchestrator for a repository.

    Query params:
        repo_root: str - Repository root path
    """
    from ..infra import supervisor

    path = _validate_repo_root(repo_root)
    if path is None:
        return JSONResponse(
            {"error": "Invalid or missing repo_root"},
            status_code=400,
        )

    status_info = supervisor.status(path)
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

    path = _validate_repo_root(repo_root)
    if path is None:
        return JSONResponse(
            {"error": "Invalid or missing repo_root"},
            status_code=400,
        )

    # Try to load config from repo
    config = None
    for name in [".issue-orchestrator.yaml", ".issue-orchestrator.yml"]:
        config_path = path / name
        if config_path.exists():
            try:
                config = Config.load(config_path)
                break
            except Exception:
                pass

    result = run_doctor(config=config, config_path=path / ".issue-orchestrator.yaml")
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


@control_app.get("/control/repos")
async def list_repos_endpoint() -> JSONResponse:
    """List all registered repositories with their status.

    Returns a list of registered repos with their current orchestrator status.
    """
    from ..infra import supervisor
    from ..infra.repo_registry import list_repos

    repos = list_repos()
    result = []

    for repo in repos:
        # Get status for each repo
        path = Path(repo.path)
        status_info = supervisor.status(path) if path.exists() else None

        result.append({
            "path": repo.path,
            "name": repo.name,
            "added_at": repo.added_at,
            "exists": path.exists(),
            "status": status_info.to_dict() if status_info else None,
        })

    return JSONResponse({"repos": result})


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


@control_app.post("/control/repos/validate")
async def validate_repo_config(request: Request) -> JSONResponse:
    """Validate a repository's configuration without starting the orchestrator.

    JSON body:
        repo_root: str - Repository root path

    Returns validation results with errors and warnings.
    """
    from ..infra.config import Config, find_config_file

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

    # Find config file
    config_path = find_config_file(repo_root)
    if not config_path:
        return JSONResponse({
            "valid": False,
            "has_config": False,
            "config_path": None,
            "errors": ["No .issue-orchestrator.yaml found"],
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


@control_app.get("/control/repos/discover")
async def discover_repos_endpoint(
    search_paths: str = Query(
        default="",
        description="Comma-separated paths to search (default: ~/dev, ~/projects, ~/code, ~/repos)",
    ),
    max_depth: int = Query(default=2, description="Max directory depth to search"),
) -> JSONResponse:
    """Discover repositories with .issue-orchestrator.yaml config files.

    Scans common development directories for repos that have orchestrator configs.
    Returns repos not yet registered in the registry.
    """
    import os
    from ..infra.repo_registry import load_registry

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
    config_names = [".issue-orchestrator.yaml", ".issue-orchestrator.yml"]

    def scan_directory(base: Path, depth: int) -> None:
        if depth > max_depth:
            return
        if not base.exists() or not base.is_dir():
            return

        try:
            for entry in os.scandir(base):
                if entry.is_dir() and not entry.name.startswith("."):
                    entry_path = Path(entry.path)

                    # Check if this directory has a config file
                    for config_name in config_names:
                        config_path = entry_path / config_name
                        if config_path.exists():
                            # Skip git worktrees (.git is a file, not a directory)
                            git_path = entry_path / ".git"
                            if git_path.exists() and git_path.is_file():
                                # This is a worktree, skip it
                                break

                            resolved = str(entry_path.resolve())
                            if resolved not in registered_paths:
                                discovered.append({
                                    "path": resolved,
                                    "name": entry_path.name,
                                    "config": config_name,
                                })
                            break
                    else:
                        # No config found, recurse deeper
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

    checks = {}

    # git
    try:
        result = subprocess.run(
            ["git", "--version"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        checks["git"] = {
            "ok": result.returncode == 0,
            "version": result.stdout.strip() if result.returncode == 0 else None,
        }
    except (subprocess.TimeoutExpired, FileNotFoundError):
        checks["git"] = {"ok": False, "version": None}

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
async def setup_detect(repo_root: str = Query(...)) -> JSONResponse:
    """Detect repository state for setup wizard.

    Query params:
        repo_root: str - Repository root path

    Returns detected repo info, existing config, GitHub labels, etc.
    """
    import subprocess

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
    try:
        git_result = subprocess.run(
            ["git", "remote", "get-url", "origin"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=path,
        )
        if git_result.returncode == 0:
            url = git_result.stdout.strip()
            if "github.com" in url:
                if url.startswith("git@"):
                    parts = url.split(":")[-1]
                else:
                    parts = "/".join(url.split("/")[-2:])
                result["repo"] = parts.removesuffix(".git")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

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
    files_to_create.append({
        "path": f"{repo_root}/.issue-orchestrator.yaml",
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
async def setup_save(request: Request) -> JSONResponse:
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

    # Write config file
    config_path = repo_root / ".issue-orchestrator.yaml"

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
    if create_labels and config.get("repo"):
        try:
            from ..execution.providers import create_repository_host
            host = create_repository_host(repo=config["repo"])

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
