"""Operator-only validated-work resolution boundaries."""

from typing import Protocol

from ..domain.validated_work_commands import (
    AbandonValidatedWorkCommand,
    AbandonValidatedWorkOutcome,
)


class ValidatedWorkAbandonmentStore(Protocol):
    def abandon_if_current(
        self, command: AbandonValidatedWorkCommand
    ) -> AbandonValidatedWorkOutcome:
        """Compare authority and resolve inside one store transaction."""
        ...


class ValidatedWorkAbandonmentOwner(Protocol):
    def abandon(
        self, command: AbandonValidatedWorkCommand
    ) -> AbandonValidatedWorkOutcome:
        """Serialize abandonment with the issue-wide recovery projection."""
        ...
