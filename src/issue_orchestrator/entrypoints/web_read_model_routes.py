"""Dashboard read-model and row-rendering routes."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import time
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict

from ..contracts.ui_openapi_models import (
    DashboardViewModelPayload,
    IssueRowPayload,
    IssueRowsPayload,
)
from ..view_models.dashboard import build_dashboard_view_model
from .web_session_context import WebOrchestratorDependency
from .web_templates import get_templates

logger = logging.getLogger(__name__)

web_read_model_router = APIRouter()


class ViewModelSnapshotPayload(BaseModel):
    """Combined view-model + rendered rows from a single snapshot."""

    model_config = ConfigDict(extra="forbid")
    view_model: DashboardViewModelPayload
    rows: list[IssueRowPayload]
    active_tab: str
    count: int


@dataclass(frozen=True)
class DashboardQueryParams:
    """Shared dashboard pagination and tab query parameters."""

    queue_page: int
    e2e_page: int
    active_tab: str


def _dashboard_query_params(request: Request) -> DashboardQueryParams:
    queue_page = int(request.query_params.get("page", 1))
    if queue_page < 1:
        queue_page = 1
    e2e_page = int(request.query_params.get("e2e_page", 1))
    if e2e_page < 1:
        e2e_page = 1
    return DashboardQueryParams(
        queue_page=queue_page,
        e2e_page=e2e_page,
        active_tab=request.query_params.get("tab", "flow"),
    )


def _build_dashboard_vm_sync(orchestrator: Any, queue_page: int, active_tab: str, e2e_page: int):
    return build_dashboard_view_model(
        orchestrator,
        queue_page=queue_page,
        active_tab=active_tab,
        e2e_page=e2e_page,
    )


def _render_issue_rows_sync(template, view_model) -> list[dict[str, Any]]:
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
    return rows


@web_read_model_router.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, orchestrator: WebOrchestratorDependency) -> HTMLResponse:
    """Render the main dashboard."""
    request_start = time.time()

    query = _dashboard_query_params(request)
    logger.info("[dashboard] Request URL: %s, page=%s, tab=%s", request.url, query.queue_page, query.active_tab)

    templates = get_templates()
    template = templates.get_template("dashboard.html")
    vm_start = time.time()
    view_model = await asyncio.to_thread(
        _build_dashboard_vm_sync,
        orchestrator,
        query.queue_page,
        query.active_tab,
        query.e2e_page,
    )
    vm_elapsed = time.time() - vm_start
    render_start = time.time()
    html = await asyncio.to_thread(template.render, **view_model.template_context())
    render_elapsed = time.time() - render_start
    total_elapsed = time.time() - request_start
    logger.info(
        "[dashboard] Total request time: %.2fs (view_model=%.2fs render=%.2fs)",
        total_elapsed,
        vm_elapsed,
        render_elapsed,
    )
    return HTMLResponse(content=html)


@web_read_model_router.get("/api/view-model", response_model=DashboardViewModelPayload)
async def get_view_model(
    request: Request,
    orchestrator: WebOrchestratorDependency,
) -> DashboardViewModelPayload | JSONResponse:
    """Get the dashboard view model as JSON."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)
    query = _dashboard_query_params(request)

    view_model = await asyncio.to_thread(
        _build_dashboard_vm_sync,
        orchestrator,
        query.queue_page,
        query.active_tab,
        query.e2e_page,
    )
    return DashboardViewModelPayload.model_validate(view_model.to_dict())


@web_read_model_router.get("/api/view-model-snapshot", response_model=ViewModelSnapshotPayload)
async def get_view_model_snapshot(
    # Keep the dependency-injected orchestrator before Query defaults so
    # FastAPI and Python signature ordering both remain valid.
    orchestrator: WebOrchestratorDependency,
    tab: str = Query("flow"),
    page: int = Query(1, ge=1),
    e2e_page: int = Query(1, ge=1),
) -> ViewModelSnapshotPayload | JSONResponse:
    """Get view-model and rendered rows from a single snapshot.

    This keeps tab counts and rendered list rows in lockstep for UI refreshes.
    """
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    queue_page = page
    active_tab = tab

    templates = get_templates()
    row_template = templates.get_template("issue_row.html")

    def _build_snapshot_sync() -> tuple[Any, list[dict[str, Any]]]:
        vm = _build_dashboard_vm_sync(orchestrator, queue_page, active_tab, e2e_page)
        return vm, _render_issue_rows_sync(row_template, vm)

    view_model, rows = await asyncio.to_thread(_build_snapshot_sync)

    return ViewModelSnapshotPayload.model_validate({
        "view_model": view_model.to_dict(),
        "rows": rows,
        "active_tab": view_model.active_tab,
        "count": len(rows),
    })


@web_read_model_router.get("/api/issue-rows", response_model=IssueRowsPayload)
async def get_issue_rows(
    request: Request,
    orchestrator: WebOrchestratorDependency,
) -> IssueRowsPayload | JSONResponse:
    """Get rendered issue rows for the current view."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    query = _dashboard_query_params(request)

    templates = get_templates()
    template = templates.get_template("issue_row.html")

    def _build_rows_sync() -> tuple[Any, list[dict[str, Any]]]:
        vm = _build_dashboard_vm_sync(orchestrator, query.queue_page, query.active_tab, query.e2e_page)
        return vm, _render_issue_rows_sync(template, vm)

    view_model, rows = await asyncio.to_thread(_build_rows_sync)

    return IssueRowsPayload.model_validate({
        "rows": rows,
        "active_tab": view_model.active_tab,
        "count": len(rows),
    })
