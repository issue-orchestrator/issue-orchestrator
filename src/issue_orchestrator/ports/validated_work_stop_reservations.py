"""Narrow write authority for the validated-work owner-stop interlock."""

from __future__ import annotations

from typing import Protocol

from ..domain.validated_work_owner_stop import (
    StopReservation,
    StopReservationRefusal,
    StopValidatedWorkOwnerCommand,
)


class ValidatedWorkStopReservations(Protocol):
    def reserve_owner_stop(
        self, command: StopValidatedWorkOwnerCommand
    ) -> StopReservation | StopReservationRefusal:
        """Reserve one exact current claim generation for one stop operation."""
        ...

    def release_owner_stop(self, reservation: StopReservation) -> bool:
        """Compare and clear only the reservation represented by this token."""
        ...
