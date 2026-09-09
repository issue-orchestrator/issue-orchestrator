"""Strict transport mapping for Control Center exact-owner stop commands."""

from __future__ import annotations

from dataclasses import asdict

from pydantic import TypeAdapter

from ..contracts.public import RecoveryEngineIdentityContract
from ..contracts.ui_openapi_models import (
    StopValidatedWorkOwnerOutcomePayload,
    StopValidatedWorkOwnerRequestPayload,
)
from ..domain.validated_work_owner_stop import (
    StopOwnerOutcome,
    StopValidatedWorkOwnerCommand,
)
from .control_center_recovery import RecoveryEngineIdentityTransportMapper


_STOP_OUTCOME_ADAPTER = TypeAdapter(StopValidatedWorkOwnerOutcomePayload)


class ControlCenterRecoveryStopTransportMapper:
    """Parse external input once and serialize only typed owner outcomes."""

    @staticmethod
    def command(
        payload: StopValidatedWorkOwnerRequestPayload,
        *,
        actor: str,
    ) -> StopValidatedWorkOwnerCommand:
        if type(payload) is not StopValidatedWorkOwnerRequestPayload:
            raise TypeError("owner-stop request must use the generated contract")
        engine_contract = RecoveryEngineIdentityContract.model_validate(
            payload.expected_engine.model_dump(mode="python")
        )
        return StopValidatedWorkOwnerCommand(
            record_id=payload.record_id,
            expected_engine=RecoveryEngineIdentityTransportMapper.parse(
                engine_contract
            ),
            expected_owner_fence=payload.expected_owner_fence,
            actor=actor,
            reason=payload.reason,
        )

    @staticmethod
    def outcome(
        outcome: StopOwnerOutcome,
    ) -> StopValidatedWorkOwnerOutcomePayload:
        if type(outcome) is not StopOwnerOutcome:
            raise TypeError("owner-stop response requires a typed outcome")
        return _STOP_OUTCOME_ADAPTER.validate_python(asdict(outcome))
