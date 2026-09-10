"""Read-only launch grants for explicit retained-work recovery."""

from typing import Protocol, Sequence

from ..domain.validated_work_commands import ValidatedWorkAuthoritySnapshot


class ValidatedWorkRecoveryAuthorityReader(Protocol):
    """Select exact approval snapshots from the current durable store."""

    def grants_for(
        self, issue_numbers: Sequence[int]
    ) -> tuple[ValidatedWorkAuthoritySnapshot, ...]: ...


class NoValidatedWorkRecoveryAuthority:
    """Explicit composition for environments with admission but no recovery."""

    def grants_for(
        self, issue_numbers: Sequence[int]
    ) -> tuple[ValidatedWorkAuthoritySnapshot, ...]:
        return ()
