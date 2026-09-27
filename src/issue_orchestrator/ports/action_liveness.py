"""Ports of the action liveness owner (#7350): its durable rows and its escalation.

Two boundaries, because they fail differently. The store is local and must be
durable - a budget that lives in memory resets on every restart, which is how an
engine that restarts often retries forever. The escalation writes to the outside
world (a timeline event, the shared needs-human block, a comment) and may fail;
the owner records whether it committed rather than assuming it did.
"""

from __future__ import annotations

from typing import Protocol

from ..domain.action_liveness import ActionIdentity, LivenessKey, LivenessRow


class ActionLivenessStore(Protocol):
    """Durable failure history, one row per (subject, action, fingerprint)."""

    def row(self, key: LivenessKey) -> LivenessRow | None:
        """The row for exactly this key, or ``None`` if it never failed."""
        ...

    def put(self, row: LivenessRow) -> None:
        """Create or replace the row for ``row.key``."""
        ...

    def clear_identity(self, identity: ActionIdentity) -> tuple[LivenessRow, ...]:
        """Delete every fingerprint's row for ``identity``; return what was deleted."""
        ...

    def clear_escalation_issue(self, issue_number: int) -> tuple[LivenessRow, ...]:
        """Delete every row whose escalation names ``issue_number``; return them."""
        ...

    def escalated_rows_for_issue(self, issue_number: int) -> tuple[LivenessRow, ...]:
        """Parked rows whose escalation committed on ``issue_number``."""
        ...

    def parked_rows(self) -> tuple[LivenessRow, ...]:
        """Every parked row, oldest park first."""
        ...


class LivenessEscalation(Protocol):
    """Make a park visible to a person, and withdraw that when it resolves."""

    def escalate(self, row: LivenessRow) -> bool:
        """Announce a newly parked row. True when the human-visible block committed.

        The timeline event is always published. The needs-human block and its
        comment need ``row.key.escalation_issue``; without one, or when the
        write does not commit, this returns False and the park is still shown
        on the timeline and the tech-lead board.
        """
        ...

    def resolve(self, rows: tuple[LivenessRow, ...], *, release_issue: bool) -> None:
        """Announce that ``rows`` stopped being parked.

        ``release_issue`` withdraws this owner's cause of the needs-human block
        on their escalation issue; the caller passes it only when no other
        escalated row still stands on that issue.
        """
        ...


__all__ = ["ActionLivenessStore", "LivenessEscalation"]
