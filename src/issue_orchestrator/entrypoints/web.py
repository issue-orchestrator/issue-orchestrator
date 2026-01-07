"""Web dashboard for the orchestrator."""

import asyncio
import json
import logging
import os
import signal
import socket
import subprocess
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING, Any

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sse_starlette.sse import EventSourceResponse
from jinja2 import Environment, FileSystemLoader

if TYPE_CHECKING:
    from ..infra.orchestrator import Orchestrator

logger = logging.getLogger(__name__)

# Create FastAPI app
app = FastAPI(title="Issue Orchestrator")

# Global reference to orchestrator (set at startup)
_orchestrator: "Orchestrator | None" = None
# Global reference to uvicorn server (for shutdown)
_server: "Any" = None

# Import shutdown manager for centralized exit handling
from ..control.shutdown_manager import shutdown_manager

# SSE event subscribers - set of asyncio.Queue objects
_event_subscribers: set[asyncio.Queue] = set()


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


def get_orchestrator():
    """Get the orchestrator instance. Override in tests via app.dependency_overrides."""
    return _orchestrator


def trigger_server_shutdown():
    """Trigger uvicorn server shutdown."""
    global _server
    if _server:
        _server.should_exit = True
        _server.force_exit = True  # Don't wait for graceful shutdown


# Template directory (templates are in parent package, not entrypoints)
TEMPLATE_DIR = Path(__file__).parent.parent / "templates"


def get_templates() -> Environment:
    """Get Jinja2 template environment."""
    return Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))


QUEUE_PAGE_SIZE = 20


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
async def dashboard(
    request: Request,
    orchestrator=Depends(get_orchestrator)
) -> HTMLResponse:
    """Render the main dashboard."""
    import time
    request_start = time.time()

    # Get query params
    queue_page = int(request.query_params.get("page", 1))
    if queue_page < 1:
        queue_page = 1
    active_tab = request.query_params.get("tab", "work")  # "work" or "problems"
    logger.info("[dashboard] Request URL: %s, page=%s, tab=%s", request.url, queue_page, active_tab)

    templates = get_templates()
    template = templates.get_template("dashboard.html")

    state = orchestrator.state if orchestrator else None
    config = orchestrator.config if orchestrator else None

    work_items = []  # Active + Queue + Completed
    problems = []    # Failed + Blocked + Needs-human + Timed-out
    seen_issues = set()  # Track issue numbers to avoid duplicates

    # Problem statuses that go to the problems tab
    PROBLEM_STATUSES = {"failed", "blocked", "needs_human", "timed_out"}

    def make_issue_url(issue_number: int) -> str:
        return f"https://github.com/{config.repo}/issues/{issue_number}" if config and config.repo else ""

    # Initialize queue_total outside conditional to avoid unbound variable
    queue_total = 0

    if state and config:
        active_numbers = {s.issue.number for s in state.active_sessions}

        # Always track active sessions to avoid showing them in queue on later pages
        seen_issues.update(active_numbers)

        # 1. Active sessions (only on page 1 of work tab)
        if queue_page == 1:
            for session in state.active_sessions:
                # Determine if session is over its timeout
                timeout = session.agent_config.timeout_minutes
                runtime = session.runtime_minutes

                # Determine phase: coding (issue-*) or reviewing (review-*)
                tmux_name = session.terminal_id or ""
                is_review = tmux_name.startswith("review-")
                phase = "Reviewing" if is_review else "Coding"

                if runtime >= timeout:
                    status = "slow"
                    status_label = "Slow"
                    status_reason = f"Over timeout ({runtime} min / {timeout} min)"
                else:
                    status = "active"
                    status_label = phase  # Show "Coding" or "Reviewing"
                    status_reason = f"Running for {runtime} min"

                seen_issues.add(session.issue.number)
                item = {
                    "issue_number": session.issue.number,
                    "title": session.issue.title,
                    "agent_type": (session.issue.agent_type or "unknown").replace("agent:", ""),
                    "status": status,
                    "status_label": status_label,
                    "status_reason": status_reason,
                    "phase": phase,
                    "time": f"{runtime} min",
                    "action": "focus",
                    "action_icon": "→",
                    "action_hint": "Click to focus iTerm2 tab",
                    "url": "",
                    # Quick links
                    "issue_url": make_issue_url(session.issue.number),
                    "pr_url": "",  # Active sessions may not have PR yet
                    "has_terminal": True,
                    "worktree_path": str(session.worktree_path) if session.worktree_path else "",
                }
                work_items.append(item)

        # 2. Queue (use cached issues for instant pagination)
        dependency_info = {}
        if state.startup_status == "complete":
            queue_issues = state.cached_queue_issues
            queue_total = len(queue_issues)
            logger.info("[dashboard] Using %d cached queue issues", queue_total)

            # Get dependency info for queue issues
            from ..infra.audit import get_issue_dependencies
            dependency_info = get_issue_dependencies(queue_issues, config)

            start_idx = (queue_page - 1) * QUEUE_PAGE_SIZE
            end_idx = start_idx + QUEUE_PAGE_SIZE
            for issue in queue_issues[start_idx:end_idx]:
                if issue.number in seen_issues:
                    continue
                seen_issues.add(issue.number)

                # Get dependency info for this issue
                dep_info = dependency_info.get(issue.number)
                has_deps = dep_info.has_dependencies if dep_info else False
                deps_json = json.dumps([
                    {"number": d[0], "title": d[1]}
                    for d in (dep_info.dependencies if dep_info else [])
                ])
                dep_summary = dep_info.summary if dep_info else ""

                item = {
                    "issue_number": issue.number,
                    "title": issue.title,
                    "agent_type": (issue.agent_type or "unknown").replace("agent:", ""),
                    "status": "queue",
                    "status_label": "Queue",
                    "status_reason": dep_summary,  # Show dependency info in tooltip
                    "time": "",
                    "action": "open",
                    "action_icon": "↗",
                    "action_hint": "Click to open issue on GitHub",
                    "url": make_issue_url(issue.number),
                    # Quick links
                    "issue_url": make_issue_url(issue.number),
                    "pr_url": "",
                    "has_terminal": False,
                    "worktree_path": "",
                    # Dependency info
                    "has_dependencies": has_deps,
                    "dependencies": deps_json,
                    "dependency_summary": dep_summary,
                }
                work_items.append(item)

        # 3. Session history - separate into work (completed) vs problems
        status_labels = {
            "completed": "Done",
            "failed": "Failed",
            "blocked": "Blocked",
            "needs_human": "Human",
            "timed_out": "Timeout",
        }
        for entry in reversed(state.session_history[-50:]):  # Increased limit for problems
            if entry.issue_number in seen_issues:
                continue
            seen_issues.add(entry.issue_number)

            url = entry.pr_url if entry.pr_url else make_issue_url(entry.issue_number)
            action_hint = "Click to open PR" if entry.pr_url else "Click to open issue on GitHub"
            status_reason = getattr(entry, 'status_reason', None) or status_labels.get(entry.status, entry.status)

            item = {
                "issue_number": entry.issue_number,
                "title": entry.title,
                "agent_type": entry.agent_type.replace("agent:", ""),
                "status": entry.status,
                "status_label": status_labels.get(entry.status, entry.status),
                "status_reason": status_reason,
                "time": f"{entry.runtime_minutes} min",
                "action": "open",
                "action_icon": "↗",
                "action_hint": action_hint,
                "url": url,
                # Quick links
                "issue_url": make_issue_url(entry.issue_number),
                "pr_url": entry.pr_url or "",
                "has_terminal": False,
                "worktree_path": "",
            }

            if entry.status in PROBLEM_STATUSES:
                problems.append(item)
            elif entry.status == "completed":
                work_items.append(item)

    # For backwards compatibility, create combined issues list based on active tab
    issues = work_items if active_tab == "work" else problems

    # Calculate pagination info
    queue_total_pages = (queue_total + QUEUE_PAGE_SIZE - 1) // QUEUE_PAGE_SIZE if queue_total > 0 else 1
    if queue_page > queue_total_pages:
        queue_page = queue_total_pages
    logger.info("[dashboard] Pagination: page=%d, total_pages=%d, total_items=%d", queue_page, queue_total_pages, queue_total)

    # Compute active session count for status display
    active_count = len(state.active_sessions) if state else 0
    shutdown_requested = getattr(orchestrator, '_shutdown_requested', False) if orchestrator else False

    # Get agents for the create issue form
    agents = config.agents if config else {}

    html = template.render(
        issues=issues,
        work_items=work_items,
        problems=problems,
        problem_count=len(problems),
        active_tab=active_tab,
        paused=state.paused if state else False,
        shutdown_requested=shutdown_requested,
        active_session_count=active_count,
        startup_status=state.startup_status if state else "pending",
        startup_message=state.startup_message if state else "",
        repo=config.repo if config else "",
        repo_root=str(config.repo_root) if config and config.repo_root else "",
        queue_page=queue_page,
        queue_total_pages=queue_total_pages,
        queue_total=queue_total,
        queue_refresh_seconds=config.queue_refresh_seconds if config else 600,
        agents=agents,
    )
    total_elapsed = time.time() - request_start
    logger.info("[dashboard] Total request time: %.2fs", total_elapsed)
    return HTMLResponse(content=html)


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

    return JSONResponse({
        "paused": state.paused,
        "shutdown_requested": getattr(_orchestrator, '_shutdown_requested', False),
        "active_sessions": sessions,
        "max_sessions": config.max_concurrent_sessions,
        "completed_today": state.completed_today,
        "queue": state.priority_queue,
        "pending_reviews": pending_reviews,
    })


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
    bypassing the queue_refresh_seconds interval. Also resets the timer for
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
    return JSONResponse({"status": "refresh_requested"})


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
        _orchestrator._kill_session(session.terminal_id)
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
    """Focus the iTerm2 tab for a specific session."""
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
    if _orchestrator.session_runner.focus_session(issue_number):
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
async def get_session_log(issue_number: int) -> JSONResponse:
    """Get Claude session log for an issue.

    Finds the most recent session log from ~/.claude/projects/<worktree-path>/
    """
    from pathlib import Path

    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    # Find worktree path from active session or history
    worktree_path = None

    # Check active sessions first
    for s in _orchestrator.state.active_sessions:
        if s.issue.number == issue_number:
            worktree_path = s.worktree_path
            break

    # If not found, check history
    if not worktree_path:
        for entry in _orchestrator.state.session_history:
            if entry.issue_number == issue_number:
                # History entries may have worktree_path stored
                worktree_path = getattr(entry, 'worktree_path', None)
                break

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

    return JSONResponse({
        "version": "0.1.0",  # TODO: get from package
        "repo": config.repo,
        "ui_mode": config.ui_mode,
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
        # Set filter_label so orchestrator picks them up
        config.filter_label = "test-data"
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
        "test_mode": config.filter_label == "test-data",  # Inferred from filter
        "filter_label": config.filter_label,
        "filter_milestone": config.filter_milestone,
        "filter_milestones": config.get_filter_milestones(),
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

    _config = _orchestrator.config

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
        # Wait for server to start and serve initial request before running startup
        await asyncio.sleep(0.5)
        try:
            # Run startup in a thread pool to avoid blocking the event loop
            # startup() makes synchronous GitHub API calls that would block serving requests
            startup_start = time.time()
            logger.info("[web] Starting orchestrator startup in thread pool...")

            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, run_startup_sync)

            startup_elapsed = time.time() - startup_start
            logger.info("[web] Startup completed in %.1fs, emitting startup_complete event", startup_elapsed)

            # Emit startup_complete HERE in the main event loop (not from thread)
            # This ensures SSE subscribers receive it properly
            await broadcast_event("startup_complete", {"elapsed_seconds": startup_elapsed})

            await orchestrator.run_loop()
        except asyncio.CancelledError:
            pass

    # Start orchestrator (startup + loop) in background
    orchestrator_task = asyncio.create_task(run_startup_and_loop())

    try:
        # Run web server in foreground (available immediately)
        await run_web_dashboard(orchestrator, port, open_browser=open_browser)
    finally:
        # When web server stops, stop orchestrator
        logger.info("[web] Shutting down orchestrator...")
        orchestrator._shutdown_requested = True
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
