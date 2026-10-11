"""Control-API pause and resume routes.

Split out of ``control_api.py`` the way the other ``control_api_*_routes``
modules are: that file is far over its line budget, and pause now carries real
behaviour (actor resolution, typed status, provenance in the status payload)
rather than a one-line delegation.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..domain.pause_state import PauseActor
from .pause_response import pause_engine, resume_engine

control_pause_router = APIRouter()


@control_pause_router.post("/api/pause")
async def pause(request: Request) -> JSONResponse:
    """Pause the orchestrator - stop launching new sessions."""
    # Imported lazily: control_api includes this router, so a module-level
    # import would close the cycle.
    from .control_api import get_orchestrator

    orchestrator = get_orchestrator()
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not initialized"}, status_code=503)

    return await pause_engine(request, orchestrator, PauseActor.CONTROL_API)


@control_pause_router.post("/api/resume")
async def resume(request: Request) -> JSONResponse:
    """Resume the orchestrator - allow launching new sessions."""
    from .control_api import get_orchestrator

    orchestrator = get_orchestrator()
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not initialized"}, status_code=503)

    return await resume_engine(request, orchestrator, PauseActor.CONTROL_API)
