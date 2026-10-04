"""The Control Center's cross-repository Tech lead page routes (#7763).

Thin adapters over :class:`~..execution.control_center_tech_lead.ControlCenterTechLead`:
one read of every configured repository's section, and one typed Approve /
Decline command proxied to the engine that owns the proposal. No policy here.
"""

from typing import Annotated

from fastapi import APIRouter, Path
from fastapi.responses import JSONResponse

from ..contracts.ui_openapi_models import (
    ControlCenterTechLeadPayload,
    TechLeadProposalCommandPayload,
    TechLeadProposalOutcomePayload,
)
from ..domain.control_center_recovery import CONFIGURED_REPOSITORY_KEY_PATTERN
from ..execution.control_center_tech_lead import UnknownTechLeadRepositoryError
from .control_api_repo_support import ControlApiRepoDependency

control_tech_lead_router = APIRouter()


@control_tech_lead_router.get(
    "/api/control-center/tech-lead",
    response_model=ControlCenterTechLeadPayload,
)
def get_control_center_tech_lead(deps: ControlApiRepoDependency) -> object:
    """Every configured repository's tech-lead section, engines not running included."""
    return deps.get_tech_lead().page().model_dump(mode="json")


@control_tech_lead_router.post(
    "/api/control-center/repositories/{repo_key}/tech-lead/proposals",
    response_model=TechLeadProposalOutcomePayload,
    responses={
        404: {"model": TechLeadProposalOutcomePayload},
        409: {"model": TechLeadProposalOutcomePayload},
        503: {"model": TechLeadProposalOutcomePayload},
    },
)
def command_control_center_tech_lead_proposal(
    repo_key: Annotated[str, Path(pattern=CONFIGURED_REPOSITORY_KEY_PATTERN)],
    payload: TechLeadProposalCommandPayload,
    deps: ControlApiRepoDependency,
) -> JSONResponse:
    """Approve or decline one proposal through its repository's engine."""
    try:
        result = deps.get_tech_lead().command(repo_key, payload)
    except UnknownTechLeadRepositoryError:
        missing = TechLeadProposalOutcomePayload(
            proposal_issue_number=payload.proposal_issue_number,
            outcome="unavailable",
            detail="Configured repository was not found",
        )
        return JSONResponse(missing.model_dump(mode="json"), status_code=404)
    return JSONResponse(result.outcome.model_dump(mode="json"), status_code=result.status_code)


__all__ = ["control_tech_lead_router"]
