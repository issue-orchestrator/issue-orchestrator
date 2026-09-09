"""Narrow durable facts for aggregate recovery-block ownership."""

from typing import Protocol

from ..domain.recovery_block import RecoveryBlockSnapshot, RecoveryCleanupKey


class RecoveryBlockStore(Protocol):
    def recovery_block_snapshot(
        self, repo_slug: str, issue_number: int
    ) -> RecoveryBlockSnapshot:
        """Read every retained sibling, phase and capture in one transaction.

        Released publishing interests require durable successful publication
        and routing, never an unchecked phase column. Invalid rows raise.
        """
        ...

    def begin_block_label_cleanup(
        self, keys: tuple[RecoveryCleanupKey, ...], label: str
    ) -> bool:
        """Persist exact-generation intent BEFORE removal; True only once.

        False means a prior removal may have committed. The caller may observe
        absence but must not repeat removal of a currently present label.
        Invalid/changed/ineligible generations raise without partial writes.
        """
        ...

    def acknowledge_block_cleanup(self, keys: tuple[RecoveryCleanupKey, ...]) -> bool:
        """After observed cleanup, CAS its exact evidence/attempt generations.

        Called under the issue gate (and the publishing caller's effect scope).
        False refuses changed or ineligible records; no partial receipt writes.
        """
        ...
