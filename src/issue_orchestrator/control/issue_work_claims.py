"""Which runs hold claimed work on one issue, read from the durable claim ledger.

The pending-work claim store (``ports.pending_work_claim_store``) is the
authority on queued work that has left its queue: a HELD row is a live run
doing it, a DEFERRED row is work waiting to be relaunched. It answers per run,
and it reports rows it can read separately from rows it cannot rebuild. A
question about an ISSUE ("does any run still owe work here?") must ask both:
an unreadable row names nobody's work, so it can prove nothing is owed.

This is the one place that folds both listings into that per-issue answer, so
callers cannot ask only the readable half (#7399 review r1 F1).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..ports.pending_work_claim_store import PendingWorkClaimStore


@dataclass(frozen=True, slots=True)
class IssueWorkClaim:
    """One ledger row that holds claimed work on an issue.

    ``session_name`` and ``started_at`` are the holding run's recorded
    identity. ``work`` names the claimed work, or why the row is unreadable.
    """

    issue_number: int
    session_name: str
    started_at: str
    readable: bool
    work: str

    def held_by(self, session_name: str, started_at: str) -> bool:
        """Whether this is a READABLE row of exactly that run."""
        return self.readable and (self.session_name, self.started_at) == (session_name, started_at)

    def describe(self) -> str:
        what = self.work if self.readable else f"unreadable claim ({self.work})"
        return f"{what} held by {self.session_name} started {self.started_at}"


def claims_on_issue(store: "PendingWorkClaimStore", issue_number: int) -> tuple[IssueWorkClaim, ...]:
    """Every readable AND unreadable claim row recorded against ``issue_number``."""
    readable = tuple(
        IssueWorkClaim(row.issue_number, row.session_name, row.started_at, True, row.claim.work_key())
        for row in store.list_unresolved_claims()
        if row.issue_number == issue_number
    )
    unreadable = tuple(
        IssueWorkClaim(row.issue_number, row.session_name, row.started_at, False, row.error)
        for row in store.list_unreadable_claims()
        if row.issue_number == issue_number
    )
    return readable + unreadable
