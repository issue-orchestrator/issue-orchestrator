"""Port: the write-ahead record of an approved decision's retry (#7593).

An approved ``propose_decision`` ends by retrying its item through the
operator's retry, a GitHub label write followed by local state. The op that
carries the approval is replayed until its proposal is finalized, so the
executor must know on a replay whether the retry already happened: retrying
twice would clear a ``needs-human`` raised after the first retry, and reading
the unblocked item as "nothing waits on this decision" would close an applied
decision as stale.

So the retry is bracketed: ``begin`` is durable BEFORE the retry is attempted,
``commit`` once it committed, and ``abandon`` once it settled without
committing. What a replay does with that record is the decision table in
``domain/operator_decision_retry``.
"""

from __future__ import annotations

from typing import Protocol

from ..domain.operator_decision_retry import DecisionRetryState


class DecisionRetryLedger(Protocol):
    """Durable per-proposal retry state; cleared with the proposal's op."""

    def begin_decision_retry(self, *, proposal_issue_number: int) -> None: ...

    def commit_decision_retry(self, *, proposal_issue_number: int) -> None: ...

    def abandon_decision_retry(self, *, proposal_issue_number: int) -> None:
        """The retry settled without committing; a replay may attempt it again."""
        ...

    def decision_retry_state(self, *, proposal_issue_number: int) -> DecisionRetryState | None: ...


__all__ = ["DecisionRetryLedger", "DecisionRetryState"]
