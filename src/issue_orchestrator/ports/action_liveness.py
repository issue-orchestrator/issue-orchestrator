"""Ports of the action liveness owner (#7350): its durable rows and its escalation.

Two boundaries, because they fail differently. The store is local and must be
durable - a budget that lives in memory resets on every restart, which is how an
engine that restarts often retries forever. The escalation writes to the outside
world (a timeline event, the shared needs-human block, a comment) and may fail;
the owner records whether it committed rather than assuming it did.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from ..domain.action_liveness import (
    ActionIdentity,
    LivenessAnnouncement,
    LivenessKey,
    LivenessRow,
)


@dataclass(frozen=True, slots=True)
class PendingRelease:
    """A withdrawal of the owner's block cause that has not committed yet."""

    issue_number: int
    attempts: int
    attempted_at: datetime | None


class ActionLivenessStore(Protocol):
    """Durable failure history, one row per (subject, action, fingerprint)."""

    def row(self, key: LivenessKey) -> LivenessRow | None:
        """The row for exactly this key, or ``None`` if it never failed."""
        ...

    def put(self, row: LivenessRow) -> None:
        """Create or replace the row for ``row.key``."""
        ...

    def clear_key(self, key: LivenessKey, *, done_at: datetime) -> tuple[LivenessRow, ...]:
        """The operation succeeded: note its progress and delete exactly ``key``'s row.

        Every ``clear_*``, ``release_*`` and ``retire_*`` owes, in the same
        transaction, a release (:meth:`request_release`) to the issue of every
        deleted row whose block had committed, so no crash can forget a block
        that has to come off.
        """
        ...

    def update_escalation(self, row: LivenessRow) -> bool:
        """Write ``row``'s escalation state onto the SAME park it was read from.

        Conditional on that park still existing (same key and first failure,
        still parked): False when an operator released it meanwhile, so an
        escalation can never resurrect a released park.
        """
        ...

    def release_identity(self, identity: ActionIdentity) -> tuple[LivenessRow, ...]:
        """An operator's release: delete every fingerprint's row for ``identity``.

        Like every deletion it owes each deleted park a durable
        ``action.released`` announcement, published by the engine wherever the
        release came from (the CLI runs outside the engine and has no timeline).
        """
        ...

    def settle(
        self, expected: LivenessRow | None, row: LivenessRow, *, announce_parked: bool
    ) -> bool:
        """Write the row an attempt leaves behind, conditionally and atomically.

        Only if the key's row is still ``expected`` (the one the attempt read;
        ``None`` - no row - for a first failure): an operator's release in
        between must not be undone by a stale settlement, which is discarded
        (False). A FIRST failure recorded after a release is new evidence from
        an attempt that really ran, so it counts against the fresh budget. ``announce_parked`` owes the row's ``action.parked`` in the
        same transaction.
        """
        ...

    def pending_announcements(
        self,
    ) -> tuple[tuple[int, LivenessAnnouncement, LivenessRow], ...]:
        """Owed timeline announcements, oldest first, each with its id.

        Every transaction that parks a row owes a PARKED announcement, and
        every one that deletes a parked row (success, retirement, an operator's
        release) owes a RELEASED one, so a crash can delay an announcement but
        never lose it.
        """
        ...

    def clear_announcement(self, announcement_id: int) -> None:
        """The announcement was published."""
        ...

    def retire_unplanned(
        self, *, abandoned_before: datetime, superseded_before: datetime
    ) -> tuple[LivenessRow, ...]:
        """Delete every row nobody has asked about since ``abandoned_before``, and
        every row not asked since ``superseded_before`` whose operation has
        succeeded since it was last asked."""
        ...

    def touch(self, key: LivenessKey, planned_at: datetime) -> None:
        """Record that a replanning path asked ``key``'s question again."""
        ...

    def clear_escalation_issue(self, issue_number: int) -> tuple[LivenessRow, ...]:
        """Delete every row whose escalation names ``issue_number``; return them.

        An operator settled that issue's block, so any withdrawal still owed to
        it is forgotten in the same transaction: replayed later, it could take
        off a block a person has since put back.
        """
        ...

    def parked_rows_for_issue(self, issue_number: int) -> tuple[LivenessRow, ...]:
        """Every parked row that escalates on ``issue_number``, block landed or not."""
        ...

    def parked_rows(self) -> tuple[LivenessRow, ...]:
        """Every parked row, oldest park first."""
        ...

    def rows_owing_escalation(self) -> tuple[LivenessRow, ...]:
        """Parked rows with an escalation issue whose block or comment has not landed."""
        ...

    def request_release(self, issue_number: int) -> None:
        """Durably owe ``issue_number`` a withdrawal of this owner's block cause."""
        ...

    def pending_releases(self) -> tuple[PendingRelease, ...]:
        """Every owed withdrawal, with how often it has been tried."""
        ...

    def record_release_attempt(self, issue_number: int, attempted_at: datetime) -> None:
        """Count one withdrawal attempt that did not commit."""
        ...

    def clear_release(self, issue_number: int) -> None:
        """The withdrawal committed, or is no longer owed."""
        ...


class LivenessEscalation(Protocol):
    """Make a park visible to a person, and withdraw that when it resolves.

    Announcements are local and cannot fail. The block and its withdrawal are
    writes to the outside world, so each reports whether it committed and the
    owner keeps retrying an uncommitted one from its durable rows.
    """

    def announce_parked(self, row: LivenessRow) -> None:
        """Publish the park on the timeline."""
        ...

    def announce_released(self, rows: tuple[LivenessRow, ...]) -> None:
        """Publish that ``rows`` stopped being parked."""
        ...

    def block(self, row: LivenessRow) -> bool:
        """Put the needs-human block, under this owner's cause, on the row's
        escalation issue. True when it committed."""
        ...

    def explain(self, row: LivenessRow) -> bool:
        """Post the one comment explaining the block. True when it committed."""
        ...

    def unblock(self, issue_number: int) -> bool:
        """Withdraw this owner's cause of the block. True when that committed."""
        ...


__all__ = ["ActionLivenessStore", "LivenessEscalation", "PendingRelease"]
