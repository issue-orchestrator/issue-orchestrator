"""Dashboard retrospective-review routes."""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..control.actions import AddLabelAction
from ..control.retrospective_review import (
    preflight_retrospective_review_issues,
    queue_retrospective_review_request,
    retrospective_review_preflight_payload,
)
from ..contracts.ui_openapi_models import (
    RetrospectiveReviewExecutePayload,
    RetrospectiveReviewPreflightPayload,
)
from .web_issue_number_payload import parse_issue_numbers_payload
from .web_retry_history_routes import elapsed_ms
from .web_session_context import WebOrchestratorDependency

logger = logging.getLogger(__name__)

web_retrospective_review_router = APIRouter()


@web_retrospective_review_router.post(
    "/api/retrospective-review/preflight",
    response_model=RetrospectiveReviewPreflightPayload,
)
async def retrospective_review_preflight(
    request: Request,
    orchestrator: WebOrchestratorDependency,
) -> RetrospectiveReviewPreflightPayload | JSONResponse:
    """Preview retrospective-review decisions without mutation."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    parsed = await parse_issue_numbers_payload(request)
    if parsed.error_response is not None:
        return parsed.error_response

    decisions = preflight_retrospective_review_issues(
        issue_numbers=parsed.issue_numbers,
        repository_host=orchestrator.repository_host,
        config=orchestrator.config,
    )
    return RetrospectiveReviewPreflightPayload.model_validate(
        retrospective_review_preflight_payload(
            decisions,
            trigger_label=orchestrator.config.retrospective_review_trigger_label,
        )
    )


@web_retrospective_review_router.post(
    "/api/retrospective-review",
    response_model=RetrospectiveReviewExecutePayload,
)
async def queue_retrospective_review(
    request: Request,
    orchestrator: WebOrchestratorDependency,
) -> RetrospectiveReviewExecutePayload | JSONResponse:
    """Apply the trigger label and queue eligible retrospective reviews."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    parsed = await parse_issue_numbers_payload(request)
    if parsed.error_response is not None:
        return parsed.error_response

    started = time.monotonic()
    decisions = preflight_retrospective_review_issues(
        issue_numbers=parsed.issue_numbers,
        repository_host=orchestrator.repository_host,
        config=orchestrator.config,
    )
    queued: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for decision in decisions:
        if not decision.eligible:
            continue
        action = AddLabelAction(
            issue_number=decision.issue,
            label=orchestrator.config.retrospective_review_trigger_label,
            reason="retrospective review queued from dashboard",
        )
        result = orchestrator.deps.action_applier.apply(action)
        if not result.success:
            failed.append({
                "issue": decision.issue,
                "error": result.error or "Failed to apply retrospective review label",
            })
            continue
        queued_now = queue_retrospective_review_request(
            state=orchestrator.state,
            repository_host=orchestrator.repository_host,
            decision=decision,
        )
        payload = decision.to_payload()
        payload["queued"] = queued_now
        queued.append(payload)

    skipped = [
        decision.to_payload()
        for decision in decisions
        if not decision.eligible
    ]
    logger.info(
        "[retrospective-review] Dashboard queue complete: issues=%s queued=%s "
        "skipped=%s failed=%s duration_ms=%d",
        parsed.issue_numbers,
        [item["issue"] for item in queued],
        [item["issue"] for item in skipped],
        [item.get("issue") for item in failed],
        elapsed_ms(started),
    )
    return RetrospectiveReviewExecutePayload.model_validate(
        {
            "queued": queued,
            "failed": failed,
            "skipped": skipped,
            "workflow": "retrospective_review",
            "trigger_label": orchestrator.config.retrospective_review_trigger_label,
            "refresh_triggered": False,
        }
    )
