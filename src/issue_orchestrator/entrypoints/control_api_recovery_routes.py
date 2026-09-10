"""Authenticated Control Center reads for cold retained validated work."""

from typing import Annotated, assert_never

from fastapi import APIRouter, HTTPException, Path
from fastapi.responses import JSONResponse

from ..contracts.ui_openapi_models import (
    ControlCenterRecoveryRowsPayload,
    StopValidatedWorkOwnerOutcomePayload,
    StopValidatedWorkOwnerRequestPayload,
)
from ..control.control_center_recovery_queries import (
    UnknownConfiguredRepositoryError,
)
from ..domain.control_center_recovery import CONFIGURED_REPOSITORY_KEY_PATTERN
from ..domain.validated_work_owner_stop import (
    StopOwnerOutcome,
    StopOwnerStatus,
)
from ..view_models.control_center_recovery import ControlCenterRecoveryTransportMapper
from ..view_models.control_center_recovery_stop import (
    ControlCenterRecoveryStopTransportMapper,
)
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


@control_recovery_router.post(
    "/api/control-center/repositories/{repo_key}/engines/{instance_key}/stop-validated-work-owner",
    response_model=StopValidatedWorkOwnerOutcomePayload,
    responses={
        404: {"model": StopValidatedWorkOwnerOutcomePayload},
        409: {"model": StopValidatedWorkOwnerOutcomePayload},
        500: {"model": StopValidatedWorkOwnerOutcomePayload},
        503: {"model": StopValidatedWorkOwnerOutcomePayload},
    },
)
def stop_repository_validated_work_owner(
    repo_key: Annotated[str, Path(pattern=CONFIGURED_REPOSITORY_KEY_PATTERN)],
    instance_key: Annotated[str, Path(min_length=1)],
    payload: StopValidatedWorkOwnerRequestPayload,
    deps: ControlApiRepoDependency,
) -> JSONResponse:
    """Stop only the route- and fence-selected Repository Engine incarnation."""
    try:
        command = ControlCenterRecoveryStopTransportMapper.command(
            payload,
            actor="control-center.validated-work-stop",
        )
    except (TypeError, ValueError) as error:
        raise HTTPException(
            status_code=422,
            detail=f"Invalid exact-owner stop command: {error}",
        ) from error
    try:
        outcome = deps.get_recovery_stops().stop_owning_engine(
            repo_key,
            instance_key,
            command,
        )
    except UnknownConfiguredRepositoryError:
        outcome = StopOwnerOutcome(
            StopOwnerStatus.REPO_MISMATCH,
            None,
            "Configured repository was not found",
        )
        status_code = 404
    else:
        status_code = _stop_owner_status_code(outcome.status)
    response = ControlCenterRecoveryStopTransportMapper.outcome(outcome)
    return JSONResponse(response.model_dump(mode="json"), status_code=status_code)


def _stop_owner_status_code(status: StopOwnerStatus) -> int:
    match status:
        case StopOwnerStatus.STOPPED:
            return 200
        case StopOwnerStatus.NO_SUCH_RECORD:
            return 404
        case StopOwnerStatus.RECORD_UNAVAILABLE:
            return 503
        case (
            StopOwnerStatus.NOT_OWNED
            | StopOwnerStatus.OWNER_CHANGED
            | StopOwnerStatus.STOP_IN_PROGRESS
            | StopOwnerStatus.REPO_MISMATCH
            | StopOwnerStatus.REMOTE_HOST
        ):
            return 409
        case StopOwnerStatus.STOP_FAILED:
            return 500
        case _:
            assert_never(status)


__all__ = ["control_recovery_router"]
