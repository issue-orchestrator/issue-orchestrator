"""Typed completion and operator historical intake transport."""

import base64
from dataclasses import asdict
from pathlib import Path

from fastapi import APIRouter, Header, HTTPException

from ..contracts.ui_openapi_models import (
    CompletionIntakeReceiptPayload,
    CompletionSubmissionPayload,
    HistoricalIntakeCommandPayload,
    HistoricalIntakeOutcomePayload,
)
from ..domain.completion_intake import (
    CompletionIntakeError,
    IntakeClosed,
    IntakeUnauthorized,
    SubmissionConflict,
    SubmitCompletionEvidence,
)
from ..domain.historical_intake import HistoricalIntakeCommand
from .control_api_issue_support import ControlApiIssueDependency

completion_intake_router = APIRouter()


@completion_intake_router.post(
    "/api/completion/submissions", response_model=CompletionIntakeReceiptPayload
)
def submit_completion(
    body: CompletionSubmissionPayload,
    deps: ControlApiIssueDependency,
    x_completion_capability: str = Header(...),
) -> CompletionIntakeReceiptPayload:
    engine = deps.get_orchestrator()
    if engine is None:
        raise HTTPException(503, "Completion intake unavailable")
    try:
        command = SubmitCompletionEvidence(
            base64.b64decode(body.raw_bytes, validate=True),
            body.content_sha256,
            body.submission_key,
        )
        receipt = engine.submit_completion_evidence(x_completion_capability, command)
    except IntakeUnauthorized as exc:
        raise HTTPException(401, "Invalid run capability") from exc
    except (IntakeClosed, SubmissionConflict) as exc:
        raise HTTPException(
            409, "Completion intake closed or submission key conflict"
        ) from exc
    except ValueError as exc:
        raise HTTPException(422, "Invalid completion bytes or content hash") from exc
    except (CompletionIntakeError, OSError) as exc:
        raise HTTPException(
            503, "Completion intake unavailable; candidate must be retained"
        ) from exc
    return CompletionIntakeReceiptPayload(
        entry_id=receipt.entry_id, content_sha256=receipt.content_sha256
    )


@completion_intake_router.post(
    "/api/validated-work/intake", response_model=HistoricalIntakeOutcomePayload
)
def import_historical(
    body: HistoricalIntakeCommandPayload, deps: ControlApiIssueDependency
):
    # Shared middleware permits admin bearer/session+CSRF only; this path is never
    # in the agent callback allowlist. Actor is audit text, not authentication.
    engine = deps.get_orchestrator()
    if engine is None:
        raise HTTPException(503, "Historical intake unavailable")
    try:
        command = HistoricalIntakeCommand(
            body.repo_slug,
            body.issue_number,
            body.branch_name,
            body.target_head_sha,
            Path(body.candidate_path),
            body.candidate_sha256,
            body.actor,
            body.reason,
        )
        return asdict(engine.import_historical_completion(command))
    except ValueError as exc:
        raise HTTPException(422, "Invalid historical selection") from exc
    except (CompletionIntakeError, OSError) as exc:
        raise HTTPException(
            503, "Historical intake unavailable; candidate retained"
        ) from exc
