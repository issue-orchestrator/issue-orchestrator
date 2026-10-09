"""Operator-only validated-work resolution boundaries."""

from typing import Protocol

from ..domain.validated_work_commands import (
    AbandonAllOutcome,
    AbandonValidatedWorkCommand,
    AbandonValidatedWorkOutcome,
)


class ValidatedWorkAbandonmentStore(Protocol):
    def abandon_if_current(
        self, command: AbandonValidatedWorkCommand
    ) -> AbandonValidatedWorkOutcome:
        """Compare authority and resolve inside one store transaction."""
        ...

    def abandon_all_if_current(
        self, commands: tuple[AbandonValidatedWorkCommand, ...]
    ) -> AbandonAllOutcome:
        """Resolve every record in ONE transaction, or none of them (#9092)."""
        ...


class ValidatedWorkAbandonmentOwner(Protocol):
    def abandon(
        self, command: AbandonValidatedWorkCommand
    ) -> AbandonValidatedWorkOutcome:
        """Serialize abandonment with the issue-wide recovery projection."""
        ...

    def abandon_all(
        self, commands: tuple[AbandonValidatedWorkCommand, ...]
    ) -> AbandonAllOutcome:
        """Abandon several records of one issue atomically (#9092)."""
        ...
