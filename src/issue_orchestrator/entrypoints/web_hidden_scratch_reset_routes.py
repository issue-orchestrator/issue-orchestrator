"""Dashboard hidden issue scratch-reset routes."""

from __future__ import annotations

import logging
import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..control.hidden_scratch_reset import (
    hidden_scratch_preflight_payload,
    preflight_hidden_scratch_reset_issues,
)
from ..control.queue_cache import QueueCache
from ..domain.fresh_lifecycle_rerun import FRESH_LIFECYCLE_RERUN_INTENT
from .web_issue_number_payload import parse_issue_numbers_payload
from .web_retry_history_routes import elapsed_ms, reset_and_retry_issue
from .web_session_context import WebOrchestratorDependency

logger = logging.getLogger(__name__)

web_hidden_scratch_reset_router = APIRouter()


@web_hidden_scratch_reset_router.post("/api/reset-retry/hidden-scratch/preflight")
async def hidden_scratch_reset_preflight(
    request: Request,
    orchestrator: WebOrchestratorDependency,
) -> JSONResponse:
    """Preview hidden issue reset-from-scratch decisions without mutation."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    parsed = await parse_issue_numbers_payload(request)
    if parsed.error_response is not None:
        return parsed.error_response

    decisions = preflight_hidden_scratch_reset_issues(
        issue_numbers=parsed.issue_numbers,
        repository_host=orchestrator.repository_host,
        config=orchestrator.config,
    )
    return JSONResponse(hidden_scratch_preflight_payload(decisions))


@web_hidden_scratch_reset_router.post("/api/reset-retry/hidden-scratch")
async def hidden_scratch_reset_and_retry(
    request: Request,
    orchestrator: WebOrchestratorDependency,
) -> JSONResponse:
    """Reset hidden issues from scratch after scope-safe preflight."""
    if orchestrator is None:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    from ..control.maintenance import reset_issue

    parsed = await parse_issue_numbers_payload(request)
    if parsed.error_response is not None:
        return parsed.error_response

    started = time.monotonic()
    state = orchestrator.state
    config = orchestrator.config
    repository_host = orchestrator.repository_host
    deps = orchestrator.deps
    lm = deps.label_manager
    queue_cache = QueueCache(config, state, deps.queue_cache_store)

    decisions = preflight_hidden_scratch_reset_issues(
        issue_numbers=parsed.issue_numbers,
        repository_host=repository_host,
        config=config,
    )
    reset_results: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []
    reopened: list[int] = []

    for decision in decisions:
        if not decision.eligible:
            continue
        try:
            if decision.will_reopen:
                repository_host.update_issue_state(decision.issue, "open")
                reopened.append(decision.issue)
        except Exception as exc:
            logger.error(
                "[reset-retry] Failed to reopen hidden issue #%d: %s",
                decision.issue,
                exc,
                exc_info=True,
            )
            failed.append({"issue": decision.issue, "error": str(exc)})
            continue

        success_payload, failure_payload = reset_and_retry_issue(
            issue_number=decision.issue,
            from_scratch=True,
            pending_label=lm.reset_retry_pending,
            scratch_pending_label=lm.reset_retry_scratch_pending,
            repository_host=repository_host,
            queue_cache=queue_cache,
            state=state,
            deps=deps,
            config=config,
            reset_issue_fn=reset_issue,
            current_labels=decision.labels,
            extra_pending_labels=[lm.fresh_lifecycle_rerun],
        )
        if success_payload is not None:
            success_payload["reopened"] = decision.will_reopen
            reset_results.append(success_payload)
            continue
        if failure_payload is not None:
            failure_payload["reopened"] = decision.will_reopen
            failed.append(failure_payload)
            continue
        failed.append({
            "issue": decision.issue,
            "error": "Unknown hidden scratch reset failure",
            "reopened": decision.will_reopen,
        })

    skipped = [
        decision.to_payload()
        for decision in decisions
        if not decision.eligible
    ]
    logger.info(
        "[reset-retry] Hidden scratch reset complete: issues=%s reset=%s "
        "skipped=%s failed=%s reopened=%s duration_ms=%d",
        parsed.issue_numbers,
        [result["issue"] for result in reset_results],
        [decision["issue"] for decision in skipped],
        [failure.get("issue") for failure in failed],
        reopened,
        elapsed_ms(started),
    )
    return JSONResponse({
        "reset": reset_results,
        "failed": failed,
        "skipped": skipped,
        "reopened": reopened,
        "from_scratch": True,
        "rerun_intent": FRESH_LIFECYCLE_RERUN_INTENT,
        "refresh_triggered": False,
    })
