"""Lightweight control API for the orchestrator.

This API is always available regardless of UI mode, providing programmatic
control over the orchestrator. The web dashboard (in web mode) adds additional
routes on top of this.

Control API endpoints:
- POST /api/refresh - Trigger immediate issue refresh
- POST /api/pause - Pause orchestrator
- POST /api/resume - Resume orchestrator
- GET /api/status - Get orchestrator status
- POST /api/shutdown - Request graceful shutdown
"""

import asyncio
import logging
from typing import TYPE_CHECKING, Any, Optional

from fastapi import FastAPI
from fastapi.responses import JSONResponse

if TYPE_CHECKING:
    from .orchestrator import Orchestrator

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
async def refresh() -> JSONResponse:
    """Request an immediate refresh of issues from GitHub.

    This triggers the orchestrator to fetch issues on the next loop iteration,
    bypassing the queue_refresh_seconds interval.
    """
    if _orchestrator is None:
        return JSONResponse({"error": "Orchestrator not initialized"}, status_code=503)

    _orchestrator.request_refresh()
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
