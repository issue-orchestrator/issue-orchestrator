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


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request) -> HTMLResponse:
    """Render the main dashboard."""
    templates = get_templates()
    template = templates.get_template("dashboard.html")

    # Get current state
    state = _orchestrator.state if _orchestrator else None
    config = _orchestrator.config if _orchestrator else None

    sessions = []
    if state:
        for session in state.active_sessions:
            sessions.append({
                "issue_number": session.issue.number,
                "title": session.issue.title,
                "runtime_minutes": session.runtime_minutes,
                "agent_type": session.issue.agent_type or "unknown",
                "status": "running" if session.runtime_minutes < session.agent_config.timeout_minutes else "slow",
                "branch": session.branch_name,
            })

    html = template.render(
        sessions=sessions,
        paused=state.paused if state else False,
        max_sessions=config.max_sessions if config else 0,
        completed_count=len(state.completed_today) if state else 0,
        queue_count=len(state.priority_queue) if state else 0,
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


@app.post("/api/shutdown")
async def shutdown() -> JSONResponse:
    """Request orchestrator shutdown."""
    if not _orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)
    _orchestrator.request_shutdown()
    return JSONResponse({"status": "shutdown_requested"})


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
