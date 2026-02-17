"""Web dashboard for the orchestrator."""

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import socket
import subprocess
import time
import webbrowser
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any, Callable

from fastapi import Depends, FastAPI, Query, Request
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse
from jinja2 import Environment, FileSystemLoader
from pydantic import BaseModel, ConfigDict

from ..history import latest_history_entries_by_issue
from ..infra.e2e_runner import get_e2e_role
from ..view_models.dashboard import (
    build_dashboard_view_model,
    blocked_summary,
    flow_steps_for,
    issue_url_for,
)
from ..view_models.dialogs import (
    build_blocked_issues_dialog,
    build_config_dialog,
    build_debug_dialog,
    build_doctor_dialog,
    build_info_dialog,
    build_phase_dialog,
    build_session_diagnostics_dialog,
)
from ..view_models.issue_detail import IssueStoryContext, build_issue_detail_view_model
from ..contracts.ui_openapi_models import (
    BlockedIssuesDialogPayload,
    ConfigDialogPayload,
    DashboardViewModelPayload,
    DebugDialogPayload,
    DoctorDialogPayload,
    InfoDialogPayload,
    IssueRowsPayload,
    PhaseDialogPayload,
    SessionDiagnosticsDialogPayload,
    IssueDetailPayload,
    IssueRowPayload,
)
from ..control.queue_cache import QueueCache, QueueMutationStatus
from ..execution.manifest_accessor import (
    ArtifactNotFoundError,
    ManifestAccessor,
    RunIdentity,
)
from ..domain.event_taxonomy import (
    EventIntent,
    is_review_oriented_event,
    is_rework_event_name,
    is_review_event_name,
    is_session_event_name,
)
from ..timeline import MIN_SUPPORTED_TIMELINE_SCHEMA_VERSION

if TYPE_CHECKING:
    from ..infra.orchestrator import Orchestrator

logger = logging.getLogger(__name__)


class ViewModelSnapshotPayload(BaseModel):
    """Combined view-model + rendered rows from a single snapshot."""

    model_config = ConfigDict(extra="forbid")
    view_model: DashboardViewModelPayload
    rows: list[IssueRowPayload]
    active_tab: str
    count: int


@dataclass(frozen=True)
class IssueSessionContext:
    """Resolved latest session context for an issue across active/history/storage."""

    worktree_path: Path | None = None
    session_name: str | None = None
    run_dir: Path | None = None


# Pattern to match ANSI escape sequences and control characters:
# - \x1b[...m (SGR - colors, bold, etc.)
# - \x1b[...A/B/C/D/etc (cursor movement)
# - \x1b]...BEL (OSC sequences - terminal titles)
# - \x1b[?...h/l/s/u (private mode set/reset like ?2026h)
# - \x1b[>...u/c (extended key sequences)
# - \x1b[<u (pop key mode)
# - \x1b7, \x1b8 (cursor save/restore without bracket)
_ANSI_ESCAPE_PATTERN = re.compile(
    r"\x1b\[[0-9;]*[a-zA-Z]"  # Standard CSI sequences (colors, cursor, etc.)
    r"|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)"  # OSC sequences (title, etc.) - BEL or ST terminator
    r"|\x1b\[\?[0-9;]*[a-zA-Z]"  # Private mode sequences (?2026h, ?25l, etc.)
    r"|\x1b\[>[0-9;]*[a-zA-Z]"  # Extended sequences (>1u, etc.)
    r"|\x1b\[<[a-zA-Z]"  # Pop sequences (<u)
    r"|\x1b[78]"  # Cursor save/restore (ESC 7, ESC 8)
    r"|\x07"  # Bell character
)


def strip_ansi_codes(text: str) -> str:
    """Strip ANSI escape sequences from text."""
    return _ANSI_ESCAPE_PATTERN.sub("", text)


# Spinner characters used by Claude Code (dots, stars, etc.)
_SPINNER_CHARS = set("·✶✻✽✳✢*/-\\|●○◉◎◯◐◑◒◓⎿")


def clean_terminal_line(line: str) -> str:
    """Clean a terminal log line for display in web UI.

    Handles:
    - ANSI escape sequences (colors, cursor movement)
    - Carriage returns (spinner animations that overwrite lines)
    - Control characters
    """
    # Handle carriage returns: terminal overwrites from start of line
    # Take only the content after the last carriage return
    if "\r" in line:
        # Split by \r and take the last non-empty segment
        segments = line.split("\r")
        # Find last segment with actual content (not just spaces)
        for segment in reversed(segments):
            stripped = strip_ansi_codes(segment).strip()
            if stripped:
                line = segment
                break
        else:
            # All segments empty after stripping, use last one
            line = segments[-1] if segments else ""

    # Strip ANSI escape sequences
    line = strip_ansi_codes(line)

    # Remove other control characters except tab and newline
    line = "".join(c for c in line if c >= " " or c in "\t\n")

    return line


def _is_ui_noise(lower: str) -> bool:
    """Check if line content is repetitive UI noise to filter."""
    # Thinking/loading status lines that repeat during spinner animation
    if "fiddle-faddling" in lower or "thinking" in lower or "running…" in lower:
        return True
    # Partial think-time displays like "ought for 2s)"
    if lower.endswith("s)") and ("ought for" in lower or "hought for" in lower):
        return True
    # Permission bypass hints (UI chrome on every command)
    if "bypass permissions" in lower or "shift+tab to cycle" in lower:
        return True
    return False


def _is_meaningful_short_line(stripped: str) -> bool:
    """Check if a short line (<8 chars) is meaningful content to keep."""
    # Separator lines (───)
    if stripped.startswith(("─", "━")):
        return True
    # Prompt characters
    if stripped in ("❯", ">", "$", "%"):
        return True
    # Tool call prefixes - always meaningful
    if stripped.startswith(("⏺", "⎿")):
        return True
    # Checkmarks/bullets with substantial content
    if stripped.startswith(("✓", "✗", "•")) and len(stripped) > 4:
        return True
    return False


def is_spinner_fragment(line: str) -> bool:
    """Check if a line is a spinner animation fragment to filter out."""
    stripped = line.strip()
    if not stripped:
        return True

    # Lines that are just spinner characters
    if all(c in _SPINNER_CHARS for c in stripped):
        return True

    # Filter repetitive UI noise
    if _is_ui_noise(stripped.lower()):
        return True

    # Short lines are fragments unless they're meaningful UI elements
    if len(stripped) < 8:
        return not _is_meaningful_short_line(stripped)

    return False


def dedupe_consecutive_lines(lines: list[str]) -> list[str]:
    """Remove consecutive duplicate or near-duplicate lines.

    Terminal logs often have repeated separator lines, prompts, etc.
    This collapses them to a single occurrence.
    """
    if not lines:
        return lines

    result = [lines[0]]
    for line in lines[1:]:
        prev = result[-1].strip()
        curr = line.strip()

        # Skip exact duplicates
        if curr == prev:
            continue

        # Skip if both are separator lines
        if prev.startswith("─") and curr.startswith("─"):
            continue

        # Skip if both are just prompts
        if prev in ("❯", ">") and curr in ("❯", ">"):
            continue

        result.append(line)

    return result


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

# Import shutdown manager for centralized exit handling
from ..control.shutdown_manager import shutdown_manager

# SSE event subscribers - set of asyncio.Queue objects
_event_subscribers: set[asyncio.Queue] = set()

# Main event loop reference for thread-safe event broadcasting
# Set at startup so worker threads can schedule SSE broadcasts
_main_loop: asyncio.AbstractEventLoop | None = None
_NOISY_TIMELINE_EVENTS = frozenset({"issue.labels_changed"})
_TIMELINE_ARTIFACT_PATH_TYPES = frozenset({
    "completion_record",
    "run_dir",
    "validation",
    "worktree",
})
_TIMELINE_START_EVENTS = frozenset({"session.started", "review.started", "rework.started"})
_TIMELINE_FAILURE_EVENTS = frozenset({
    "issue.blocked",
    "issue.needs_human",
    "issue.pr_rejected",
    "session.blocked",
    "session.failed",
    "session.timeout",
    "session.validation_failed",
    "review.changes_requested",
    "review.escalated",
})


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


def set_orchestrator(orchestrator) -> None:
    """Set the orchestrator instance. Used by tests and application startup."""
    global _orchestrator
    _orchestrator = orchestrator


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


# Template directory (templates are in parent package, not entrypoints)
TEMPLATE_DIR = Path(__file__).parent.parent / "templates"


def get_templates() -> Environment:
    """Get Jinja2 template environment."""
    return Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))


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


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, orchestrator=Depends(get_orchestrator)) -> HTMLResponse:
    """Render the main dashboard."""
    import time
    request_start = time.time()

    # Get query params
    queue_page = int(request.query_params.get("page", 1))
    if queue_page < 1:
        queue_page = 1
    e2e_page = int(request.query_params.get("e2e_page", 1))
    if e2e_page < 1:
        e2e_page = 1
    active_tab = request.query_params.get("tab", "flow")
    logger.info("[dashboard] Request URL: %s, page=%s, tab=%s", request.url, queue_page, active_tab)

    templates = get_templates()
    template = templates.get_template("dashboard.html")
    view_model = build_dashboard_view_model(
        orchestrator,
        queue_page=queue_page,
        active_tab=active_tab,
        e2e_page=e2e_page,
    )
    html = template.render(**view_model.template_context())
    total_elapsed = time.time() - request_start
    logger.info("[dashboard] Total request time: %.2fs", total_elapsed)
    return HTMLResponse(content=html)


@app.get("/api/view-model", response_model=DashboardViewModelPayload)
async def get_view_model(
    request: Request,
    orchestrator=Depends(get_orchestrator),
) -> DashboardViewModelPayload | JSONResponse:
    """Get the dashboard view model as JSON."""
    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)
    queue_page = int(request.query_params.get("page", 1))
    if queue_page < 1:
        queue_page = 1
    e2e_page = int(request.query_params.get("e2e_page", 1))
    if e2e_page < 1:
        e2e_page = 1
    active_tab = request.query_params.get("tab", "flow")

    view_model = build_dashboard_view_model(
        orchestrator,
        queue_page=queue_page,
        active_tab=active_tab,
        e2e_page=e2e_page,
    )
    return DashboardViewModelPayload.model_validate(view_model.to_dict())


@app.get("/api/view-model-snapshot", response_model=ViewModelSnapshotPayload)
async def get_view_model_snapshot(
    tab: str = Query("flow"),
    page: int = Query(1, ge=1),
    e2e_page: int = Query(1, ge=1),
    orchestrator=Depends(get_orchestrator),
) -> ViewModelSnapshotPayload | JSONResponse:
    """Get view-model and rendered rows from a single snapshot.

    This keeps tab counts and rendered list rows in lockstep for UI refreshes.
    """
    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    queue_page = page
    active_tab = tab

    view_model = build_dashboard_view_model(
        orchestrator,
        queue_page=queue_page,
        active_tab=active_tab,
        e2e_page=e2e_page,
    )

    templates = get_templates()
    row_template = templates.get_template("issue_row.html")
    rows = []
    for issue in view_model.issues:
        html = row_template.render(
            issue=issue,
            active_tab=view_model.active_tab,
            github_owner=view_model.github_owner,
            github_repo=view_model.github_repo,
        )
        rows.append({
            "issue_number": issue.get("issue_number"),
            "html": html,
        })

    return ViewModelSnapshotPayload.model_validate({
        "view_model": view_model.to_dict(),
        "rows": rows,
        "active_tab": view_model.active_tab,
        "count": len(rows),
    })


@app.get("/api/issue-rows", response_model=IssueRowsPayload)
async def get_issue_rows(request: Request, orchestrator=Depends(get_orchestrator)) -> IssueRowsPayload | JSONResponse:
    """Get rendered issue rows for the current view."""
    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    queue_page = int(request.query_params.get("page", 1))
    if queue_page < 1:
        queue_page = 1
    e2e_page = int(request.query_params.get("e2e_page", 1))
    if e2e_page < 1:
        e2e_page = 1
    active_tab = request.query_params.get("tab", "flow")

    view_model = build_dashboard_view_model(
        orchestrator,
        queue_page=queue_page,
        active_tab=active_tab,
        e2e_page=e2e_page,
    )

    templates = get_templates()
    template = templates.get_template("issue_row.html")

    rows = []
    for issue in view_model.issues:
        html = template.render(
            issue=issue,
            active_tab=view_model.active_tab,
            github_owner=view_model.github_owner,
            github_repo=view_model.github_repo,
        )
        rows.append({
            "issue_number": issue.get("issue_number"),
            "html": html,
        })

    return IssueRowsPayload.model_validate({
        "rows": rows,
        "active_tab": view_model.active_tab,
        "count": len(rows),
    })


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
    response = await get_session_manifest(issue_number, run_dir=run_dir)
    if response.status_code != 200:
        return response
    payload = _response_json(response)
    return SessionDiagnosticsDialogPayload.model_validate(build_session_diagnostics_dialog(issue_number, payload))


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
    response = await get_session_phases(issue_number)
    if response.status_code != 200:
        return response
    payload = _response_json(response)
    return PhaseDialogPayload.model_validate(build_phase_dialog(payload, issue_number, phase))


@app.get("/api/status")
async def get_status() -> JSONResponse:
    """Get current orchestrator status as JSON."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    state = _orchestrator.state
    config = _orchestrator.config

    sessions = []
    for session in state.active_sessions:
        sessions.append({
            "issue_number": session.issue.number,
            "title": session.issue.title,
            "runtime_minutes": session.runtime_minutes,
            "agent_type": session.issue.agent_type,
            "status": "running" if session.runtime_minutes < session.agent_config.timeout_minutes else "slow",
            "branch": session.branch_name,
        })

    # Serialize pending reviews
    pending_reviews = []
    for review in state.pending_reviews:
        pending_reviews.append({
            "issue_number": review.issue_number,
            "pr_number": review.pr_number,
            "pr_url": review.pr_url,
            "branch_name": review.branch_name,
        })

    tick_id = _orchestrator.event_context.tick_id
    if not isinstance(tick_id, (int, float)):
        tick_id = None
    last_tick_time = _orchestrator.last_tick_time
    if not isinstance(last_tick_time, (int, float)):
        last_tick_time = None

    # Determine E2E role for this instance
    instance_id = os.environ.get("INSTANCE_ID")
    e2e_role = get_e2e_role(config.e2e, instance_id=instance_id)

    # Collect publish job status
    publish_jobs = []
    try:
        executor = _orchestrator.deps.publish_executor
        for job in executor.get_running_jobs():
            publish_jobs.append({
                "job_id": job.job_id,
                "issue_number": job.issue_number,
                "session_key": job.session_key,
                "status": job.status.value,
                "started_at": job.started_at,
            })
        publish_job_stats = {
            "running": executor.get_running_count(),
            "pending": executor.get_pending_count(),
        }
    except Exception:
        publish_job_stats = {"running": 0, "pending": 0}

    return JSONResponse({
        "paused": state.paused,
        "shutdown_requested": _orchestrator.shutdown_requested,
        "active_sessions": sessions,
        "max_sessions": config.max_concurrent_sessions,
        "completed_today": state.completed_today,
        "queue": state.priority_queue,
        "pending_reviews": pending_reviews,
        "tick_id": tick_id,
        "last_tick_time": last_tick_time,
        "e2e_role": e2e_role if config.e2e.enabled else None,
        "publish_jobs": publish_jobs,
        "publish_job_stats": publish_job_stats,
    })


@app.get("/api/publish-jobs")
async def get_publish_jobs(issue_number: int | None = None) -> JSONResponse:
    """Get publish job history.

    Query params:
        issue_number: Optional filter to a specific issue

    Returns:
        List of recent publish jobs with their status and results.
    """
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    try:
        executor = _orchestrator.deps.publish_executor
        records = executor.get_job_history(issue_number=issue_number, limit=100)

        jobs = []
        for record in records:
            jobs.append({
                "job_id": record.job_id,
                "issue_number": record.issue_number,
                "session_key": record.session_key,
                "worktree_path": record.worktree_path,
                "worktree_id": record.worktree_id,
                "branch_name": record.branch_name,
                "status": record.status,
                "created_at": record.created_at,
                "started_at": record.started_at,
                "finished_at": record.finished_at,
                "pr_url": record.pr_url,
                "pr_number": record.pr_number,
                "error_message": record.error_message,
                "duration_seconds": record.duration_seconds,
            })

        return JSONResponse({"jobs": jobs, "count": len(jobs)})
    except Exception as e:
        return JSONResponse({"error": str(e), "jobs": []}, status_code=500)


@app.get("/api/excluded-issues")
async def get_excluded_issues() -> JSONResponse:
    """Get issues known to the system but excluded from scheduling."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    state = _orchestrator.state
    config = _orchestrator.config

    displayed_numbers = {
        s.issue.number for s in state.active_sessions
    } | {
        i.number for i in state.cached_queue_issues
    } | {
        e.issue_number for e in state.session_history
    }

    from ..infra.audit import audit_queue, SkipReason

    entries = audit_queue(config, state=state, issue_tracker=_orchestrator.repository_host)
    excluded: list[dict[str, object]] = []

    for entry in entries:
        if entry.status == SkipReason.QUEUED:
            continue
        if entry.issue.number in displayed_numbers:
            continue

        dep_problem = state.dependency_problems.get(entry.issue.number)
        if dep_problem:
            reason = f"dependency: {dep_problem.summary}"
        else:
            reason = entry.status.value
            if entry.detail:
                reason = f"{reason}: {entry.detail}"

        flow_stage = "not_eligible"
        excluded.append({
            "issue_number": entry.issue.number,
            "title": entry.issue.title,
            "agent_type": (entry.issue.agent_type or "unknown").replace("agent:", ""),
            "issue_url": issue_url_for(config, entry.issue.number),
            "excluded_reason": reason,
            "flow_stage": flow_stage,
            "flow_steps": flow_steps_for(flow_stage),
            "blocked_summary": blocked_summary(
                list(entry.issue.labels),
                _orchestrator.deps.label_manager,
                dep_problem.summary if dep_problem else None,
            ),
        })

    return JSONResponse({"excluded": excluded})


@app.post("/api/pause")
async def pause() -> JSONResponse:
    """Pause the orchestrator."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)
    _orchestrator.pause()
    return JSONResponse({"status": "paused"})


@app.post("/api/resume")
async def resume() -> JSONResponse:
    """Resume the orchestrator."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)
    _orchestrator.resume()
    return JSONResponse({"status": "resumed"})


@app.post("/api/refresh")
async def refresh(request: Request) -> JSONResponse:
    """Request an immediate refresh of issues from GitHub.

    This triggers the orchestrator to fetch issues on the next loop iteration,
    bypassing the fetch-layer network sync interval. Also resets the timer for
    the next scheduled refresh.

    Optional JSON body:
        inflight_stable_ids: list[str] - Issue IDs that tests expect to discover.
            If provided and these issues are not found after a cached refresh,
            the orchestrator will retry without cache to handle GitHub's
            eventual consistency.
    """
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

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
    return JSONResponse({
        "status": "refresh_requested",
        "refresh": {
            "requested": True,
            "in_progress": bool(_orchestrator.state.queue_refresh_in_progress),
        },
    })


@app.post("/api/refresh/visibility")
async def update_refresh_visibility(request: Request) -> JSONResponse:
    """Store issue visibility hints from Flow UI for visibility-aware refresh.

    JSON body:
        issues: list[int] - Issue numbers currently visible to the user.
    """
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    raw_issues = body.get("issues", [])
    if not isinstance(raw_issues, list):
        return JSONResponse({"error": "issues must be a list"}, status_code=400)

    visible_numbers: list[int] = []
    for value in raw_issues:
        try:
            number = int(value)
        except (TypeError, ValueError):
            continue
        if number > 0:
            visible_numbers.append(number)

    state = _orchestrator.state
    state.ui_visible_issue_numbers = sorted(set(visible_numbers))
    state.ui_visible_updated_at = time.time()
    return JSONResponse({"status": "ok", "count": len(state.ui_visible_issue_numbers)})


@app.post("/api/issues/{issue_number}/refresh")
async def refresh_issue(issue_number: int) -> JSONResponse:
    """Refresh a single issue from GitHub and update cached queue state."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    issue = _orchestrator.repository_host.get_issue(issue_number)
    if issue is None:
        return JSONResponse({"error": f"Issue #{issue_number} not found"}, status_code=404)

    state = _orchestrator.state
    config = _orchestrator.config
    queue_cache = QueueCache(config, state)
    outcome = queue_cache.upsert_refreshed_issue(issue)
    refreshed_at = time.time()
    if outcome.status == QueueMutationStatus.ACCEPTED:
        state.issue_refresh_timestamps[issue_number] = refreshed_at
        state.issue_last_refreshed_at[issue_number] = refreshed_at
    else:
        state.issue_refresh_timestamps.pop(issue_number, None)
        state.issue_last_refreshed_at.pop(issue_number, None)
    queue_cache.prune_refresh_timestamps()

    return JSONResponse({
        "status": "refreshed" if outcome.status == QueueMutationStatus.ACCEPTED else outcome.status.value,
        "issue_number": issue_number,
        "updated": outcome.updated,
        "in_scope": outcome.in_queue,
        "last_refreshed_label": "just now",
        "last_refreshed_age_seconds": 0,
        "is_stale": False,
        "stale_reason": "",
    })


@app.post("/api/kill/{issue_number}")
async def kill_session(issue_number: int) -> JSONResponse:
    """Force kill a specific session."""
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

    # Kill the session
    try:
        _orchestrator.kill_session(session.terminal_id)
    except Exception as e:
        return JSONResponse({"error": f"Failed to kill session: {e}"}, status_code=500)

    # Remove from active sessions
    _orchestrator.state.active_sessions = [
        s for s in _orchestrator.state.active_sessions
        if s.issue.number != issue_number
    ]

    return JSONResponse({
        "status": "killed",
        "issue_number": issue_number,
        "title": session.issue.title,
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


@app.post("/api/finder/{issue_number}")
async def open_in_finder(issue_number: int) -> JSONResponse:
    """Open the worktree folder in Finder for a specific session."""
    import subprocess
    import os

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

    # Open in Finder (macOS only)
    if os.uname().sysname == "Darwin":
        subprocess.run(["open", str(worktree_path)])
        return JSONResponse({"status": "opened", "path": str(worktree_path)})
    else:
        return JSONResponse({"error": "Finder is only available on macOS"}, status_code=400)


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

    This reads .issue-orchestrator/sessions/<session>/session.log from the worktree.

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
    try:
        artifact = accessor.get_agent_log()
    except ArtifactNotFoundError as e:
        return JSONResponse({
            "error": "No agent UI log found",
            "hint": "Session may not have started or logging was not enabled",
            "diagnostic": {
                "run_dir": str(run_identity.run_dir),
                "detail": str(e),
            },
        }, status_code=404)
    log_path = artifact.path

    try:
        all_lines = log_path.read_text(errors="ignore").splitlines()
        total_lines = len(all_lines)

        # If offset specified, return lines from that point
        if offset > 0:
            lines = all_lines[offset:]
        else:
            lines = all_lines

        # Apply limit (0 = no limit for live tailing)
        truncated = False
        if limit > 0 and len(lines) > limit:
            # For initial load (offset=0), take last N lines
            # For polling (offset>0), take first N lines (new content)
            if offset == 0:
                lines = lines[-limit:]
                truncated = True
            else:
                lines = lines[:limit]

        # Clean terminal output: strip ANSI codes, handle carriage returns,
        # filter empty lines and spinner fragments for readable display
        cleaned_lines = []
        for line in lines:
            cleaned = clean_terminal_line(line)
            # Filter empty lines and spinner animation fragments
            if cleaned.strip() and not is_spinner_fragment(cleaned):
                cleaned_lines.append(cleaned)

        # Remove consecutive duplicate lines (repeated separators, prompts, etc.)
        cleaned_lines = dedupe_consecutive_lines(cleaned_lines)

        return JSONResponse({
            "issue_number": issue_number,
            "log_path": str(log_path),
            "total_lines": total_lines,
            "offset": offset,
            "truncated": truncated,
            "lines": cleaned_lines,
        })
    except Exception as e:
        return JSONResponse({"error": f"Failed to read log: {e}"}, status_code=500)


def _manifest_response(
    run_dir: Path,
    session_name: str | None,
) -> JSONResponse:
    """Load RunManifest + analysis from run_dir and return as JSON."""
    from ..domain.run_manifest import RunManifest
    from ..control.session_analyzer import load_analysis

    try:
        manifest = RunManifest.load(run_dir)
    except FileNotFoundError:
        return JSONResponse({
            "run_dir": str(run_dir),
            "session_name": session_name,
            "manifest": None,
        })
    except Exception as e:
        return JSONResponse({"error": f"Failed to read manifest: {e}"}, status_code=500)

    result: dict[str, Any] = {
        "run_dir": str(run_dir),
        "session_name": session_name,
        "manifest": manifest.to_dict(),
    }

    analysis = load_analysis(run_dir)
    if analysis:
        result["analysis"] = {
            "headline": analysis.headline,
            "detail": analysis.detail,
            "suggestions": list(analysis.suggestions),
        }

    return JSONResponse(result)


@app.get("/api/session/manifest/{issue_number}")
async def get_session_manifest(
    issue_number: int,
    run_dir: str | None = None,
) -> JSONResponse:  # noqa: C901, PLR0912 - manifest lookup with multiple path strategies
    """Get the session manifest for an issue."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    requested_run_dir = run_dir
    context = _resolve_issue_session_context(issue_number)
    worktree_path = context.worktree_path
    session_name = context.session_name
    run_dir = context.run_dir

    if requested_run_dir:
        candidate = Path(requested_run_dir)
        if candidate.exists():
            run_dir = candidate

    if run_dir:
        from ..execution.session_output_adapter import FileSystemSessionOutput

        session_output_manager = FileSystemSessionOutput()
        if not session_name:
            session_name = session_output_manager.session_name_from_path(str(run_dir))
        session_output_manager.attach_claude_log(run_dir)
        return _manifest_response(run_dir, session_name)

    if not worktree_path:
        return JSONResponse({
            "error": f"No worktree path found for issue #{issue_number}",
            "hint": "Session may have been cleaned up or never started",
        }, status_code=404)

    from ..execution.session_output_adapter import FileSystemSessionOutput
    session_output_helper = FileSystemSessionOutput()
    run_dir = session_output_helper.find_run_dir_for_issue(
        worktree_path,
        issue_number,
    )
    if not run_dir:
        return JSONResponse({
            "error": "No session run found",
            "hint": "Session may not have started or output was removed",
        }, status_code=404)
    session_output_helper.attach_claude_log(run_dir)

    return _manifest_response(run_dir, session_name)


@app.get("/api/session/worktree/{issue_number}")
async def get_session_worktree(issue_number: int) -> JSONResponse:  # noqa: C901 - worktree resolution with multiple fallbacks
    """Get the worktree path for a session (active or history)."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    context = _resolve_issue_session_context(issue_number)
    worktree_path = context.worktree_path

    if not worktree_path:
        return JSONResponse({
            "error": f"No worktree path found for issue #{issue_number}",
        }, status_code=404)

    return JSONResponse({
        "issue_number": issue_number,
        "worktree_path": str(worktree_path),
        "session_name": context.session_name,
    })


@app.get("/api/session/phases/{issue_number}")
async def get_session_phases(issue_number: int) -> JSONResponse:  # noqa: C901 - phase data extraction with fallback sources
    """Get the linear phase history for an issue.

    Returns the sequence of phases (coding-1, review-1, coding-2, etc.)
    with their status and summary information for the UI.
    """
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    from ..execution.session_output_adapter import FileSystemSessionOutput

    context = _resolve_issue_session_context(issue_number)
    worktree_path = context.worktree_path

    if not worktree_path:
        return JSONResponse({
            "phases": [],
            "current_phase": None,
            "error": "No worktree found for issue",
        })

    session_output = FileSystemSessionOutput()
    runs = session_output.list_runs(worktree_path)

    # Transform runs into phase info for UI
    phases = []
    current_phase = None
    for run in runs:
        phase_name = run.get("session_name", "unknown")
        status = run.get("status", "unknown")
        phase = {
            "name": phase_name,
            "display_name": _format_phase_name(phase_name),
            "status": status,
            "status_icon": _phase_status_icon(status),
            "started_at": run.get("started_at"),
            "ended_at": run.get("ended_at"),
            "agent_label": run.get("agent_label"),
            "run_dir": run.get("run_dir"),
            "outcome": run.get("outcome"),
            "validation_passed": run.get("validation_passed"),
        }
        phases.append(phase)
        if status == "in_progress":
            current_phase = phase_name

    return JSONResponse({
        "phases": phases,
        "current_phase": current_phase,
        "issue_number": issue_number,
    })


@app.get("/api/timeline/{issue_number}")
async def get_issue_timeline(issue_number: int) -> JSONResponse:
    """Get timeline events for an issue."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    reader = _orchestrator.deps.timeline_reader
    stream = reader.read(issue_number, limit=2000)
    payload = stream.to_dict()
    events = _filter_timeline_events(payload.get("events", []))
    events = _decorate_timeline_events(events, issue_number)
    payload["events"] = events
    payload["phase_toc"] = _build_phase_toc(events)
    payload["cycles"] = _build_timeline_cycles(events)
    diagnostic = _timeline_missing_diagnostic(issue_number, events)
    if diagnostic:
        payload["diagnostic"] = diagnostic
    return JSONResponse(payload)


@app.get("/api/issue-detail/{issue_number}", response_model=IssueDetailPayload)
async def get_issue_detail(issue_number: int) -> IssueDetailPayload | JSONResponse:
    """Get an issue-detail payload for drawer rendering."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    reader = _orchestrator.deps.timeline_reader
    stream = reader.read(issue_number, limit=2000)
    timeline = stream.to_dict()
    events = _filter_timeline_events(timeline.get("events", []))
    events = _decorate_timeline_events(events, issue_number)
    phase_toc = _build_phase_toc(events)
    cycles = _build_timeline_cycles(events)
    title = _issue_title_for(issue_number)
    issue_url = issue_url_for(_orchestrator.config, issue_number)
    context = _build_issue_story_context(issue_number)
    payload = build_issue_detail_view_model(
        issue_number=issue_number,
        title=title,
        issue_url=issue_url,
        events=events,
        phase_toc=phase_toc,
        cycles=cycles,
        context=context,
    )
    diagnostic = _timeline_missing_diagnostic(issue_number, events)
    if diagnostic:
        summary = payload.get("summary")
        if isinstance(summary, dict):
            summary["timeline_diagnostic"] = diagnostic
        payload["status_explanation"] = (
            f"Timeline data missing ({', '.join(diagnostic.get('signals', []))})"
        )
    return IssueDetailPayload.model_validate(payload)


def _build_issue_story_context(issue_number: int) -> IssueStoryContext | None:  # noqa: C901, PLR0912 — assembles story from multiple state sources
    """Assemble story context from orchestrator state for one issue."""
    if not _orchestrator:
        return None
    state = _orchestrator.state
    config = _orchestrator.config

    # Active session?
    active_runtime: int | None = None
    active_task_kind: str | None = None
    for session in state.active_sessions:
        if session.issue.number == issue_number:
            active_runtime = session.runtime_minutes
            active_task_kind = session.key.task.value
            break

    # Labels from cached queue issues or active session
    labels: tuple[str, ...] = ()
    for issue in state.cached_queue_issues:
        if issue.number == issue_number:
            labels = tuple(issue.labels)
            break
    if not labels:
        for session in state.active_sessions:
            if session.issue.number == issue_number:
                labels = tuple(session.issue.labels)
                break

    # Dependency
    dep_problem = state.dependency_problems.get(issue_number)
    dep_summary = dep_problem.summary if dep_problem else None

    # Rework cycle
    rework_cycle = 0
    for rework in state.pending_reworks:
        if rework.resolve_issue_number() == issue_number:
            rework_cycle = rework.rework_cycle
            break

    # PR info
    pr_url: str | None = None
    pr_number: int | None = None
    for review in state.pending_reviews:
        if review.issue_number == issue_number:
            pr_url = review.pr_url
            pr_number = review.pr_number
            break
    if not pr_url:
        for entry in state.session_history:
            if entry.issue_number == issue_number and entry.pr_url:
                pr_url = entry.pr_url
                break

    # Flow stage
    flow_stage = _determine_issue_flow_stage(
        issue_number, labels, active_task_kind, state, pr_url,
    )

    return IssueStoryContext(
        flow_stage=flow_stage,
        active_runtime_minutes=active_runtime,
        active_task_kind=active_task_kind,
        labels=labels,
        dependency_summary=dep_summary,
        current_rework_cycle=rework_cycle,
        max_rework_cycles=config.max_rework_cycles,
        pr_url=pr_url,
        pr_number=pr_number,
    )


def _determine_issue_flow_stage(
    issue_number: int,
    labels: tuple[str, ...],
    active_task_kind: str | None,
    state: Any,
    pr_url: str | None,
) -> str:
    """Determine the flow stage for an issue."""
    from ..domain.models import _is_blocking_label, _base_of

    # Active session = in_progress
    if active_task_kind is not None:
        return "in_progress"

    # Check labels
    if any(_is_blocking_label(l) for l in labels):
        return "blocked"

    if any(_base_of(l) == "pr-pending" for l in labels):
        return "awaiting_merge"

    # Check session history for completion
    for entry in state.session_history:
        if entry.issue_number == issue_number:
            if entry.status == "completed":
                return "done" if not pr_url else "awaiting_merge"
            if entry.status in ("blocked", "needs_human", "failed", "timed_out"):
                return "blocked"

    return "queued"


def _build_phase_toc(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    toc: list[dict[str, Any]] = []
    seen: set[str] = set()
    for event in events:
        phase = str(event.get("phase") or "system")
        if phase in seen:
            continue
        seen.add(phase)
        toc.append({
            "phase": phase,
            "label": _format_phase_name(phase),
        })
    return toc


def _build_timeline_cycles(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group timeline events into code/review cycles.

    Each cycle starts at a ``session.started`` event and encompasses
    the code → review → rework lifecycle for one attempt at the issue.
    """
    cycles: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    cycle_number = 0
    cycle_phases = {"in_progress", "reviewing", "rework", "triage", "orchestrator"}
    cycle_start_events = {"session.started", "rework.started", "review.started", "triage.launching"}

    for event in events:
        event_name = str(event.get("event") or "")
        phase = str(event.get("phase") or "system")
        if phase not in cycle_phases:
            continue

        # Do not start a new cycle from orchestration-only events.
        if current is None and event_name not in cycle_start_events:
            continue

        # A new session.started event means a new cycle — close the previous one
        if current is not None and event_name == "session.started":
            cycles.append(current)
            current = None

        if current is None:
            cycle_number += 1
            current = {
                "cycle": cycle_number,
                "start": event.get("timestamp"),
                "end": event.get("timestamp"),
                "status": event.get("status") or "started",
                "phases": [phase],
                "events": [event],
                "summary": event.get("summary") or "",
            }
            continue
        current["end"] = event.get("timestamp")
        current["status"] = event.get("status") or current["status"]
        if phase not in current["phases"]:
            current["phases"].append(phase)
        current["events"].append(event)
        if phase == "reviewing" and str(event.get("status")) in {"completed", "failed"}:
            cycles.append(current)
            current = None

    if current is not None:
        cycles.append(current)
    return cycles


def _latest_history_entries(session_history: list[Any], limit: int = 50) -> list[Any]:
    """Return most recent history entries, deduplicated by issue number."""
    return latest_history_entries_by_issue(session_history, limit=limit)


def _timeline_missing_diagnostic(
    issue_number: int,
    events: list[dict[str, Any]],
) -> dict[str, Any] | None:
    """Return diagnostic details when timeline is unexpectedly empty."""
    if events or not _orchestrator:
        return None

    state = _orchestrator.state
    signals: list[str] = []

    if any(session.issue.number == issue_number for session in state.active_sessions):
        signals.append("active_session_present")
    if any(entry.issue_number == issue_number for entry in state.session_history):
        signals.append("session_history_present")
    if any(review.issue_number == issue_number for review in state.pending_reviews):
        signals.append("pending_review_present")
    if any(rework.resolve_issue_number() == issue_number for rework in state.pending_reworks):
        signals.append("pending_rework_present")
    if issue_number in state.completed_today:
        signals.append("completed_today_present")

    # Fall back to persisted run presence as a stronger signal.
    context = _resolve_issue_session_context(issue_number)
    if context.run_dir is not None:
        signals.append("session_run_present")

    if not signals:
        return None

    logger.warning(
        "Timeline missing for issue #%s despite signals: %s",
        issue_number,
        ", ".join(signals),
    )
    from ..infra.repo_identity import state_dir

    timeline_db_path = state_dir(_orchestrator.config.repo_root) / "timeline.sqlite"
    return {
        "state": "expected_history_missing",
        "signals": signals,
        "expected_timeline_store": str(timeline_db_path),
        "expected_timeline_store_exists": timeline_db_path.exists(),
        "resolved_run_dir": str(context.run_dir) if context.run_dir else None,
    }


def _latest_session_history_entry(issue_number: int) -> Any | None:
    """Return the most recent history entry for an issue."""
    if not _orchestrator:
        return None
    for entry in reversed(_orchestrator.state.session_history):
        if entry.issue_number == issue_number:
            return entry
    return None


def _resolve_issue_session_context(issue_number: int) -> IssueSessionContext:
    """Resolve current issue session context from active or local history."""
    if not _orchestrator:
        return IssueSessionContext()

    from ..execution.session_output_adapter import FileSystemSessionOutput

    session_output = FileSystemSessionOutput()

    # Active session is authoritative.
    for session in _orchestrator.state.active_sessions:
        if session.issue.number == issue_number:
            run_dir = session_output.find_run_dir(
                session.worktree_path,
                session_name=session.terminal_id,
            )
            return IssueSessionContext(
                worktree_path=session.worktree_path,
                session_name=session.terminal_id,
                run_dir=run_dir,
            )

    # Otherwise use the latest matching history entry.
    history_entry = _latest_session_history_entry(issue_number)
    if history_entry:
        worktree_value = getattr(history_entry, "worktree_path", None)
        worktree_path = Path(worktree_value) if worktree_value else None
        run_dir = None
        if worktree_path:
            run_dir = session_output.find_run_dir_for_issue(worktree_path, issue_number)
        session_name = session_output.session_name_from_path(str(run_dir)) if run_dir else None
        return IssueSessionContext(
            worktree_path=worktree_path,
            session_name=session_name,
            run_dir=run_dir,
        )

    # Fail-fast: do not scan sibling worktrees/repos for session state.
    return IssueSessionContext()


def _filter_timeline_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop high-volume low-signal events from timeline payloads."""
    filtered: list[dict[str, Any]] = []
    for event in events:
        event_name = str(event.get("event"))
        if event_name in _NOISY_TIMELINE_EVENTS and not _is_high_signal_timeline_event(event):
            continue
        filtered.append(event)
    return filtered


def _is_high_signal_timeline_event(event: dict[str, Any]) -> bool:
    """Return True for otherwise-noisy events that affect lifecycle semantics."""
    event_name = str(event.get("event"))
    if event_name != "issue.labels_changed":
        return False
    removed = event.get("removed")
    if not isinstance(removed, list):
        return False
    return any(
        isinstance(label, str) and label.split(":", 1)[0] == "pr-pending"
        for label in removed
    )


def _decorate_timeline_events(events: list[dict[str, Any]], issue_number: int) -> list[dict[str, Any]]:
    decorated: list[dict[str, Any]] = []
    for event in events:
        event_with_actions = dict(event)
        event_with_actions["actions"] = _timeline_event_actions(event, issue_number)
        decorated.append(event_with_actions)
    return decorated


def _timeline_event_recommended_actions(
    *,
    event: dict[str, Any],
    event_name: str,
    issue_number: int,
    agent_log_label: str = "View Most Recent Session Log",
    add_action: Callable[[dict[str, Any], str], None],
) -> None:
    """Add event-specific suggested actions."""
    if bool(event.get("review_oriented")) or is_review_event_name(event_name):
        add_action(
            {"type": "open_review_feedback", "label": "View Review Feedback", "issue_number": issue_number},
            f"review-feedback:{issue_number}",
        )
    if event_name in _TIMELINE_START_EVENTS:
        add_action(
            {"type": "open_agent_log", "label": agent_log_label, "issue_number": issue_number},
            f"agent:{issue_number}",
        )
        add_action(
            {"type": "view_claude_log", "label": "View Claude Session Log", "issue_number": issue_number},
            f"claude:{issue_number}",
        )
    if event_name.startswith("validation."):
        add_action(
            {"type": "open_orchestrator_log", "label": "Open Orchestrator Log for This Issue ↗", "issue_number": issue_number},
            f"orchestrator:{issue_number}",
        )
    if event_name in _TIMELINE_FAILURE_EVENTS:
        add_action(
            {
                "type": "open_session_diagnostics",
                "label": "Diagnostics…",
                "issue_number": issue_number,
            },
            f"diagnostics:{issue_number}",
        )


def _timeline_event_artifact_actions(
    *,
    event: dict[str, Any],
    issue_number: int,
    add_action: Callable[[dict[str, Any], str], None],
) -> None:
    """Add actions derived from timeline event artifacts and run directory."""
    for artifact in event.get("artifacts", []):
        if not isinstance(artifact, dict):
            continue
        artifact_type = str(artifact.get("type") or "")
        label = str(artifact.get("label") or artifact_type or "Artifact")
        value = str(artifact.get("value") or "")
        if not value:
            continue
        if value.startswith("http://") or value.startswith("https://"):
            add_action(
                {"type": "open_url", "label": f"Open {label} ↗", "url": value},
                value,
            )
            continue
        if artifact_type in _TIMELINE_ARTIFACT_PATH_TYPES:
            add_action(
                {"type": "open_path", "label": f"Open {label}", "path": value},
                value,
            )

    run_dir = event.get("run_dir")
    if isinstance(run_dir, str) and run_dir:
        add_action(
            {"type": "open_path", "label": "Open Run Dir", "path": run_dir},
            run_dir,
        )


def _timeline_event_default_actions(
    *,
    issue_number: int,
    agent_log_label: str = "View Most Recent Session Log",
    add_action: Callable[[dict[str, Any], str], None],
) -> None:
    """Add default diagnostics and log actions shown for every timeline event."""
    add_action(
        {"type": "open_agent_log", "label": agent_log_label, "issue_number": issue_number},
        f"agent:{issue_number}",
    )
    add_action(
        {"type": "view_claude_log", "label": "View Claude Session Log", "issue_number": issue_number},
        f"claude:{issue_number}",
    )
    add_action(
        {"type": "open_orchestrator_log", "label": "Open Orchestrator Log for This Issue ↗", "issue_number": issue_number},
        f"orchestrator:{issue_number}",
    )
    add_action(
        {
            "type": "open_session_diagnostics",
            "label": "Diagnostics…",
            "issue_number": issue_number,
        },
        f"diagnostics:{issue_number}",
    )


def _timeline_event_actions(event: dict[str, Any], issue_number: int) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    event_name = str(event.get("event") or "")
    agent_log_label = _agent_log_label_for_event(event)
    event_run_dir = str(event.get("run_dir") or "")
    timeline_schema_version_raw = event.get("timeline_schema_version")
    timeline_schema_version = timeline_schema_version_raw if isinstance(timeline_schema_version_raw, int) else 0
    run_scoped_action_types = {
        "open_agent_log",
        "view_claude_log",
    }

    def _add_action(action: dict[str, Any], dedupe_value: str) -> None:
        action_type = str(action.get("type") or "")
        if action_type in run_scoped_action_types and not event_run_dir:
            return
        if event_run_dir and action_type in {
            "open_agent_log",
            "view_claude_log",
            "open_orchestrator_log",
            "open_session_diagnostics",
        }:
            action = {**action, "run_dir": event_run_dir}
        key = (action_type, dedupe_value)
        if key in seen:
            return
        seen.add(key)
        actions.append(action)

    if timeline_schema_version < MIN_SUPPORTED_TIMELINE_SCHEMA_VERSION:
        _add_action(
            {
                "type": "open_session_diagnostics",
                "label": f"Unsupported Timeline Event Version v{timeline_schema_version} (Diagnostics…)",
                "issue_number": issue_number,
            },
            f"unsupported-version:{issue_number}:{event_name}:{timeline_schema_version}",
        )
        logger.warning(
            "Timeline event has unsupported schema version: issue=%s event=%s version=%s",
            issue_number,
            event_name,
            timeline_schema_version,
        )
        return actions

    if _timeline_event_requires_run_dir(event) and not event_run_dir:
        _add_action(
            {
                "type": "open_session_diagnostics",
                "label": "Run Context Missing (Diagnostics…)",
                "issue_number": issue_number,
            },
            f"missing-run-dir:{issue_number}:{event_name}",
        )
        logger.warning(
            "Timeline event missing run_dir for run-scoped event: issue=%s event=%s",
            issue_number,
            event_name,
        )

    _timeline_event_recommended_actions(
        event=event,
        event_name=event_name,
        issue_number=issue_number,
        agent_log_label=agent_log_label,
        add_action=_add_action,
    )
    _timeline_event_artifact_actions(
        event=event,
        issue_number=issue_number,
        add_action=_add_action,
    )
    _timeline_event_default_actions(
        issue_number=issue_number,
        agent_log_label=agent_log_label,
        add_action=_add_action,
    )
    return actions


def _timeline_event_requires_run_dir(event: dict[str, Any]) -> bool:
    """Return True when a timeline event is expected to be tied to a run directory."""
    event_name = str(event.get("event") or "")
    return (
        event_name in _TIMELINE_START_EVENTS
        or is_session_event_name(event_name)
        or bool(event.get("review_oriented"))
        or is_rework_event_name(event_name)
    )


def _agent_log_label_for_event(event: dict[str, Any]) -> str:
    """Describe which session log the user will see for this event."""
    event_name = str(event.get("event") or "")
    task = str(event.get("task") or "").strip().lower()
    intent = str(event.get("event_intent") or "")
    if intent == EventIntent.REVIEW.value or bool(event.get("review_oriented")) or is_review_oriented_event(event_name=event_name, task=task):
        return "View Reviewer Session Log"
    if intent == EventIntent.REWORK.value or is_rework_event_name(event_name) or task == "rework":
        return "View Rework Session Log"
    if intent == EventIntent.CODING.value or is_session_event_name(event_name) or task in {"code", "coding"}:
        return "View Coding Session Log"
    return "View Most Recent Session Log"


def _worktree_path_from_run_dir(run_dir: Path) -> Path | None:
    """Infer worktree root from a run directory path."""
    parts = run_dir.resolve().parts
    if ".issue-orchestrator" not in parts:
        return None
    idx = parts.index(".issue-orchestrator")
    if idx <= 0:
        return None
    return Path(*parts[:idx])


def _issue_title_for(issue_number: int) -> str:
    if not _orchestrator:
        return f"Issue #{issue_number}"
    for session in _orchestrator.state.active_sessions:
        if session.issue.number == issue_number:
            return session.issue.title
    for issue in _orchestrator.state.cached_queue_issues:
        if issue.number == issue_number:
            return issue.title
    for entry in reversed(_orchestrator.state.session_history):
        if entry.issue_number == issue_number:
            return entry.title
    return f"Issue #{issue_number}"


def _format_phase_name(phase_name: str) -> str:
    """Format phase name for display (e.g., 'coding-1' -> 'Coding 1')."""
    if not phase_name:
        return "Unknown"
    parts = phase_name.split("-")
    if len(parts) == 2:
        name, num = parts
        return f"{name.title()} {num}"
    return phase_name.replace("-", " ").title()


def _phase_status_icon(status: str) -> str:
    """Return status icon for a phase."""
    icons = {
        "completed": "✓",
        "in_progress": "●",
        "validation_failed": "✗",
        "blocked": "✗",
        "timeout": "✗",
        "unknown": "○",
    }
    return icons.get(status, "○")


@app.get("/api/session/orchestrator-log/{issue_number}")
async def get_filtered_orchestrator_log(issue_number: int, run_dir: str | None = None) -> JSONResponse:  # noqa: C901, PLR0912 - log filtering with pattern matching
    """Generate and return a filtered orchestrator log for an issue.

    This generates the log on demand, filtering to entries relevant to the issue.
    Returns the path to the generated file which can be opened in an editor.
    """
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    from ..execution.session_output_adapter import FileSystemSessionOutput
    from ..infra.logging_config import get_repo_log_path

    session_output_util = FileSystemSessionOutput()

    context = _resolve_issue_session_context(issue_number)
    worktree_path = context.worktree_path
    session_name = context.session_name
    resolved_run_dir = context.run_dir
    if run_dir:
        candidate = Path(run_dir)
        if candidate.exists():
            resolved_run_dir = candidate
            inferred_worktree = _worktree_path_from_run_dir(candidate)
            if inferred_worktree:
                worktree_path = inferred_worktree
            session_name = session_output_util.session_name_from_path(str(candidate))

    if not worktree_path:
        return JSONResponse({
            "error": f"No worktree found for issue #{issue_number}",
        }, status_code=404)

    if not session_name:
        session_name = session_output_util.session_name_from_path(str(resolved_run_dir)) if resolved_run_dir else None
    if not session_name:
        return JSONResponse({
            "error": "Could not determine session name for issue log filtering",
            "worktree_path": str(worktree_path),
        }, status_code=500)

    # Get the orchestrator log path
    log_path = get_repo_log_path(_orchestrator.config.repo_root)
    if not log_path.exists():
        return JSONResponse({
            "error": "Orchestrator log file not found",
            "full_log_path": str(log_path),
        }, status_code=404)

    # Generate the filtered log
    if not resolved_run_dir:
        resolved_run_dir = session_output_util.find_run_dir_for_issue(worktree_path, issue_number)
    if not resolved_run_dir:
        return JSONResponse({
            "error": "Could not find session run directory",
            "worktree_path": str(worktree_path),
        }, status_code=500)
    tail_path = session_output_util.write_orchestrator_tail(
        resolved_run_dir,
        log_path,
        issue_number,
        session_name,
        max_lines=500,
    )

    if not tail_path:
        return JSONResponse({
            "error": f"No issue-scoped orchestrator log entries found for issue #{issue_number}",
        }, status_code=500)

    return JSONResponse({
        "filtered_log_path": str(tail_path),
        "full_log_path": str(log_path),
        "issue_number": issue_number,
    })


@app.get("/api/session/claude-log/{issue_number}")
async def get_claude_log_content(  # noqa: C901, PLR0912 - log content retrieval with multiple format handling
    issue_number: int, limit: int = 200, run_dir: str | None = None
) -> JSONResponse:
    """Fetch and parse Claude session log for viewing in the dashboard.

    Returns parsed JSONL entries for display in a formatted viewer.
    """
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)
    if not run_dir:
        return JSONResponse({
            "error": "run_dir is required",
            "hint": "Open Claude log from a run-scoped timeline action.",
        }, status_code=400)
    run_identity = RunIdentity(issue_number=issue_number, run_dir=Path(run_dir))
    accessor = ManifestAccessor(run_identity)
    try:
        artifact = accessor.get_claude_log()
    except ArtifactNotFoundError as e:
        return JSONResponse(
            {
                "error": "Claude log not found",
                "run_dir": str(run_identity.run_dir),
                "detail": str(e),
            },
            status_code=404,
        )
    log_path = artifact.path

    # Parse JSONL file
    entries = []
    try:
        with open(log_path, "r") as f:
            for i, line in enumerate(f):
                if i >= limit:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    entries.append(entry)
                except json.JSONDecodeError:
                    entries.append({"_raw": line, "_parse_error": True})
    except Exception as e:
        return JSONResponse({"error": f"Failed to read log: {e}"}, status_code=500)

    return JSONResponse({
        "log_path": str(log_path),
        "issue_number": issue_number,
        "run_dir": str(run_identity.run_dir),
        "entry_count": len(entries),
        "entries": entries,
    })


@app.post("/api/prompt/{agent_type}")
async def open_agent_prompt(agent_type: str) -> JSONResponse:
    """Open the agent's prompt file in the default editor."""
    import subprocess
    import os

    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    # Find the agent config - agent_type might be "backend" or "agent:backend"
    full_label = agent_type if agent_type.startswith("agent:") else f"agent:{agent_type}"
    agent_config = _orchestrator.config.agents.get(full_label)

    if not agent_config:
        return JSONResponse({"error": f"Agent type '{agent_type}' not found"}, status_code=404)

    prompt_path = agent_config.prompt_path
    # Resolve relative paths from repo root
    if not prompt_path.is_absolute():
        prompt_path = _orchestrator.config.repo_root / prompt_path

    if not prompt_path.exists():
        return JSONResponse({"error": f"Prompt file not found: {prompt_path}"}, status_code=404)

    # Open with default application
    if os.uname().sysname == "Darwin":
        subprocess.run(["open", str(prompt_path)])
        return JSONResponse({"status": "opened", "path": str(prompt_path)})
    else:
        return JSONResponse({"error": "Open is only available on macOS"}, status_code=400)


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
    from ..infra.repo_identity import get_repo_head_sha
    commit_sha = get_repo_head_sha(config.repo_root)

    return JSONResponse({
        "version": "0.1.0",  # TODO: get from package
        "repo": config.repo,
        "repo_root": str(config.repo_root) if config.repo_root else None,
        "ui_mode": config.ui_mode,
        "terminal_backend": config.terminal_adapter or "subprocess",
        "commit_sha": commit_sha,
        "commit_short": commit_sha[:7] if commit_sha else None,
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
    """Kill sessions and re-queue for retry."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)
    body = await request.json()
    issue_numbers = body.get("issue_numbers", [])
    state = _orchestrator.state
    killed = []
    for num in issue_numbers:
        # Kill active session if any
        for session in list(state.active_sessions):
            if session.issue.number == num:
                try:
                    _orchestrator.kill_session(session.terminal_id)
                except Exception:
                    pass
                state.active_sessions = [
                    s for s in state.active_sessions
                    if s.issue.number != num
                ]
        # Remove from history
        state.session_history = [
            entry for entry in state.session_history
            if entry.issue_number != num
        ]
        if num in state.completed_today:
            state.completed_today.remove(num)
        killed.append(num)
    return JSONResponse({"killed": killed})


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


@app.post("/api/open-file")
async def open_file(request: Request) -> JSONResponse:
    """Open a file in the default system application.

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
        # Use macOS 'open' command to open in default app
        subprocess.run(["open", file_path], check=True)
        return JSONResponse({"status": "opened", "path": file_path})
    except subprocess.CalledProcessError as e:
        return JSONResponse({"error": f"Failed to open file: {e}"}, status_code=500)
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


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

    This "nuclear option" cleans up all local and remote state:
    - Deletes local worktrees
    - Deletes remote branches
    - Removes blocking labels
    - Clears from session history

    Issues return to "available" state for a completely fresh retry.
    """
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    from ..control.maintenance import reset_issue, ResetResult

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    issue_numbers = body.get("issues", [])
    if not issue_numbers or not isinstance(issue_numbers, list):
        return JSONResponse({"error": "issues must be a non-empty list"}, status_code=400)

    state = _orchestrator.state
    config = _orchestrator.config
    repository_host = _orchestrator.repository_host
    deps = _orchestrator.deps
    lm = deps.label_manager

    reset_results: list[dict] = []
    failed: list[dict] = []

    for issue_number in issue_numbers:
        try:
            # Get current labels for full orchestrator label cleanup
            current_labels = repository_host.get_issue_labels(issue_number)

            result: ResetResult = reset_issue(
                issue_number=issue_number,
                config=config,
                worktree_manager=deps.worktree_manager,
                working_copy=deps.working_copy,
                action_applier=deps.action_applier,
                label_manager=lm,
                current_labels=current_labels,
                session_history=state.session_history,
                completed_today=state.completed_today,
                label_store=deps.label_store,
            )

            if result.success:
                reset_results.append({
                    "issue": result.issue_number,
                    "deleted_worktree": result.deleted_worktree,
                    "deleted_branch": result.deleted_branch,
                    "labels_removed": result.labels_removed,
                })
                logger.info(
                    "[reset-retry] Reset issue #%d: worktree=%s branch=%s labels=%s",
                    issue_number,
                    result.deleted_worktree or "(none)",
                    result.deleted_branch or "(none)",
                    result.labels_removed or "(none)",
                )
            else:
                failed.append({
                    "issue": issue_number,
                    "error": result.error or "Unknown error",
                    "partial": {
                        "deleted_worktree": result.deleted_worktree,
                        "deleted_branch": result.deleted_branch,
                        "labels_removed": result.labels_removed,
                    },
                })

        except Exception as e:
            logger.error("[reset-retry] Failed to reset issue #%d: %s", issue_number, e)
            failed.append({"issue": issue_number, "error": str(e)})

    # Trigger a refresh so the orchestrator picks up the reset issues
    if reset_results:
        _orchestrator.request_refresh()
        logger.info("[reset-retry] Reset %d issues, refresh triggered", len(reset_results))

    return JSONResponse({
        "reset": reset_results,
        "failed": failed,
        "refresh_triggered": len(reset_results) > 0,
    })


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


@app.get("/settings", response_class=HTMLResponse)
async def settings_page() -> HTMLResponse:
    """Render the settings page."""
    from ..infra.settings_schema import TAB_DEFINITIONS, from_config, get_settings_json_schema

    templates = get_templates()
    template = templates.get_template("settings.html")

    if not _orchestrator:
        from ..infra.config import Config
        config = Config()
    else:
        config = _orchestrator.config

    tab_values = from_config(config)
    schemas = get_settings_json_schema()
    values_dump = {k: v.model_dump() for k, v in tab_values.items()}

    # Serialize for JavaScript (Jinja2 env has no tojson filter)
    tabs_for_js = [{"key": t["key"], "label": t["label"]} for t in TAB_DEFINITIONS]

    html = template.render(
        tabs=TAB_DEFINITIONS,
        schemas=schemas,
        values=values_dump,
        tabs_json=json.dumps(tabs_for_js),
        schemas_json=json.dumps(schemas),
    )
    return HTMLResponse(content=html)


@app.get("/api/settings")
async def get_settings() -> JSONResponse:
    """Get current settings as JSON for the settings UI."""
    from ..infra.settings_schema import from_config

    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    tab_values = from_config(_orchestrator.config)
    return JSONResponse({k: v.model_dump() for k, v in tab_values.items()})


@app.post("/api/settings")
async def update_settings(request: Request) -> JSONResponse:
    """Update settings and save to YAML.

    Validates via Pydantic, applies to config, runs doctor validation,
    and saves to YAML. Rolls back on any failure.

    JSON body: {tab_key: {field: value, ...}, ...}
    """
    from pydantic import ValidationError
    from ..infra.settings_schema import TAB_DEFINITIONS, from_config, apply_to

    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    config = _orchestrator.config

    # Snapshot for rollback
    snapshot = from_config(config)
    snapshot_dump = {k: v.model_dump() for k, v in snapshot.items()}

    # Validate + parse via Pydantic
    try:
        new_tabs = {}
        for tab in TAB_DEFINITIONS:
            key = tab["key"]
            if key in body:
                new_tabs[key] = tab["model"].model_validate(body[key])
            else:
                new_tabs[key] = snapshot[key]
    except ValidationError as e:
        return JSONResponse({
            "error": "Validation failed",
            "errors": [{"name": err["loc"][-1] if err["loc"] else "unknown",
                         "detail": err["msg"]} for err in e.errors()],
        }, status_code=400)

    # Apply to config
    restart_required = apply_to(new_tabs, config)

    # Run doctor validation
    from ..infra.doctor import run_doctor
    from ..execution.command_runner import LocalCommandRunner
    result = run_doctor(config=config, runner=LocalCommandRunner())

    # Check for errors - rollback on validation failure
    errors = [c for c in result.checks if c.status == "error"]
    if errors:
        rollback_tabs = {tab["key"]: tab["model"](**snapshot_dump[tab["key"]])
                         for tab in TAB_DEFINITIONS}
        apply_to(rollback_tabs, config)
        return JSONResponse({
            "error": "Validation failed",
            "errors": [{"name": c.name, "detail": c.detail} for c in errors],
        }, status_code=400)

    # Save config to YAML
    try:
        if config.config_path:
            config.save()
            logger.info("[settings] Config saved to %s", config.config_path)
    except Exception as e:
        logger.error("[settings] Failed to save config: %s", e)
        rollback_tabs = {tab["key"]: tab["model"](**snapshot_dump[tab["key"]])
                         for tab in TAB_DEFINITIONS}
        apply_to(rollback_tabs, config)
        return JSONResponse({
            "error": f"Failed to save config: {e}",
        }, status_code=500)

    return JSONResponse({
        "success": True,
        "restart_required": restart_required,
        "warnings": [{"name": c.name, "detail": c.detail} for c in result.checks if c.status == "warning"],
    })


@app.get("/api/milestones")
async def get_milestones() -> JSONResponse:
    """Get available milestones, indicating which are included/excluded."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    config = _orchestrator.config

    try:
        # Get all milestones from GitHub via repository_host protocol
        all_milestones = _orchestrator.repository_host.list_milestones(state="open")

        # Get filter milestones from config
        filter_milestones = config.get_filter_milestones()

        milestones = []
        for m in all_milestones:
            title = m.get("title", "")
            number = m.get("number")
            is_included = not filter_milestones or title in filter_milestones
            milestones.append({
                "title": title,
                "number": number,
                "description": m.get("description", ""),
                "due_on": m.get("due_on"),
                "open_issues": m.get("open_issues", 0),
                "included": is_included,
            })

        return JSONResponse({
            "milestones": milestones,
            "filter_active": bool(filter_milestones),
            "filter_milestones": filter_milestones,
        })
    except Exception as e:
        logger.error("[web] Failed to fetch milestones: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


@app.post("/api/issues")
async def create_issue(request: Request) -> JSONResponse:
    """Create a new issue with specified labels and milestone.

    JSON body:
        title: str - Issue title (required)
        body: str - Issue body/description
        milestone: int - Milestone number (optional)
        agent: str - Agent label (e.g., "agent:backend")
        priority: str - Priority label (e.g., "P1")
        labels: list[str] - Additional labels (optional)
    """
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON"}, status_code=400)

    title = body.get("title", "").strip()
    if not title:
        return JSONResponse({"error": "Title is required"}, status_code=400)

    issue_body = body.get("body", "")
    milestone = body.get("milestone")  # milestone number
    agent = body.get("agent")
    priority = body.get("priority")
    extra_labels = body.get("labels", [])

    # Build labels list
    labels = []
    if agent:
        labels.append(agent)
    if priority:
        labels.append(priority)
    labels.extend(extra_labels)

    try:
        # Create the issue via repository_host protocol
        result = _orchestrator.repository_host.create_issue(
            title=title,
            body=issue_body,
            labels=labels,
            milestone=milestone,
        )

        if result is None:
            return JSONResponse({"error": "Failed to create issue"}, status_code=500)

        issue_number = result.get("number")
        issue_url = result.get("html_url")

        return JSONResponse({
            "status": "created",
            "issue_number": issue_number,
            "url": issue_url,
        })
    except Exception as e:
        logger.error("[web] Failed to create issue: %s", e)
        return JSONResponse({"error": str(e)}, status_code=500)


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


async def run_web_dashboard(
    orchestrator: "Orchestrator",
    port: int = 8080,
    open_browser: bool = True,
) -> None:
    """Run the web dashboard server.

    Args:
        orchestrator: The orchestrator instance
        port: Port to run on (default 8080)
        open_browser: If True, auto-open browser (default True)
    """
    global _orchestrator, _server
    _orchestrator = orchestrator

    # Also set orchestrator for mounted control_app
    from .control_api import set_orchestrator as set_control_orchestrator
    set_control_orchestrator(orchestrator)

    # Ensure port is available before starting
    ensure_port_available(port)

    import uvicorn

    logger.info("[web] Starting uvicorn server on 127.0.0.1:%d", port)

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",  # Reduce noise, we have our own logging
        timeout_graceful_shutdown=0,  # Exit immediately when shutdown requested
    )
    server = uvicorn.Server(config)
    _server = server  # Store for shutdown access

    # Open browser after a very short delay (server needs to be ready)
    if open_browser:
        async def do_open_browser():
            await asyncio.sleep(0.3)
            url = f"http://127.0.0.1:{port}"
            logger.info("[web] Opening browser to %s", url)
            webbrowser.open(url)

        asyncio.create_task(do_open_browser())

    logger.info("[web] Server starting...")
    await server.serve()
    logger.info("[web] Server stopped")


async def run_with_web_dashboard(
    orchestrator: "Orchestrator",
    port: int = 8080,
    open_browser: bool = True,
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

    import os

    if os.environ.get("ORCHESTRATOR_NO_BROWSER") in {"1", "true", "True"}:
        open_browser = False

    # Start orchestrator (startup + loop) in background
    orchestrator_task = asyncio.create_task(run_startup_and_loop())

    try:
        # Run web server in foreground (available immediately)
        await run_web_dashboard(orchestrator, port, open_browser=open_browser)
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
