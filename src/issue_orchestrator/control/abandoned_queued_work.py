"""Retire every run an operator explicitly abandoned (#7348).

Terminate, cancel-from-queue and scratch reset all end an issue's work for
good. Neither an in-memory queue entry nor an active-session record is the
whole of that work:

* a QUEUED validation retry or tech-lead run can also hold a DEFERRED row in
  the durable pending-work ledger;
* a terminal the operator ENDED still holds its claim as a HELD row, and
  termination drops the session record without settling it.

The per-tick recovery sweep re-admits both kinds of row as soon as no live run
holds them. Each abandonment path therefore goes through this one call, so none
of them can clear the queue or kill the terminal and leave a claim behind --
which is how a cancelled or terminated investigation came back on the next
tick.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Sequence

from .in_flight_work import InFlightWorkLedger, SettlementOutcome
from .tech_lead_run_retirement import TechLeadRunRetirement
from .validation_retry_retirement import ValidationRetryRetirement

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState, Session
    from ..ports.pending_work_claim_store import PendingWorkClaimStore
    from ..ports.tech_lead_authority import TechLeadAuthorityStore


def retire_abandoned_queued_work(
    *,
    state: "OrchestratorState",
    claims: "PendingWorkClaimStore",
    tech_lead_authority: "TechLeadAuthorityStore",
    issue_number: int,
    ended_sessions: Sequence["Session"],
) -> None:
    """End this issue's queued and stopped work, durably.

    ``ended_sessions`` are the terminals the operator's command ended: stopped,
    or found already dead and cleared -- never one it failed to stop. (A queue
    cancel and a scratch reset pass none: see their call sites.) Their claims are
    settled as CONSUMED: the work was attempted and then ended on purpose, so
    nothing may hand it back to a queue.
    """
    ledger = InFlightWorkLedger(state, claims)
    for session in ended_sessions:
        ledger.settle(session, SettlementOutcome.CONSUMED)
    ValidationRetryRetirement(
        state=state, claims=claims, tech_lead_authority=tech_lead_authority
    ).retire_issue(issue_number)
    TechLeadRunRetirement(state, claims).retire_issue(issue_number)


__all__ = ["retire_abandoned_queued_work"]
