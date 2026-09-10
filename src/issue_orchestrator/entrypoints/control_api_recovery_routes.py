"""Authenticated Control Center reads for cold retained validated work."""

from typing import Annotated

from fastapi import APIRouter, HTTPException, Path

from ..contracts.ui_openapi_models import ControlCenterRecoveryRowsPayload
from ..control.control_center_recovery_queries import (
    UnknownConfiguredRepositoryError,
)
from ..domain.control_center_recovery import CONFIGURED_REPOSITORY_KEY_PATTERN
from ..view_models.control_center_recovery import ControlCenterRecoveryTransportMapper
from .control_api_repo_support import ControlApiRepoDependency

control_recovery_router = APIRouter()


@control_recovery_router.get(
    "/api/control-center/repositories/{repo_key}/validated-work",
    response_model=ControlCenterRecoveryRowsPayload,
)
def get_repository_validated_work(
    repo_key: Annotated[str, Path(pattern=CONFIGURED_REPOSITORY_KEY_PATTERN)],
    deps: ControlApiRepoDependency,
) -> object:
    """Project retained work without contacting the target Repository Engine."""
    try:
        rows = deps.get_recovery_queries().repository(repo_key)
    except UnknownConfiguredRepositoryError as error:
        raise HTTPException(
            status_code=404,
            detail="Configured repository was not found",
        ) from error
    return ControlCenterRecoveryTransportMapper.to_contract(rows).model_dump(
        mode="json"
    )


__all__ = ["control_recovery_router"]
