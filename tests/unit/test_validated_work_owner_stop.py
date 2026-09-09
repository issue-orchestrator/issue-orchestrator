"""Strict value contracts for guarded validated-work owner stops."""

from __future__ import annotations

from dataclasses import replace

import pytest

from issue_orchestrator.domain.repository_engine_lifecycle import (
    EngineIdentity,
    EngineStopAvailability,
)
from issue_orchestrator.domain.validated_work_claim import ProcessIdentity
from issue_orchestrator.domain.validated_work_discovery import ClaimOwnerFact
from issue_orchestrator.domain.validated_work_owner_stop import (
    StopOwnerOutcome,
    StopOwnerStatus,
    StopReservation,
    StopReservationRefusal,
    StopValidatedWorkOwnerCommand,
)


PROCESS = ProcessIdentity("local", 42, "linux-proc-v1:boot:17", "engine-a")
ENGINE = EngineIdentity("/repo", "engine-a", "local", "engine-a", PROCESS)
OWNER = ClaimOwnerFact(ENGINE, 3, EngineStopAvailability.AVAILABLE)


def test_stop_command_and_reservation_require_exact_typed_authority() -> None:
    command = StopValidatedWorkOwnerCommand("record", ENGINE, 3, "operator", "wedged")
    reservation = StopReservation("reservation", "record", ENGINE, 3)

    assert command.expected_engine == reservation.engine
    with pytest.raises(ValueError, match="typed engine"):
        replace(command, expected_engine="engine")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="expected owner fence"):
        replace(command, expected_owner_fence=True)
    with pytest.raises(ValueError, match="typed engine"):
        replace(reservation, engine="engine")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="owner fence"):
        replace(reservation, owner_fence=0)


@pytest.mark.parametrize(
    "status",
    [
        StopOwnerStatus.NO_SUCH_RECORD,
        StopOwnerStatus.RECORD_UNAVAILABLE,
        StopOwnerStatus.NOT_OWNED,
    ],
)
def test_owner_absent_outcomes_reject_an_observed_owner(
    status: StopOwnerStatus,
) -> None:
    StopOwnerOutcome(status, None, "unavailable")
    with pytest.raises(ValueError, match="cannot carry"):
        StopOwnerOutcome(status, OWNER, "unavailable")


@pytest.mark.parametrize(
    "status",
    [
        StopOwnerStatus.STOPPED,
        StopOwnerStatus.STOP_IN_PROGRESS,
        StopOwnerStatus.REMOTE_HOST,
        StopOwnerStatus.STOP_FAILED,
    ],
)
def test_owner_required_outcomes_reject_missing_owner(status: StopOwnerStatus) -> None:
    StopOwnerOutcome(status, OWNER, "result")
    with pytest.raises(ValueError, match="requires an observed owner"):
        StopOwnerOutcome(status, None, "result")


@pytest.mark.parametrize(
    "status", [StopOwnerStatus.OWNER_CHANGED, StopOwnerStatus.REPO_MISMATCH]
)
def test_context_dependent_outcomes_allow_optional_owner(
    status: StopOwnerStatus,
) -> None:
    StopOwnerOutcome(status, None, "result")
    StopOwnerOutcome(status, OWNER, "result")


def test_outcomes_reject_untyped_status_owner_and_blank_message() -> None:
    with pytest.raises(ValueError, match="status must be typed"):
        StopOwnerOutcome("stopped", OWNER, "result")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="typed owner"):
        StopOwnerOutcome(StopOwnerStatus.STOPPED, object(), "result")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="message"):
        StopOwnerOutcome(StopOwnerStatus.STOPPED, OWNER, " ")


@pytest.mark.parametrize(
    "status",
    [
        StopOwnerStatus.STOPPED,
        StopOwnerStatus.RECORD_UNAVAILABLE,
        StopOwnerStatus.STOP_FAILED,
    ],
)
def test_reservation_refusal_rejects_non_admission_statuses(
    status: StopOwnerStatus,
) -> None:
    owner = OWNER if status is not StopOwnerStatus.RECORD_UNAVAILABLE else None
    with pytest.raises(ValueError, match="precede a stop attempt"):
        StopReservationRefusal(status, owner, "result")
