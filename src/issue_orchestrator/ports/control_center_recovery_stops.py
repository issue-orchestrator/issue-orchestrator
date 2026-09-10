"""Behavior-level boundary for Control Center retained-work owner stops."""

from __future__ import annotations

from typing import Protocol

from ..domain.validated_work_owner_stop import (
    StopOwnerOutcome,
    StopValidatedWorkOwnerCommand,
)


class ControlCenterRecoveryStopPort(Protocol):
    def stop_owning_engine(
        self,
        repo_key: str,
        instance_key: str,
        command: StopValidatedWorkOwnerCommand,
    ) -> StopOwnerOutcome:
        """Stop the exact rendered owner within the route-selected repository."""
        ...
