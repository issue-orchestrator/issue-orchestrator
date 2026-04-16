"""Web dashboard for the orchestrator."""

import asyncio
import contextlib
import json
import logging
import os
import platform
import signal
import socket
import subprocess
import time
import webbrowser
from collections.abc import Iterator
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Awaitable, Callable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from sse_starlette.sse import EventSourceResponse

from ..contracts.ui_openapi_models import (
    BlockedIssuesDialogPayload,
    ConfigDialogPayload,
    DebugDialogPayload,
    DoctorDialogPayload,
    InfoDialogPayload,
    PhaseDialogPayload,
    SessionDiagnosticsDialogPayload,
    ValidationFailureDialogPayload,
)
from ..control.label_manager import LabelManager
from ..control.queue_cache import QueueCache, QueueMutationStatus, clear_issue_refresh, record_issue_refreshes
from ..control.shutdown_manager import shutdown_manager
from ..events import EventName
from ..execution.client_host import ClientHost, detect_client_host
from ..execution.label_ops import LabelOperation, apply_label_operations
from ..execution.manifest_accessor import ArtifactNotFoundError, ManifestAccessor, RunIdentity
from ..history import latest_history_entries_by_issue
from ..infra.terminal_cleaning import extract_stream_json_text
from ..ports.event_sink import make_trace_event
from ..view_models.dialogs import (
    build_blocked_issues_dialog,
    build_config_dialog,
    build_debug_dialog,
    build_doctor_dialog,
    build_info_dialog,
    build_phase_dialog,
    build_session_diagnostics_dialog,
    build_validation_failure_dialog,
)
from .timeline_presentation import (
    _decorate_timeline_events,
    _is_agent_scoped_event,
    _timeline_event_default_actions,
    _timeline_event_actions,
    _timeline_event_recommended_actions,
    _timeline_event_requires_run_dir,
)
from .web_issue_detail_routes import web_issue_detail_router
from .web_read_model_routes import web_read_model_router
from .web_refresh_routes import web_refresh_router
from .web_settings_routes import web_settings_router
from .web_session_context import (
    install_web_session_context_dependencies,
    resolve_issue_session_context,
)
from .web_session_routes import (
    build_ui_log_stream_observation as _build_ui_log_stream_observation,
    get_session_manifest,
    get_session_phases,
    preview_lines_from_claude_jsonl as _preview_lines_from_claude_jsonl,
    preview_lines_from_terminal_recording as _preview_lines_from_terminal_recording,
    serve_terminal_recording,
    web_session_router,
)
from .web_status_routes import web_status_router

if TYPE_CHECKING:
    from ..infra.orchestrator import Orchestrator

logger = logging.getLogger(__name__)
_COMPAT_EXPORTS = (
    _decorate_timeline_events,
    _is_agent_scoped_event,
    _timeline_event_default_actions,
    _timeline_event_actions,
    _timeline_event_recommended_actions,
    _timeline_event_requires_run_dir,
)
__all__ = ("_resolve_issue_session_context", "serve_terminal_recording")


# Create FastAPI app
app = FastAPI(title="Issue Orchestrator")

# Static directory (sibling to templates directory)
STATIC_DIR = Path(__file__).parent.parent / "static"

# Mount static files with proper security (prevents path traversal, uses async streaming)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


if os.environ.get("IO_DEV"):

    @app.middleware("http")
    async def no_cache_static(request: Request, call_next):
        """Prevent browser from caching static assets (CSS/JS).

        Without this, the dashboard iframe in the control center serves stale
        JS/CSS even after a hard-refresh of the parent page.
        Only active when IO_DEV=1 is set.
        """
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        return response

# Global reference to orchestrator (set at startup)
_orchestrator: "Orchestrator | None" = None
# Global reference to uvicorn server (for shutdown)
_server: "Any" = None
_client_host: ClientHost = detect_client_host()

# SSE event subscribers - set of asyncio.Queue objects
_event_subscribers: set[asyncio.Queue] = set()

# Main event loop reference for thread-safe event broadcasting
# Set at startup so worker threads can schedule SSE broadcasts
_main_loop: asyncio.AbstractEventLoop | None = None


async def broadcast_event(event_type: str, data: dict | None = None) -> None:
    """Broadcast an event to all SSE subscribers.

    Args:
        event_type: Type of event (e.g., "session_started", "session_completed", "state_changed")
        data: Optional data to include with the event
    """
    event = {"type": event_type, "data": data or {}}
    dead_subscribers = []

    for queue in _event_subscribers:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            # Queue full, mark for removal
            dead_subscribers.append(queue)

    # Clean up dead subscribers
    for queue in dead_subscribers:
        _event_subscribers.discard(queue)


def add_event_subscriber(queue: asyncio.Queue) -> None:
    """Register an SSE subscriber queue."""
    _event_subscribers.add(queue)


def remove_event_subscriber(queue: asyncio.Queue) -> None:
    """Remove an SSE subscriber queue."""
    _event_subscribers.discard(queue)


def event_subscribers_snapshot() -> set[asyncio.Queue]:
    """Return a snapshot of current SSE subscribers."""
    return set(_event_subscribers)


def get_main_loop() -> asyncio.AbstractEventLoop | None:
    """Return the main event loop reference for SSE scheduling."""
    return _main_loop


@contextlib.contextmanager
def swapped_event_subscribers(subscribers: set[asyncio.Queue]) -> Iterator[None]:
    """Temporarily replace the SSE subscriber set (for tests)."""
    global _event_subscribers
    original = _event_subscribers
    _event_subscribers = subscribers
    try:
        yield
    finally:
        _event_subscribers = original


def get_orchestrator():
    """Get the orchestrator instance. Override in tests via app.dependency_overrides."""
    return _orchestrator


def _resolve_issue_session_context(issue_number: int):
    """Compatibility wrapper for callers still importing from ``web``."""
    return resolve_issue_session_context(get_orchestrator(), issue_number)


install_web_session_context_dependencies(app, get_orchestrator=get_orchestrator)
app.include_router(web_read_model_router)
app.include_router(web_status_router)
app.include_router(web_refresh_router)
app.include_router(web_settings_router)
app.include_router(web_session_router)
app.include_router(web_issue_detail_router)


def set_orchestrator(orchestrator) -> None:
    """Set the orchestrator instance. Used by tests and application startup."""
    global _orchestrator
    _orchestrator = orchestrator


def set_client_host(client_host: ClientHost) -> None:
    """Set the client-host adapter. Used by tests."""
    global _client_host
    _client_host = client_host


def trigger_server_shutdown():
    """Trigger uvicorn server shutdown."""
    global _server
    if _server:
        _server.should_exit = True
        _server.force_exit = True  # Don't wait for graceful shutdown


def set_server(server) -> None:
    """Set the server instance. Used by tests and application startup."""
    global _server
    _server = server


def _response_json(response: JSONResponse) -> dict:
    body = response.body
    if isinstance(body, memoryview):
        body = body.tobytes()
    return json.loads(body.decode("utf-8"))


@app.get("/favicon.ico")
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


@app.get("/api/dialog/info", response_model=InfoDialogPayload)
async def get_info_dialog() -> InfoDialogPayload | JSONResponse:
    """Get view model for the About dialog."""
    response = await get_info()
    if response.status_code != 200:
        return response
    payload = _response_json(response)
    return InfoDialogPayload.model_validate(build_info_dialog(payload))


@app.get("/api/dialog/config", response_model=ConfigDialogPayload)
async def get_config_dialog() -> ConfigDialogPayload | JSONResponse:
    """Get view model for the configuration dialog."""
    response = await get_config()
    if response.status_code != 200:
        return response
    payload = _response_json(response)
    return ConfigDialogPayload.model_validate(build_config_dialog(payload.get("config", "")))


@app.get("/api/dialog/debug", response_model=DebugDialogPayload)
async def get_debug_dialog() -> DebugDialogPayload | JSONResponse:
    """Get view model for the debug dialog."""
    response = await get_debug()
    if response.status_code != 200:
        return response
    payload = _response_json(response)
    return DebugDialogPayload.model_validate(build_debug_dialog(payload))


@app.get("/api/dialog/doctor", response_model=DoctorDialogPayload)
async def get_doctor_dialog() -> DoctorDialogPayload | JSONResponse:
    """Get view model for the doctor dialog."""
    response = await get_doctor()
    if response.status_code != 200:
        return response
    payload = _response_json(response)
    return DoctorDialogPayload.model_validate(build_doctor_dialog(payload))


@app.get("/api/dialog/session-diagnostics/{issue_number}", response_model=SessionDiagnosticsDialogPayload)
async def get_session_diagnostics_dialog(
    issue_number: int,
    run_dir: str | None = None,
) -> SessionDiagnosticsDialogPayload | JSONResponse:
    """Get view model for session diagnostics dialog."""
    response = await get_session_manifest(
        issue_number,
        orchestrator=get_orchestrator(),
        run_dir=run_dir,
    )
    if response.status_code != 200:
        return response
    payload = _response_json(response)
    return SessionDiagnosticsDialogPayload.model_validate(build_session_diagnostics_dialog(issue_number, payload))


@app.get("/api/dialog/validation-failure/{issue_number}", response_model=ValidationFailureDialogPayload)
async def get_validation_failure_dialog(
    issue_number: int,
    run_dir: str | None = None,
) -> ValidationFailureDialogPayload | JSONResponse:
    """Get a focused dialog for a failed validation run."""
    response = await get_session_manifest(
        issue_number,
        orchestrator=get_orchestrator(),
        run_dir=run_dir,
    )
    if response.status_code != 200:
        return response
    payload = _response_json(response)
    validation = payload.get("validation_failure")
    if not isinstance(validation, dict):
        return JSONResponse({"error": "No validation failure details found"}, status_code=404)
    return ValidationFailureDialogPayload.model_validate(build_validation_failure_dialog(issue_number, payload))


@app.get("/api/dialog/blocked-issues", response_model=BlockedIssuesDialogPayload)
async def get_blocked_issues_dialog() -> BlockedIssuesDialogPayload | JSONResponse:
    """Get view model for blocked issues dialog."""
    response = await get_blocked_issues()
    if response.status_code != 200:
        return response
    payload = _response_json(response)
    return BlockedIssuesDialogPayload.model_validate(build_blocked_issues_dialog(payload))


@app.get("/api/dialog/phase/{issue_number}", response_model=PhaseDialogPayload)
async def get_phase_dialog(issue_number: int, phase: str | None = None) -> PhaseDialogPayload | JSONResponse:
    """Get view model for phase details dialog."""
    response = await get_session_phases(issue_number, orchestrator=get_orchestrator())
    if response.status_code != 200:
        return response
    payload = _response_json(response)
    return PhaseDialogPayload.model_validate(build_phase_dialog(payload, issue_number, phase))


def _label_manager_for_api() -> LabelManager:
    deps_lm = getattr(getattr(_orchestrator, "deps", None), "label_manager", None)
    if isinstance(deps_lm, LabelManager):
        return deps_lm
    assert _orchestrator is not None
    return LabelManager(_orchestrator.config)


def _terminate_issue_and_hold(issue_number: int, sessions: list[Any]) -> dict[str, Any]:
    """Terminate running sessions and apply a hold guard to prevent auto-requeue."""
    assert _orchestrator is not None
    from datetime import datetime
    from ..domain.models import SessionHistoryEntry

    state = _orchestrator.state
    repo = _orchestrator.repository_host
    lm = _label_manager_for_api()

    killed_sessions: list[str] = []
    pr_numbers = sorted(
        {
            int(s.pr_number)
            for s in sessions
            if getattr(s, "pr_number", None) is not None
        }
    )

    errors = _terminate_sessions(sessions=sessions, killed_sessions=killed_sessions)
    _prune_issue_runtime_state(state=state, issue_number=issue_number)
    _append_operator_termination_history(
        state=state,
        issue_number=issue_number,
        primary_session=sessions[0],
        session_entry_cls=SessionHistoryEntry,
        now=datetime.now(),
    )
    state.failed_this_cycle.add(issue_number)

    # Label policy:
    # - issue: add blocked-failed guard, remove in-progress/pr-pending
    # - linked PR(s): add blocked-failed and remove needs-rework (scanner trigger)
    label_ops: list[LabelOperation] = [
        LabelOperation("add", issue_number, lm.blocked_failed),
        LabelOperation("remove", issue_number, lm.in_progress),
        LabelOperation("remove", issue_number, lm.pr_pending),
    ]
    for pr_number in pr_numbers:
        label_ops.extend(
            [
                LabelOperation("add", pr_number, lm.blocked_failed),
                LabelOperation("remove", pr_number, lm.needs_rework),
            ]
        )
    apply_label_operations(
        repo,
        label_ops,
        logger=logger,
        log_prefix="[terminate]",
    )

    return {
        "killed_sessions": killed_sessions,
        "errors": errors,
        "hold_label": lm.blocked_failed,
    }


def _terminate_sessions(*, sessions: list[Any], killed_sessions: list[str]) -> list[str]:
    assert _orchestrator is not None
    errors: list[str] = []
    for session in sessions:
        try:
            _orchestrator.kill_session(session.terminal_id)
            killed_sessions.append(session.terminal_id)
        except Exception as exc:
            errors.append(f"{session.terminal_id}: {exc}")
    return errors


def _prune_issue_runtime_state(*, state: Any, issue_number: int) -> None:
    state.active_sessions = [s for s in state.active_sessions if s.issue.number != issue_number]
    state.pending_reviews = [r for r in state.pending_reviews if r.issue_number != issue_number]
    state.pending_reworks = [r for r in state.pending_reworks if r.resolve_issue_number() != issue_number]
    state.pending_triage_reviews = [r for r in state.pending_triage_reviews if r.issue_number != issue_number]
    state.pending_validation_retries = [r for r in state.pending_validation_retries if r.issue_number != issue_number]
    state.discovered_reviews = [r for r in state.discovered_reviews if r.issue_number != issue_number]
    state.discovered_reworks = [r for r in state.discovered_reworks if r.issue_number != issue_number]
    state.discovered_failures = [r for r in state.discovered_failures if r.issue_number != issue_number]
    state.immediate_cleanups = [c for c in state.immediate_cleanups if c.issue_number != issue_number]


def _resolve_agent_label(primary_session: Any) -> str:
    agent_label = primary_session.agent_label
    if agent_label:
        return str(agent_label)
    for label in primary_session.issue.labels:
        if label.startswith("agent:"):
            return label
    return "agent:unknown"


def _append_operator_termination_history(
    *,
    state: Any,
    issue_number: int,
    primary_session: Any,
    session_entry_cls: Any,
    now: Any,
) -> None:
    state.session_history.append(
        session_entry_cls(
            issue_number=issue_number,
            title=primary_session.issue.title,
            agent_type=_resolve_agent_label(primary_session),
            status="blocked",
            runtime_minutes=primary_session.runtime_minutes,
            status_reason="Terminated by operator",
            worktree_path=primary_session.worktree_path,
            completed_at=now,
        )
    )


def _queue_related_pr_numbers(state: Any, issue_number: int) -> list[int]:
    pr_numbers = {
        int(review.pr_number)
        for review in state.pending_reviews
        if review.issue_number == issue_number
    }
    pr_numbers.update(
        int(rework.pr_number)
        for rework in state.pending_reworks
        if rework.resolve_issue_number() == issue_number and rework.pr_number is not None
    )
    return sorted(pr_numbers)


def _hold_queued_issue(issue_number: int) -> dict[str, Any]:
    """Place a queued issue on hold and remove it from launchable runtime state."""
    assert _orchestrator is not None
    from ..control.queue_cache import QueueCache
    from ..domain.models import SessionHistoryEntry

    state = _orchestrator.state
    repo = _orchestrator.repository_host
    lm = _orchestrator.deps.label_manager

    issue = next((candidate for candidate in state.cached_queue_issues if candidate.number == issue_number), None)
    if issue is None:
        raise LookupError(f"Issue #{issue_number} not found in queued state")

    pr_numbers = _queue_related_pr_numbers(state, issue_number)
    label_ops: list[LabelOperation] = [
        LabelOperation("add", issue_number, lm.blocked_failed),
        LabelOperation("remove", issue_number, lm.in_progress),
        LabelOperation("remove", issue_number, lm.pr_pending),
    ]
    for pr_number in pr_numbers:
        label_ops.extend(
            [
                LabelOperation("add", pr_number, lm.blocked_failed),
                LabelOperation("remove", pr_number, lm.code_review),
                LabelOperation("remove", pr_number, lm.needs_rework),
            ]
        )
    apply_label_operations(
        repo,
        label_ops,
        logger=logger,
        log_prefix="[cancel-queued]",
    )

    QueueCache(_orchestrator.config, state).remove_issue(issue_number)
    _prune_issue_runtime_state(state=state, issue_number=issue_number)
    state.session_history.append(
        SessionHistoryEntry(
            issue_number=issue_number,
            title=issue.title,
            agent_type=issue.agent_type or "agent:unknown",
            status="blocked",
            runtime_minutes=0,
            status_reason="Cancelled from queue by operator",
            worktree_path=None,
            completed_at=datetime.now(),
        )
    )
    state.failed_this_cycle.add(issue_number)
    return {
        "issue_number": issue_number,
        "title": issue.title,
        "hold_label": lm.blocked_failed,
        "linked_pr_numbers": pr_numbers,
    }


@app.post("/api/kill/{issue_number}")
async def kill_session(issue_number: int) -> JSONResponse:
    """Force terminate an issue session and prevent automatic relaunch."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    sessions = [s for s in _orchestrator.state.active_sessions if s.issue.number == issue_number]
    if not sessions:
        return JSONResponse({"error": f"Session #{issue_number} not found"}, status_code=404)

    terminated = _terminate_issue_and_hold(issue_number, sessions)
    if not terminated["killed_sessions"]:
        return JSONResponse(
            {"error": "Failed to terminate session(s)", "details": terminated["errors"]},
            status_code=500,
        )

    return JSONResponse({
        "status": "terminated",
        "issue_number": issue_number,
        "title": sessions[0].issue.title,
        "killed_sessions": terminated["killed_sessions"],
        "hold_label": terminated["hold_label"],
        "errors": terminated["errors"],
    })


@app.post("/api/focus/{issue_number}")
async def focus_session(issue_number: int) -> JSONResponse:
    """Focus the terminal session for a specific issue."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    # Find the session
    session = None
    for s in _orchestrator.state.active_sessions:
        if s.issue.number == issue_number:
            session = s
            break

    if not session:
        return JSONResponse({"error": f"Session #{issue_number} not found"}, status_code=404)

    # Use session_runner protocol to focus the terminal session
    if _orchestrator.session_runner.focus_session(issue_number, session.terminal_id):
        return JSONResponse({"status": "focused", "issue_number": issue_number})
    else:
        return JSONResponse({"error": f"Could not focus session #{issue_number}"}, status_code=500)


async def _reveal_worktree(issue_number: int) -> JSONResponse:
    """Reveal the worktree path in the current client host for a specific session."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    # Find the session
    session = None
    for s in _orchestrator.state.active_sessions:
        if s.issue.number == issue_number:
            session = s
            break

    if not session:
        return JSONResponse({"error": f"Session #{issue_number} not found"}, status_code=404)

    worktree_path = session.worktree_path
    if not worktree_path.exists():
        return JSONResponse({"error": f"Worktree not found: {worktree_path}"}, status_code=404)

    try:
        result = _client_host.reveal_worktree(worktree_path)
    except subprocess.CalledProcessError as e:
        return JSONResponse({"error": f"Failed to open worktree: {e}"}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)

    status_code = 200 if result.action == "opened" else 409
    return JSONResponse({"issue_number": issue_number, **result.to_dict()}, status_code=status_code)


@app.post("/api/host/reveal-worktree/{issue_number}")
async def reveal_worktree(issue_number: int) -> JSONResponse:
    """Reveal the worktree path in the current client host."""
    return await _reveal_worktree(issue_number)


@app.post("/api/finder/{issue_number}")
async def open_in_finder(issue_number: int) -> JSONResponse:
    """Deprecated alias for revealing a worktree path in the current client host."""
    return await _reveal_worktree(issue_number)


@app.get("/api/log/{issue_number}")
async def get_session_log(issue_number: int) -> JSONResponse:  # noqa: C901 - log retrieval with multiple fallback paths
    """Get Claude session log for an issue.

    Finds the most recent session log from ~/.claude/projects/<worktree-path>/
    """
    from pathlib import Path

    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    context = _resolve_issue_session_context(issue_number)
    worktree_path = context.worktree_path

    if not worktree_path:
        return JSONResponse({
            "error": f"No worktree path found for issue #{issue_number}",
            "hint": "Session may have been cleaned up or never started"
        }, status_code=404)

    # Convert path to Claude's escaped format
    # /path/to/worktree -> -path-to-worktree
    path_str = str(worktree_path)
    escaped_path = path_str.replace("/", "-")
    if not escaped_path.startswith("-"):
        escaped_path = "-" + escaped_path

    # Find session logs
    claude_projects = Path.home() / ".claude" / "projects" / escaped_path
    if not claude_projects.exists():
        return JSONResponse({
            "error": f"Claude project directory not found",
            "path": str(claude_projects),
            "hint": "Session may not have been started yet"
        }, status_code=404)

    # Find most recent .jsonl file
    jsonl_files = sorted(claude_projects.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not jsonl_files:
        return JSONResponse({
            "error": "No session logs found",
            "path": str(claude_projects)
        }, status_code=404)

    latest_log = jsonl_files[0]

    # Read log content (limit to last 100 lines for large logs)
    try:
        lines = latest_log.read_text().strip().split("\n")
        total_lines = len(lines)

        # Return last 100 lines max
        if total_lines > 100:
            lines = lines[-100:]
            truncated = True
        else:
            truncated = False

        return JSONResponse({
            "issue_number": issue_number,
            "log_path": str(latest_log),
            "total_lines": total_lines,
            "truncated": truncated,
            "lines": lines
        })
    except Exception as e:
        return JSONResponse({"error": f"Failed to read log: {e}"}, status_code=500)


@app.get("/api/log/local/{issue_number}")
async def get_agent_ui_log(  # noqa: C901, PLR0912 - log parsing with format detection and streaming
    issue_number: int, offset: int = 0, limit: int = 200, run_dir: str | None = None
) -> JSONResponse:
    """Get the local agent UI log for an issue.

    This serves a preview transcript derived from the canonical raw terminal
    recording for a run.

    Args:
        issue_number: Issue number to get log for
        offset: Line number to start from (for efficient polling). 0 = from beginning.
        limit: Maximum lines to return (default 200, 0 = no limit)
    """
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    if not run_dir:
        return JSONResponse({
            "error": "run_dir is required",
            "hint": "Open logs from a run-scoped timeline action.",
        }, status_code=400)

    run_identity = RunIdentity(issue_number=issue_number, run_dir=Path(run_dir))
    accessor = ManifestAccessor(run_identity)
    stream_observation = _build_ui_log_stream_observation(run_identity.run_dir, resolved_log_path=None)
    try:
        artifact = accessor.get_agent_log(allow_empty=True)
    except ArtifactNotFoundError as e:
        return JSONResponse({
            "error": "No agent log found",
            "hint": "Session may not have started or its run-scoped log was not attached",
            "diagnostic": {
                "run_dir": str(run_identity.run_dir),
                "detail": str(e),
            },
            "stream_observation": stream_observation,
        }, status_code=404)
    log_path = artifact.path
    stream_observation = _build_ui_log_stream_observation(run_identity.run_dir, resolved_log_path=log_path)

    try:
        if artifact.descriptor.content_type == "application/x-ndjson":
            all_lines = _preview_lines_from_terminal_recording(log_path)
        else:
            all_lines = _preview_lines_from_claude_jsonl(log_path)

        # Some recordings contain stream-json payloads; decode those into plain text.
        stream_json_lines = extract_stream_json_text(all_lines)
        if stream_json_lines is not None:
            all_lines = stream_json_lines

        # The file is already cleaned at write-time by SessionOutput/CleaningLogWriter.
        all_lines = [line for line in all_lines if line.strip()]
        total_lines = len(all_lines)

        # If offset specified, return lines from that point
        if offset > 0:
            lines = all_lines[offset:]
        else:
            lines = all_lines

        # Apply limit (0 = no limit for live tailing)
        truncated = False
        if limit > 0 and len(lines) > limit:
            if offset == 0:
                lines = lines[-limit:]
                truncated = True
            else:
                lines = lines[:limit]

        return JSONResponse({
            "issue_number": issue_number,
            "log_path": str(log_path),
            "total_lines": total_lines,
            "offset": offset,
            "truncated": truncated,
            "lines": lines,
            "stream_observation": stream_observation,
        })
    except Exception as e:
        return JSONResponse({"error": f"Failed to read log: {e}"}, status_code=500)


@app.post("/api/prompt/{agent_type}")
async def open_agent_prompt(agent_type: str) -> JSONResponse:
    """Open the agent's prompt file in the default editor."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    full_label = agent_type if agent_type.startswith("agent:") else f"agent:{agent_type}"
    agent_config = _orchestrator.config.agents.get(full_label)
    if not agent_config:
        return JSONResponse({"error": f"Agent type '{agent_type}' not found"}, status_code=404)

    prompt_path = agent_config.prompt_path
    if not prompt_path.is_absolute():
        prompt_path = _orchestrator.config.repo_root / prompt_path
    if not prompt_path.exists():
        return JSONResponse({"error": f"Prompt file not found: {prompt_path}"}, status_code=404)

    if os.uname().sysname == "Darwin":
        subprocess.run(["open", str(prompt_path)])
        return JSONResponse({"status": "opened", "path": str(prompt_path)})
    return JSONResponse({"error": "Open is only available on macOS"}, status_code=400)


def _latest_history_entries(session_history: list[Any], limit: int = 50) -> list[Any]:
    """Return most recent history entries, deduplicated by issue number."""
    return latest_history_entries_by_issue(session_history, limit=limit)


@app.post("/api/shutdown")
async def shutdown(force: bool = False) -> JSONResponse:
    """Request orchestrator shutdown.

    Args:
        force: If True, kill active sessions immediately instead of waiting.
    """
    import threading

    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    _orchestrator.request_shutdown(force=force)
    active_count = len(_orchestrator.state.active_sessions)

    # Request shutdown via centralized manager
    shutdown_manager.request_shutdown(reason="API /api/shutdown")

    # Broadcast shutdown event so any connected dashboards can update their UI
    await broadcast_event("shutdown_requested", {"force": force, "active_sessions": active_count})

    # Trigger uvicorn server shutdown so the process actually exits
    trigger_server_shutdown()

    # Schedule process exit after a minimal delay to allow the response to be sent
    # and SSE event to be delivered.
    # We use threading.Timer because:
    # 1. asyncio tasks might not run if the event loop is blocked
    # 2. BackgroundTasks requires FastAPI's dependency injection which isn't always available
    # 3. Thread pool threads (like startup) can't be cancelled, so we must force exit
    # The 0.2s delay allows the HTTP response and SSE event to be flushed.
    timer = threading.Timer(0.2, shutdown_manager.exit)
    timer.daemon = False  # Don't let the timer be killed when main thread exits
    timer.start()

    return JSONResponse({
        "status": "force_shutdown" if force else "shutdown_requested",
        "active_sessions": active_count,
    })


@app.post("/api/send/{issue_number}")
async def send_input(issue_number: int, request: Request) -> JSONResponse:
    """Send input to a running agent session."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    try:
        payload = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON payload"}, status_code=400)

    text = (payload.get("text") or "").strip()
    if not text:
        return JSONResponse({"error": "Text is required"}, status_code=400)

    session = None
    for s in _orchestrator.state.active_sessions:
        if s.issue.number == issue_number:
            session = s
            break

    if not session:
        return JSONResponse({"error": f"Session #{issue_number} not found"}, status_code=404)

    ok = _orchestrator.session_runner.send_to_session(issue_number, text, session.terminal_id)
    if not ok:
        return JSONResponse({"error": f"Failed to send input to #{issue_number}"}, status_code=500)

    return JSONResponse({"status": "sent", "issue_number": issue_number})


@app.get("/api/dependency-problems")
async def get_dependency_problems() -> JSONResponse:
    """Get current dependency problems for issues.

    Returns a dict mapping issue number to problem details.
    The web UI fetches this on load and then listens for
    dependency.blocked/dependency.unblocked events to stay updated.
    """
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    state = _orchestrator.state
    config = _orchestrator.config

    # Build URL helper
    def make_issue_url(issue_number: int) -> str:
        return f"https://github.com/{config.repo}/issues/{issue_number}" if config.repo else ""

    problems = {}
    for issue_num, problem in state.dependency_problems.items():
        problems[issue_num] = {
            "issue_number": problem.issue_number,
            "issue_title": problem.issue_title,
            "summary": problem.summary,
            "issue_url": make_issue_url(problem.issue_number),
        }

    return JSONResponse({"problems": problems})


@app.get("/api/stale-issues")
async def get_stale_issues() -> JSONResponse:
    """Get issues with stale in-progress labels.

    Returns a dict mapping issue number to stale state details.
    The web UI fetches this on load and then listens for
    stale.in_progress_detected/stale.in_progress_cleared events to stay updated.
    """
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    state = _orchestrator.state
    config = _orchestrator.config
    threshold = config.stale_escalation_ticks

    stale = {}
    for issue_num, ticks in state.stale_issue_ticks.items():
        stale[issue_num] = {
            "issue_number": issue_num,
            "consecutive_ticks": ticks,
            "persistent": threshold > 0 and ticks >= threshold,
            "threshold": threshold,
        }

    return JSONResponse({"stale": stale})


@app.get("/api/info")
async def get_info() -> JSONResponse:
    """Get orchestrator info for the About modal."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    state = _orchestrator.state
    config = _orchestrator.config
    from ..infra.repo_identity import build_repo_identity
    repo_identity = build_repo_identity(config.repo_root)
    commit_sha = repo_identity.commit_sha
    client_capabilities = _client_host.capabilities()

    return JSONResponse({
        "version": "0.1.0",  # TODO: get from package
        "repo": config.repo,
        "repo_root": str(config.repo_root) if config.repo_root else None,
        "ui_mode": config.ui_mode,
        "terminal_backend": config.terminal_adapter or "subprocess",
        "client_capabilities": {
            "focus_session": (config.terminal_adapter or "subprocess") != "subprocess",
            "open_path": client_capabilities.open_path,
            "reveal_worktree": client_capabilities.reveal_worktree,
            "local_server_paths_only": client_capabilities.local_only,
            "host_platform": platform.system().lower(),
        },
        "commit_sha": commit_sha,
        "commit_short": commit_sha[:7] if commit_sha else None,
        "repo_identity": repo_identity.to_dict(),
        "max_sessions": config.max_concurrent_sessions,
        "active_sessions": len(state.active_sessions),
        "completed_today": len(state.completed_today),
    })


@app.get("/api/config")
async def get_config() -> JSONResponse:
    """Get the raw config file contents."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    config = _orchestrator.config

    # Try to read the config file
    config_text = "Config file not found"
    if config.config_path and config.config_path.exists():
        config_text = config.config_path.read_text()

    return JSONResponse({"config": config_text})


@app.get("/api/events")
async def events(request: Request):
    """Server-Sent Events endpoint for real-time updates.

    The dashboard connects to this endpoint to receive instant notifications
    when sessions start, complete, or state changes.
    """
    async def event_generator():
        queue: asyncio.Queue = asyncio.Queue(maxsize=100)
        _event_subscribers.add(queue)
        logger.info("[SSE] Client connected, %d total subscribers", len(_event_subscribers))

        try:
            while True:
                # Check if client disconnected
                if await request.is_disconnected():
                    break

                try:
                    # Wait for event with timeout (sends keepalive)
                    event = await asyncio.wait_for(queue.get(), timeout=30.0)
                    yield {
                        "event": event["type"],
                        "data": json.dumps(event["data"]),
                    }
                except asyncio.TimeoutError:
                    # Send keepalive comment to prevent connection timeout
                    yield {"comment": "keepalive"}
        finally:
            _event_subscribers.discard(queue)
            logger.info("[SSE] Client disconnected, %d remaining subscribers", len(_event_subscribers))

    return EventSourceResponse(event_generator())


@app.post("/api/test/create")
async def create_test_issues() -> JSONResponse:
    """Create test issues for testing."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    from ..testing.support.test_data import create_test_issues as _create_test_issues

    config = _orchestrator.config
    if not config.repo:
        return JSONResponse({"error": "No repo configured"}, status_code=400)
    try:
        urls = _create_test_issues(config.repo, list(config.agents.keys()))
        # Set filtering.label so orchestrator picks them up
        config.filtering.label = "test-data"
        return JSONResponse({"created": urls})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/test/cleanup")
async def cleanup_test_issues() -> JSONResponse:
    """Close all test issues and clear their session history."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    from ..testing.support.test_data import cleanup_test_issues as _cleanup_test_issues

    config = _orchestrator.config
    if not config.repo:
        return JSONResponse({"error": "No repo configured"}, status_code=400)
    try:
        count = _cleanup_test_issues(config.repo)
        # Also clear session history for test issues (titles starting with [TEST])
        state = _orchestrator.state
        state.session_history = [
            entry for entry in state.session_history
            if not entry.title.startswith("[TEST]")
        ]
        return JSONResponse({"closed": count})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/history")
async def get_history() -> JSONResponse:
    """Get session history entries for completed sessions."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    entries = []
    for entry in _latest_history_entries(_orchestrator.state.session_history):
        entries.append({
            "issue_number": entry.issue_number,
            "title": entry.title,
            "agent_type": entry.agent_type,
            "status": entry.status,
            "runtime_minutes": entry.runtime_minutes,
            "pr_url": entry.pr_url,
            "status_reason": entry.status_reason,
            "worktree_path": str(entry.worktree_path) if entry.worktree_path else None,
        })

    return JSONResponse({"history": entries, "count": len(entries)})


@app.post("/api/history/clear")
async def clear_history() -> JSONResponse:
    """Clear all session history (completed, failed, etc.)."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    state = _orchestrator.state
    count = len(state.session_history)
    state.session_history = []
    state.completed_today = []
    return JSONResponse({"cleared": count})


@app.post("/api/history/dismiss/{issue_number}")
async def dismiss_history_entry(issue_number: int) -> JSONResponse:
    """Dismiss a single history entry."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    state = _orchestrator.state
    original_len = len(state.session_history)
    state.session_history = [
        entry for entry in state.session_history
        if entry.issue_number != issue_number
    ]
    if issue_number in state.completed_today:
        state.completed_today.remove(issue_number)
    dismissed = original_len - len(state.session_history)
    return JSONResponse({"dismissed": dismissed})


@app.post("/api/retry/{issue_number}")
async def retry_issue(issue_number: int) -> JSONResponse:
    """Remove issue from history so it can be retried."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    state = _orchestrator.state
    # Remove from history so scheduler will pick it up again
    state.session_history = [
        entry for entry in state.session_history
        if entry.issue_number != issue_number
    ]
    if issue_number in state.completed_today:
        state.completed_today.remove(issue_number)
    return JSONResponse({"retrying": issue_number, "message": "Issue will be picked up on next cycle"})


@app.post("/api/issues/{issue_number}/retry-publish")
async def retry_publish_issue(issue_number: int) -> JSONResponse:
    """Retry publish for a publish-failed issue using the latest failed publish job."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    result = _orchestrator.deps.publish_recovery.retry_publish(issue_number, _orchestrator.state)
    if result.status == "rejected":
        return JSONResponse({"error": result.message}, status_code=409)

    return JSONResponse({
        "status": result.status,
        "message": result.message,
        "issue_number": issue_number,
        "job_id": result.job_id,
        "pr_url": result.pr_url,
        "pr_number": result.pr_number,
    })


@app.post("/api/bulk-retry")
async def bulk_retry(request: Request) -> JSONResponse:
    """Re-queue multiple blocked issues for retry."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)
    body = await request.json()
    issue_numbers = body.get("issue_numbers", [])
    state = _orchestrator.state
    retried = []
    for num in issue_numbers:
        state.session_history = [
            entry for entry in state.session_history
            if entry.issue_number != num
        ]
        if num in state.completed_today:
            state.completed_today.remove(num)
        retried.append(num)
    return JSONResponse({"retried": retried})


@app.post("/api/bulk-kill")
async def bulk_kill(request: Request) -> JSONResponse:
    """Terminate sessions and hold issues until explicit retry/unblock."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)
    body = await request.json()
    issue_numbers = body.get("issue_numbers", [])
    terminated: list[int] = []
    failed: list[dict[str, Any]] = []
    for num in issue_numbers:
        sessions = [s for s in _orchestrator.state.active_sessions if s.issue.number == num]
        if not sessions:
            failed.append({"issue_number": num, "error": "Session not found"})
            continue
        result = _terminate_issue_and_hold(num, sessions)
        if result["killed_sessions"]:
            terminated.append(num)
        else:
            failed.append(
                {"issue_number": num, "error": "Failed to terminate", "details": result["errors"]}
            )
    return JSONResponse({"terminated": terminated, "failed": failed})


@app.post("/api/bulk-cancel-queued")
async def bulk_cancel_queued(request: Request) -> JSONResponse:
    """Place queued issues on hold so they are not launched automatically."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    body = await request.json()
    issue_numbers = body.get("issue_numbers", [])
    if not isinstance(issue_numbers, list):
        return JSONResponse({"error": "issue_numbers must be a list"}, status_code=400)

    cancelled: list[int] = []
    failed: list[dict[str, Any]] = []
    for raw_number in issue_numbers:
        try:
            issue_number = int(raw_number)
        except (TypeError, ValueError):
            failed.append({"issue_number": raw_number, "error": "Invalid issue number"})
            continue

        try:
            result = _hold_queued_issue(issue_number)
        except LookupError:
            failed.append({"issue_number": issue_number, "error": "Issue not found in queue"})
            continue
        except Exception as exc:
            failed.append({"issue_number": issue_number, "error": str(exc)})
            continue
        cancelled.append(result["issue_number"])

    return JSONResponse({"cancelled": cancelled, "failed": failed})


@app.post("/api/bulk-deprioritize")
async def bulk_deprioritize(request: Request) -> JSONResponse:
    """Remove issues from the priority queue."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)
    body = await request.json()
    issue_numbers = body.get("issue_numbers", [])
    state = _orchestrator.state
    removed = []
    for num in issue_numbers:
        if num in state.priority_queue:
            state.priority_queue.remove(num)
            removed.append(num)
    return JSONResponse({"deprioritized": removed})


@app.get("/api/blocked-issues")
async def get_blocked_issues() -> JSONResponse:
    """Get all blocked issues with their blocking labels and context.

    Returns detailed information for the "Manage Blocked Issues" modal.
    Includes worktree path and completion status for debug session support.
    """
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    from ..control.worktree_manager import get_worktree_path

    state = _orchestrator.state
    config = _orchestrator.config
    lm = _orchestrator.deps.label_manager

    def make_issue_url(issue_number: int) -> str:
        return f"https://github.com/{config.repo}/issues/{issue_number}" if config.repo else ""

    blocked_issues = []

    # Get blocked issues from cached queue
    if state.startup_status == "complete":
        for issue in state.cached_queue_issues:
            if not issue.is_blocked:
                continue

            blocking_labels = lm.get_blocking(list(issue.labels))
            blocking_label = blocking_labels[0] if blocking_labels else "blocked"
            needs_human = lm.requires_human_any(list(issue.labels))

            # Try to get failure reason from history
            failure_reason = None
            for entry in reversed(state.session_history):
                if entry.issue_number == issue.number:
                    failure_reason = getattr(entry, 'status_reason', None) or entry.status
                    break

            # Get worktree info for debug session support
            worktree_path = get_worktree_path(config, issue.number)
            worktree_exists = worktree_path.exists()
            has_completion = False
            if worktree_exists:
                completion_path = worktree_path / ".issue-orchestrator" / "completion.json"
                has_completion = completion_path.exists()

            blocked_issues.append({
                "issue_number": issue.number,
                "title": issue.title,
                "agent_type": (issue.agent_type or "unknown").replace("agent:", ""),
                "blocking_label": blocking_label,
                "all_blocking_labels": blocking_labels,
                "needs_human": needs_human,
                "failure_reason": failure_reason,
                "issue_url": make_issue_url(issue.number),
                "worktree_path": str(worktree_path) if worktree_exists else None,
                "has_completion": has_completion,
            })

    return JSONResponse({"blocked_issues": blocked_issues})


@app.get("/api/failure-diagnosis/{issue_number}")
async def get_failure_diagnosis(issue_number: int) -> JSONResponse:
    """Get detailed failure diagnosis for an issue.

    Analyzes AI session logs to provide actionable insights about why a session failed.
    Returns the log path so users can open it directly.
    """
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    diagnosis = _orchestrator.get_failure_diagnosis(issue_number)
    return JSONResponse(diagnosis)


@app.post("/api/issues/{issue_number}/audit")
async def force_issue_audit(issue_number: int) -> JSONResponse:
    """Force a fresh session-failure audit for an issue.

    This is an operator-facing alias for the failure diagnosis path. Use it when
    you want an explicit "audit this issue now" action without going through the
    queue-audit tool, which answers a different question.
    """
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    diagnosis = _orchestrator.get_failure_diagnosis(issue_number)
    return JSONResponse(diagnosis)


async def _open_host_path(request: Request) -> JSONResponse:
    """Open a file via the current client-host integration.

    JSON body:
        path: str - Path to the file to open
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    file_path = body.get("path")
    if not file_path:
        return JSONResponse({"error": "path is required"}, status_code=400)

    # Security: only allow opening files in known safe directories
    safe_prefixes = [
        str(Path.home() / ".claude"),
        str(Path.home() / ".issue-orchestrator"),
        "/tmp/",
    ]
    if "/.issue-orchestrator/" not in file_path and not any(file_path.startswith(prefix) for prefix in safe_prefixes):
        return JSONResponse({"error": "Cannot open files outside safe directories"}, status_code=403)

    if not Path(file_path).exists():
        return JSONResponse({"error": "File not found"}, status_code=404)

    try:
        result = _client_host.open_path(Path(file_path))
        status_code = 200 if result.action == "opened" else 409
        return JSONResponse(result.to_dict(), status_code=status_code)
    except subprocess.CalledProcessError as e:
        return JSONResponse({"error": f"Failed to open file: {e}"}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/host/open-path")
async def open_host_path(request: Request) -> JSONResponse:
    """Open a path via the current client-host integration."""
    return await _open_host_path(request)


@app.post("/api/open-file")
async def open_file(request: Request) -> JSONResponse:
    """Deprecated alias for opening a path via the current client-host integration."""
    return await _open_host_path(request)


@app.post("/api/unblock-retry")
async def unblock_and_retry(request: Request) -> JSONResponse:  # noqa: C901 - multi-step unblock with state transitions
    """Remove retry-blocking labels from issues and trigger a refresh.

    JSON body:
        issues: list[int] - Issue numbers to unblock

    Removes all blocking labels and ``pr-pending`` from each issue, clears them from history,
    and triggers a single refresh so they'll be picked up on the next cycle.
    """
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    from ..control.actions import RemoveLabelAction
    from ..control.retry_policy import labels_to_remove_for_retry

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    issue_numbers = body.get("issues", [])
    if not issue_numbers or not isinstance(issue_numbers, list):
        return JSONResponse({"error": "issues must be a non-empty list"}, status_code=400)

    state = _orchestrator.state
    repository_host = _orchestrator.repository_host
    action_applier = _orchestrator.deps.action_applier
    lm = _orchestrator.deps.label_manager

    unblocked = []
    failed = []

    for issue_number in issue_numbers:
        try:
            # Get current labels to find labels that prevent requeue
            current_labels = repository_host.get_issue_labels(issue_number)
            labels_to_remove = labels_to_remove_for_retry(current_labels, lm)

            if labels_to_remove:
                for label in labels_to_remove:
                    action = RemoveLabelAction(
                        issue_number=issue_number,
                        label=label,
                        reason="unblock via web",
                    )
                    result = action_applier.apply(action)
                    if result.success:
                        logger.info("[unblock] Removed label '%s' from issue #%d", label, issue_number)
                    else:
                        logger.warning(
                            "[unblock] Failed to remove label '%s' from #%d: %s",
                            label,
                            issue_number,
                            result.error or "unknown error",
                        )

            # Also remove from history so it's eligible for processing
            state.session_history = [
                entry for entry in state.session_history
                if entry.issue_number != issue_number
            ]
            if issue_number in state.completed_today:
                state.completed_today.remove(issue_number)

            unblocked.append(issue_number)
        except Exception as e:
            logger.error("[unblock] Failed to unblock issue #%d: %s", issue_number, e)
            failed.append({"issue": issue_number, "error": str(e)})

    # Trigger a single refresh so the orchestrator picks up the unblocked issues
    if unblocked:
        _orchestrator.request_refresh()
        logger.info("[unblock] Unblocked %d issues, refresh triggered", len(unblocked))

    return JSONResponse({
        "unblocked": unblocked,
        "failed": failed,
        "refresh_triggered": len(unblocked) > 0,
    })


@app.post("/api/reset-retry")
async def reset_and_retry(request: Request) -> JSONResponse:
    """Reset issues completely and trigger retry.

    JSON body:
        issues: list[int] - Issue numbers to reset
        from_scratch: bool - Force next launch onto a fresh branch from base

    This "nuclear option" cleans up all local and remote state:
    - Deletes local worktrees
    - Deletes remote branches
    - Removes blocking labels
    - Clears from session history

    Issues return to "available" state for a completely fresh retry.
    """
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    from ..control.maintenance import reset_issue

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    issue_numbers = body.get("issues", [])
    from_scratch = bool(body.get("from_scratch", False))
    if not issue_numbers or not isinstance(issue_numbers, list):
        return JSONResponse({"error": "issues must be a non-empty list"}, status_code=400)

    state = _orchestrator.state
    config = _orchestrator.config
    repository_host = _orchestrator.repository_host
    deps = _orchestrator.deps
    lm = deps.label_manager
    queue_cache = QueueCache(config, state)

    reset_results: list[dict] = []
    failed: list[dict] = []
    pending_label = lm.reset_retry_pending
    scratch_pending_label = lm.reset_retry_scratch_pending

    for issue_number in issue_numbers:
        success_payload, failure_payload = _reset_and_retry_issue(
            issue_number=issue_number,
            from_scratch=from_scratch,
            pending_label=pending_label,
            scratch_pending_label=scratch_pending_label,
            repository_host=repository_host,
            queue_cache=queue_cache,
            state=state,
            deps=deps,
            config=config,
            reset_issue_fn=reset_issue,
        )
        if success_payload is not None:
            reset_results.append(success_payload)
            continue
        if failure_payload is not None:
            failed.append(failure_payload)
            continue
        failed.append({"issue": issue_number, "error": "Unknown reset+retry failure"})

    return JSONResponse({
        "reset": reset_results,
        "failed": failed,
        "from_scratch": from_scratch,
        "refresh_triggered": False,
    })


def _reset_and_retry_issue(  # noqa: PLR0913
    *,
    issue_number: int,
    from_scratch: bool,
    pending_label: str,
    scratch_pending_label: str,
    repository_host: Any,
    queue_cache: QueueCache,
    state: Any,
    deps: Any,
    config: Any,
    reset_issue_fn: Callable[..., Any],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    from ..control.actions import AddLabelAction

    try:
        current_labels = repository_host.get_issue_labels(issue_number)
        result = reset_issue_fn(
            issue_number=issue_number,
            config=config,
            worktree_manager=deps.worktree_manager,
            working_copy=deps.working_copy,
            action_applier=deps.action_applier,
            label_manager=deps.label_manager,
            current_labels=current_labels,
            session_history=state.session_history,
            completed_today=state.completed_today,
            label_store=deps.label_store,
            timeline_store=deps.timeline_store if from_scratch else None,
        )
        if not result.success:
            return None, _make_reset_failure(issue_number, result, result.error or "Unknown error")

        pending_labels_to_add = _pending_labels_for_retry(
            from_scratch=from_scratch,
            pending_label=pending_label,
            scratch_pending_label=scratch_pending_label,
        )
        pending_label_error = _apply_reset_retry_pending_labels(
            issue_number=issue_number,
            labels=pending_labels_to_add,
            action_applier=deps.action_applier,
            add_label_action_cls=AddLabelAction,
        )
        if pending_label_error is not None:
            failure = _make_reset_failure(issue_number, result, pending_label_error, from_scratch=from_scratch)
            return None, failure

        enqueue_error = _enqueue_reset_retry_issue(
            issue_number=issue_number,
            repository_host=repository_host,
            queue_cache=queue_cache,
            state=state,
            pending_labels_to_add=pending_labels_to_add,
            from_scratch=from_scratch,
            result=result,
        )
        if enqueue_error is not None:
            return None, enqueue_error

        _emit_reset_retry_unblocked(
            issue_number=issue_number,
            from_scratch=from_scratch,
            pending_label=pending_label,
            pending_labels_to_add=pending_labels_to_add,
            events=deps.events,
        )
        success = _make_reset_success(issue_number, result, from_scratch, pending_label, pending_labels_to_add)
        logger.info(
            "[reset-retry] Reset issue #%d: worktree=%s branch=%s labels=%s pending=%s from_scratch=%s queued_now=true",
            issue_number,
            result.deleted_worktree or "(none)",
            result.deleted_branch or "(none)",
            result.labels_removed or "(none)",
            pending_label,
            from_scratch,
        )
        return success, None
    except Exception as exc:
        logger.error("[reset-retry] Failed to reset issue #%d: %s", issue_number, exc)
        return None, {"issue": issue_number, "error": str(exc)}


def _pending_labels_for_retry(
    *,
    from_scratch: bool,
    pending_label: str,
    scratch_pending_label: str,
) -> list[str]:
    labels = [pending_label]
    if from_scratch:
        labels.append(scratch_pending_label)
    return labels


def _apply_reset_retry_pending_labels(
    *,
    issue_number: int,
    labels: list[str],
    action_applier: Any,
    add_label_action_cls: Any,
) -> str | None:
    for label in labels:
        result = action_applier.apply(
            add_label_action_cls(
                issue_number=issue_number,
                label=label,
                reason="reset+retry requested via web",
            )
        )
        if not result.success:
            return result.error or f"Failed to set {label}"
    return None


def _enqueue_reset_retry_issue(
    *,
    issue_number: int,
    repository_host: Any,
    queue_cache: QueueCache,
    state: Any,
    pending_labels_to_add: list[str],
    from_scratch: bool,
    result: Any,
) -> dict[str, Any] | None:
    refreshed_issue = repository_host.get_issue(issue_number)
    if refreshed_issue is None:
        return _make_reset_failure(issue_number, result, f"Issue #{issue_number} not found after reset")

    outcome = queue_cache.upsert_refreshed_issue(refreshed_issue)
    refreshed_at = time.time()
    if outcome.status == QueueMutationStatus.ACCEPTED:
        record_issue_refreshes(state, {issue_number}, refreshed_at)
        queue_cache.prune_refresh_timestamps()
        if issue_number not in state.priority_queue:
            state.priority_queue.insert(0, issue_number)
        return None

    clear_issue_refresh(state, issue_number)
    return _make_reset_failure(
        issue_number,
        result,
        f"Issue #{issue_number} is not queue-eligible after reset ({outcome.status.value})",
        pending_labels=pending_labels_to_add,
        from_scratch=from_scratch,
    )


def _emit_reset_retry_unblocked(
    *,
    issue_number: int,
    from_scratch: bool,
    pending_label: str,
    pending_labels_to_add: list[str],
    events: Any,
) -> None:
    events.publish(
        make_trace_event(
            EventName.ISSUE_UNBLOCKED,
            {
                "issue_number": issue_number,
                "reason": "reset_retry_requested",
                "source": "web.reset-retry",
                "pending_label": pending_label,
                "pending_labels": pending_labels_to_add,
                "from_scratch": from_scratch,
            },
        )
    )


def _make_reset_success(
    issue_number: int,
    result: Any,
    from_scratch: bool,
    pending_label: str,
    pending_labels_to_add: list[str],
) -> dict[str, Any]:
    return {
        "issue": issue_number,
        "deleted_worktree": result.deleted_worktree,
        "deleted_branch": result.deleted_branch,
        "labels_removed": result.labels_removed,
        "pending_label": pending_label,
        "pending_labels": pending_labels_to_add,
        "from_scratch": from_scratch,
        "queued_now": True,
    }


def _make_reset_failure(
    issue_number: int,
    result: Any,
    error: str,
    *,
    pending_labels: list[str] | None = None,
    from_scratch: bool | None = None,
) -> dict[str, Any]:
    partial: dict[str, Any] = {
        "deleted_worktree": result.deleted_worktree,
        "deleted_branch": result.deleted_branch,
        "labels_removed": result.labels_removed,
    }
    if pending_labels:
        partial["pending_labels"] = pending_labels
    if from_scratch is not None:
        partial["from_scratch"] = from_scratch
    return {
        "issue": issue_number,
        "error": error,
        "partial": partial,
    }


@app.get("/api/debug")
async def get_debug() -> JSONResponse:
    """Get debug info for troubleshooting."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    state = _orchestrator.state
    config = _orchestrator.config

    agents = {}
    for name, agent_cfg in config.agents.items():
        agents[name] = {
            "timeout": agent_cfg.timeout_minutes,
            "command": agent_cfg.command[:50] + "..." if len(agent_cfg.command) > 50 else agent_cfg.command,
        }

    # Startup options based on current config state
    startup_options = {
        "ui_mode": config.ui_mode,
        "web_port": config.web_port,
        "test_mode": config.filtering.label == "test-data",  # Inferred from filter
        "filtering": {
            "label": config.filtering.label,
            "milestone": config.filtering.milestone,
            "milestones": config.filtering.get_milestones(),
        },
        "max_sessions": config.max_concurrent_sessions,
    }

    return JSONResponse({
        "paused": state.paused,
        "config_path": str(config.config_path) if config.config_path else "None",
        "repo_root": str(config.repo_root),
        "priority_queue": state.priority_queue,
        "agents": agents,
        "startup_options": startup_options,
    })


@app.get("/api/doctor")
async def get_doctor() -> JSONResponse:
    """Run diagnostics and return health status."""
    from ..infra.doctor import run_doctor
    from ..execution.command_runner import LocalCommandRunner

    # Get config from running orchestrator if available
    config = _orchestrator.config if _orchestrator else None

    # Run unified doctor
    result = run_doctor(config=config, runner=LocalCommandRunner())

    # Add orchestrator-specific check (only web knows if orchestrator is running)
    if _orchestrator:
        # Insert at position 2 (after auth checks)
        result.checks.insert(2, type(result.checks[0])(
            name="Orchestrator",
            status="ok",
            detail=f"Running, {'paused' if _orchestrator.state.paused else 'active'}",
        ))
    else:
        result.checks.insert(2, type(result.checks[0])(
            name="Orchestrator",
            status="error",
            detail="Not running",
        ))

    return JSONResponse(result.to_dict())


def _is_port_in_use(port: int, host: str = "127.0.0.1") -> bool:
    """Check if a port is already in use by another process.

    Uses SO_REUSEADDR to ignore TIME_WAIT state - a port in TIME_WAIT
    can still be bound by uvicorn (which also uses SO_REUSEADDR).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            s.bind((host, port))
            return False
        except OSError:
            return True


def _kill_process_on_port(port: int, use_sigkill: bool = False) -> bool:
    """Kill any process using the specified port.

    Args:
        port: The port to free
        use_sigkill: If True, use SIGKILL (force); otherwise SIGTERM (graceful)

    Returns True if a process was killed, False otherwise.
    """
    try:
        # Use lsof to find process using the port (macOS/Linux)
        result = subprocess.run(
            ["lsof", "-ti", f":{port}"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0 and result.stdout.strip():
            pids = result.stdout.strip().split("\n")
            sig = signal.SIGKILL if use_sigkill else signal.SIGTERM
            for pid in pids:
                try:
                    os.kill(int(pid), sig)
                    logger.info("[web] Sent %s to process %s on port %d",
                               "SIGKILL" if use_sigkill else "SIGTERM", pid, port)
                except (ProcessLookupError, ValueError):
                    pass
            return True
    except FileNotFoundError:
        # lsof not available
        pass
    return False


def ensure_port_available(port: int, host: str = "127.0.0.1", max_retries: int = 5) -> None:
    """Ensure the specified port is available, killing any existing process if needed.

    This function is designed to handle orchestrator restarts gracefully by
    automatically freeing the port from any stale processes.
    """
    import time

    if not _is_port_in_use(port, host):
        return

    logger.warning("[web] Port %d is already in use, attempting to free it...", port)

    # First try graceful shutdown (SIGTERM)
    _kill_process_on_port(port, use_sigkill=False)
    time.sleep(0.3)

    # Retry loop with escalating force
    for attempt in range(max_retries):
        if not _is_port_in_use(port, host):
            logger.info("[web] Port %d is now available", port)
            return

        # After first attempt, use SIGKILL
        if attempt > 0:
            logger.warning("[web] Port %d still in use, force killing (attempt %d/%d)...",
                          port, attempt + 1, max_retries)
            _kill_process_on_port(port, use_sigkill=True)

        # Exponential backoff: 0.5s, 1s, 2s, 4s, 8s
        wait_time = 0.5 * (2 ** attempt)
        time.sleep(wait_time)

    # Final check
    if not _is_port_in_use(port, host):
        logger.info("[web] Port %d is now available", port)
        return

    # Still in use after all retries - provide helpful error
    raise RuntimeError(
        f"Port {port} is already in use and could not be freed after {max_retries} attempts. "
        f"Try manually: lsof -ti:{port} | xargs kill -9"
    )


def _get_bound_port(server: object) -> int:
    """Read the actual bound port from a started uvicorn server."""
    for s in getattr(server, "servers", []):
        for sock in getattr(s, "sockets", []):
            addr = sock.getsockname()
            if isinstance(addr, tuple) and len(addr) >= 2:
                return addr[1]
    raise RuntimeError("Could not determine bound port from uvicorn server")


async def run_web_dashboard(
    orchestrator: "Orchestrator",
    port: int = 8080,
    open_browser: bool = True,
    on_server_started: Callable[[int], Awaitable[None] | None] | None = None,
) -> None:
    """Run the web dashboard server.

    Args:
        orchestrator: The orchestrator instance
        port: Port to run on (default 8080, 0 = auto-assign free port)
        open_browser: If True, auto-open browser (default True)
    """
    global _orchestrator, _server
    _orchestrator = orchestrator

    # Also set orchestrator for mounted control_app
    from .control_api import set_orchestrator as set_control_orchestrator
    set_control_orchestrator(orchestrator)

    if port != 0:
        # Ensure fixed port is available before starting
        ensure_port_available(port)

    import uvicorn

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",  # Reduce noise, we have our own logging
        timeout_graceful_shutdown=0,  # Exit immediately when shutdown requested
    )
    server = uvicorn.Server(config)
    _server = server  # Store for shutdown access

    async def after_server_started() -> None:
        while not server.started:
            await asyncio.sleep(0.05)
        actual_port = _get_bound_port(server) if port == 0 else port
        if on_server_started is not None:
            result = on_server_started(actual_port)
            if asyncio.iscoroutine(result):
                await result
        if open_browser:
            url = f"http://127.0.0.1:{actual_port}"
            logger.info("[web] Starting uvicorn server on %s", url)
            webbrowser.open(url)
            return
        logger.info("[web] Starting uvicorn server on 127.0.0.1:%d", actual_port)

    asyncio.create_task(after_server_started())

    await server.serve()
    logger.info("[web] Server stopped")


async def run_with_web_dashboard(
    orchestrator: "Orchestrator",
    port: int = 8080,
    open_browser: bool = True,
    on_server_started: Callable[[int], Awaitable[None] | None] | None = None,
) -> None:
    """Run orchestrator with web dashboard.

    The web server starts immediately while startup runs in background.
    The orchestrator loop waits for startup to complete before processing.

    Args:
        orchestrator: The orchestrator instance
        port: Port to run web server on
        open_browser: If True, auto-open browser (default True)
    """
    import time

    # Initialize shutdown manager with repo root for lock cleanup
    if orchestrator.config.repo_root:
        shutdown_manager.initialize(orchestrator.config.repo_root)

    def run_startup_sync():
        """Run startup synchronously in a thread.

        Note: _emit_event calls during startup won't reach SSE subscribers
        because asyncio.Queue is not thread-safe. The startup_complete event
        is emitted after returning to the main event loop.
        """
        asyncio.run(orchestrator.startup())

    async def run_startup_and_loop():
        """Run startup then the orchestrator loop."""
        global _main_loop

        # Wait for server to start and serve initial request before running startup
        await asyncio.sleep(0.5)
        try:
            # Run startup in a thread pool to avoid blocking the event loop
            # startup() makes synchronous GitHub API calls that would block serving requests
            startup_start = time.time()
            logger.info("[web] Starting orchestrator startup in thread pool...")

            loop = asyncio.get_running_loop()
            # Store main loop reference for thread-safe SSE broadcasting
            _main_loop = loop
            await loop.run_in_executor(None, run_startup_sync)

            startup_elapsed = time.time() - startup_start
            logger.info("[web] Startup completed in %.1fs, emitting startup_complete event", startup_elapsed)

            # Emit startup_complete HERE in the main event loop (not from thread)
            # This ensures SSE subscribers receive it properly
            await broadcast_event("startup_complete", {"elapsed_seconds": startup_elapsed})

            await orchestrator.run_loop()
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("[web] Orchestrator task crashed — triggering shutdown")
            orchestrator.shutdown_requested = True
            shutdown_manager.request_shutdown(reason="orchestrator task crashed")
            shutdown_manager.exit()

    import os

    if os.environ.get("ORCHESTRATOR_NO_BROWSER") in {"1", "true", "True"}:
        open_browser = False

    # Start orchestrator (startup + loop) in background
    orchestrator_task = asyncio.create_task(run_startup_and_loop())

    try:
        # Run web server in foreground (available immediately)
        await run_web_dashboard(
            orchestrator,
            port,
            open_browser=open_browser,
            on_server_started=on_server_started,
        )
    finally:
        # When web server stops, stop orchestrator
        logger.info("[web] Shutting down orchestrator...")
        orchestrator.shutdown_requested = True
        orchestrator_task.cancel()
        try:
            # Give the task 2 seconds to clean up, then exit anyway
            # (task may be stuck in synchronous GitHub API calls in thread pool)
            await asyncio.wait_for(orchestrator_task, timeout=2.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            logger.info("[web] Orchestrator task did not exit cleanly, forcing exit")

        # Force exit - thread pool threads (e.g., startup) can't be cancelled
        # and would keep the process alive indefinitely
        logger.info("[web] Shutdown complete, exiting via shutdown_manager")
        shutdown_manager.request_shutdown(reason="web server stopped")
        shutdown_manager.exit()


# Mount control API AFTER all routes are defined so app's routes take precedence
# This allows dashboard JS to fetch /control/... routes from the same port
from .control_api import control_app
app.mount("", control_app)
