"""Behavior-level boundary for guarded validated-work owner stops."""

from __future__ import annotations

from typing import Protocol

from ..domain.validated_work_owner_stop import (
    StopOwnerOutcome,
    StopValidatedWorkOwnerCommand,
)


class ValidatedWorkOwnerStop(Protocol):
    def stop_owning_engine(
        self, command: StopValidatedWorkOwnerCommand
    ) -> StopOwnerOutcome:
        """Stop the exact engine only while it owns the rendered record fence."""
        ...
