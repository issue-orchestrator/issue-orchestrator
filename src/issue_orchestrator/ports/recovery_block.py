"""Narrow durable facts for aggregate recovery-block ownership."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

from ..domain.recovery_block import (
    RecoveryBlockReconcileOutcome,
    RecoveryBlockSweepReport,
    RecoveryBlockSnapshot,
    RecoveryCleanupKey,
)

if TYPE_CHECKING:
    from .validated_work_drain import RecoveryDrainAdmission


class RecoveryBlockIssueSource(Protocol):
    def retained_issue_numbers(
        self, *, after_issue_number: int, limit: int
    ) -> tuple[int, ...]:
        """Enumerate issues with retained rows in stable bounded order."""
        ...


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


class RecoveryBlockIssueReconciler(Protocol):
    def reconcile_issue_block(
        self, issue_number: int
    ) -> RecoveryBlockReconcileOutcome: ...


class RecoveryBlockSweep(Protocol):
    def tick(self, admission: "RecoveryDrainAdmission") -> RecoveryBlockSweepReport:
        """Reproject one bounded issue batch and retain its cursor across ticks."""
        ...


class NullRecoveryBlockSweep:
    def tick(self, admission: "RecoveryDrainAdmission") -> RecoveryBlockSweepReport:
        return RecoveryBlockSweepReport(())
