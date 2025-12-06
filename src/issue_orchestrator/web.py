"""Web dashboard for the orchestrator."""

import asyncio
import logging
import webbrowser
from pathlib import Path
from typing import TYPE_CHECKING

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader

if TYPE_CHECKING:
    from .orchestrator import Orchestrator

logger = logging.getLogger(__name__)

# Create FastAPI app
app = FastAPI(title="Issue Orchestrator")

# Global reference to orchestrator (set at startup)
_orchestrator: "Orchestrator | None" = None

# Template directory
TEMPLATE_DIR = Path(__file__).parent / "templates"


def get_templates() -> Environment:
    """Get Jinja2 template environment."""
    return Environment(loader=FileSystemLoader(str(TEMPLATE_DIR)))


QUEUE_PAGE_SIZE = 20


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Render the main dashboard."""
    from .github import list_issues
    from .scheduler import Scheduler

    # Get queue page from query params
    queue_page = int(request.query_params.get("page", 1))
    if queue_page < 1:
        queue_page = 1

    templates = get_templates()
    template = templates.get_template("dashboard.html")

    state = _orchestrator.state if _orchestrator else None
    config = _orchestrator.config if _orchestrator else None

    issues = []  # Unified list
    seen_issues = set()  # Track issue numbers to avoid duplicates

    if state and config:
        active_numbers = {s.issue.number for s in state.active_sessions}
        history_numbers = {e.issue_number for e in state.session_history}

        # 1. Active sessions (sorted first)
        for session in state.active_sessions:
            # Determine if session is over its timeout
            timeout = session.agent_config.timeout_minutes
            runtime = session.runtime_minutes
            if runtime >= timeout:
                status = "slow"
                status_label = "Slow"
                status_reason = f"Over timeout ({runtime} min / {timeout} min)"
            else:
                status = "active"
                status_label = "Active"
                status_reason = f"Running for {runtime} min"

            seen_issues.add(session.issue.number)
            issues.append({
                "issue_number": session.issue.number,
                "title": session.issue.title,
                "agent_type": (session.issue.agent_type or "unknown").replace("agent:", ""),
                "status": status,
                "status_label": status_label,
                "status_reason": status_reason,
                "time": f"{runtime} min",
                "action": "focus",
                "action_icon": "→",
                "action_hint": "Click to focus iTerm2 tab",
                "url": "",
            })

        # 2. Queue (available issues not active or in history)
        queue_total = 0
        try:
            scheduler = Scheduler(config)
            all_issues = []
            for agent_label in config.agents.keys():
                labels = [agent_label]
                if config.filter_label:
                    labels.append(config.filter_label)
                fetched = list_issues(config.repo, labels=labels, milestone=config.filter_milestone)
                all_issues.extend(fetched)

            available = scheduler.get_available_issues(all_issues)
            # Filter out active/history items first to get true queue
            queue_issues = [
                issue for issue in available
                if issue.number not in active_numbers and issue.number not in history_numbers
            ]
            queue_total = len(queue_issues)

            # Apply pagination
            start_idx = (queue_page - 1) * QUEUE_PAGE_SIZE
            end_idx = start_idx + QUEUE_PAGE_SIZE
            for issue in queue_issues[start_idx:end_idx]:
                seen_issues.add(issue.number)
                issues.append({
                    "issue_number": issue.number,
                    "title": issue.title,
                    "agent_type": (issue.agent_type or "unknown").replace("agent:", ""),
                    "status": "queue",
                    "status_label": "Queue",
                    "time": "",
                    "action": "open",
                    "action_icon": "↗",
                    "action_hint": "Click to open issue on GitHub",
                    "url": f"https://github.com/{config.repo}/issues/{issue.number}",
                })
        except Exception as e:
            logger.warning(f"Failed to fetch queue: {e}")

        # 3. Session history (skip duplicates - an issue may appear multiple times in history)
        for entry in reversed(state.session_history[-20:]):
            if entry.issue_number in seen_issues:
                continue
            seen_issues.add(entry.issue_number)
            status_labels = {
                "completed": "Done",
                "failed": "Failed",
                "blocked": "Blocked",
                "needs_human": "Human",
                "timed_out": "Timeout",
            }
            url = entry.pr_url if entry.pr_url else f"https://github.com/{config.repo}/issues/{entry.issue_number}"
            action_hint = "Click to open PR" if entry.pr_url else "Click to open issue on GitHub"
            status_reason = getattr(entry, 'status_reason', None) or status_labels.get(entry.status, entry.status)
            issues.append({
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
            })

    # Calculate pagination info
    queue_total_pages = (queue_total + QUEUE_PAGE_SIZE - 1) // QUEUE_PAGE_SIZE if queue_total > 0 else 1
    if queue_page > queue_total_pages:
        queue_page = queue_total_pages

    html = template.render(
        issues=issues,
        paused=state.paused if state else False,
        repo=config.repo if config else "",
        queue_page=queue_page,
        queue_total_pages=queue_total_pages,
        queue_total=queue_total,
    )
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

    return JSONResponse({
        "paused": state.paused,
        "active_sessions": sessions,
        "max_sessions": config.max_sessions,
        "completed_today": state.completed_today,
        "queue": state.priority_queue,
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

    # Use AppleScript to focus the iTerm2 tab
    from .iterm2 import select_tab_by_name

    if select_tab_by_name(f"#{issue_number}"):
        return JSONResponse({"status": "focused", "issue_number": issue_number})
    else:
        # Try tmux as fallback
        from .tmux import get_manager
        manager = get_manager()
        if manager.select_window(issue_number):
            return JSONResponse({"status": "focused", "issue_number": issue_number})
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
async def shutdown() -> JSONResponse:
    """Request orchestrator shutdown."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)
    _orchestrator.request_shutdown()
    return JSONResponse({"status": "shutdown_requested"})


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
        "max_sessions": config.max_sessions,
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


@app.post("/api/test/create")
async def create_test_issues() -> JSONResponse:
    """Create test issues for testing."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    from .test_data import create_test_issues as _create_test_issues

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
    """Close all test issues."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    from .test_data import cleanup_test_issues as _cleanup_test_issues

    config = _orchestrator.config
    if not config.repo:
        return JSONResponse({"error": "No repo configured"}, status_code=400)
    try:
        count = _cleanup_test_issues(config.repo)
        return JSONResponse({"closed": count})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


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

    return JSONResponse({
        "paused": state.paused,
        "config_path": str(config.config_path) if config.config_path else "None",
        "repo_root": str(config.repo_root),
        "priority_queue": state.priority_queue,
        "agents": agents,
    })


async def run_web_dashboard(orchestrator: "Orchestrator", port: int = 8080) -> None:
    """Run the web dashboard server.

    Args:
        orchestrator: The orchestrator instance
        port: Port to run on (default 8080)
    """
    global _orchestrator
    _orchestrator = orchestrator

    import uvicorn

    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",  # Reduce noise
    )
    server = uvicorn.Server(config)

    # Open browser after a short delay
    async def open_browser():
        await asyncio.sleep(1)
        url = f"http://127.0.0.1:{port}"
        logger.info(f"Opening browser to {url}")
        webbrowser.open(url)

    asyncio.create_task(open_browser())

    await server.serve()


async def run_with_web_dashboard(orchestrator: "Orchestrator", port: int = 8080) -> None:
    """Run orchestrator with web dashboard.

    The orchestrator runs in a background task while the web server
    handles HTTP requests in the foreground.

    Args:
        orchestrator: The orchestrator instance
        port: Port to run web server on
    """
    async def run_orchestrator():
        """Run the orchestrator loop."""
        try:
            await orchestrator.run_loop()
        except asyncio.CancelledError:
            pass

    # Start orchestrator in background
    orchestrator_task = asyncio.create_task(run_orchestrator())

    try:
        # Run web server in foreground
        await run_web_dashboard(orchestrator, port)
    finally:
        # When web server stops, stop orchestrator
        orchestrator._shutdown_requested = True
        orchestrator_task.cancel()
        try:
            await orchestrator_task
        except asyncio.CancelledError:
            pass
