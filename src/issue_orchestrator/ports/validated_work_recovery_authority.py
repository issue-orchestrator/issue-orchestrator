"""Read-only launch grants for explicit retained-work recovery and release."""

from typing import Protocol, Sequence

from ..domain.validated_work_commands import ValidatedWorkAuthoritySnapshot


class ValidatedWorkRecoveryAuthorityReader(Protocol):
    """Select exact approval snapshots from the current durable store."""

    def grants_for(
        self, issue_numbers: Sequence[int]
    ) -> tuple[ValidatedWorkAuthoritySnapshot, ...]: ...

    def release_grants_for(
        self, issue_numbers: Sequence[int]
    ) -> tuple[ValidatedWorkAuthoritySnapshot, ...]:
        """Every unresolved record an operator could release (#9092).

        Sorted by (issue, record id). Unlike :meth:`grants_for`, an issue may
        contribute several records: a release names each one it resolves.
        """
        ...


class NoValidatedWorkRecoveryAuthority:
    """Explicit composition for environments with admission but no recovery."""

    def grants_for(
        self, issue_numbers: Sequence[int]
    ) -> tuple[ValidatedWorkAuthoritySnapshot, ...]:
        return ()

    def release_grants_for(
        self, issue_numbers: Sequence[int]
    ) -> tuple[ValidatedWorkAuthoritySnapshot, ...]:
        return ()
