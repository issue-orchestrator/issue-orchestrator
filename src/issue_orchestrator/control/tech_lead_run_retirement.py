"""Ending a QUEUED tech-lead run for good: queue AND durable claim (#7348).

A queued tech-lead run can exist in two places at once: the in-memory pending
queue, and -- once any launch of it was deferred, or startup recovery
re-admitted it -- a *deferred* row in the pending-work ledger. The per-tick
recovery sweep (``InFlightWorkLedger.recover_unresolved``) re-admits every
deferred row whose work is not live, because an in-memory queue is not durable
and a crash must not lose the work.

So removing a run from the queue is only HALF of ending it. Every path that did
only that half -- a withdrawal on revalidation, a launch-time withdrawal, a run
lost to a peer engine, an individual investigation folded into a storm health
review, an operator cancel/terminate/scratch-reset -- left the deferred row
behind, and the sweep put the run straight back on the queue next tick.
On porchpin, #200's deferred row (``tech_lead:200``, from 2026-08-20) was
withdrawn as ``issue_closed`` and re-admitted over a thousand times.

This module is the one owner of that decision. It removes the queued items and
retires their deferred rows together, in that order: once the queue projection
is gone this process cannot launch the run, and a claim-store fault then
propagates with the row still enumerable, so the incomplete retirement is
visible rather than silently half-done.

Launch routing is the one other queue writer, and it does NOT go through here:
a launch hands the claim to the launch transaction, which holds it (superseding
the deferred row) or settles it (``LaunchTransaction.settle_unspawned``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from ..domain.pending_work import PendingWorkClaim, PendingWorkKind
from ..domain.tech_lead_session import TechLeadSessionFlavor

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState, PendingTechLeadReview
    from ..ports import EventSink
    from ..ports.pending_work_claim_store import PendingWorkClaimStore
    from .tech_lead_run_ownership import TechLeadRunOwnership

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TechLeadRunRetirement:
    """Single owner for ending queued tech-lead runs without launching them."""

    state: "OrchestratorState"
    claims: "PendingWorkClaimStore"

    def retire_issue(self, issue_number: int) -> tuple["PendingTechLeadReview", ...]:
        """End every queued run for ``issue_number`` (withdrawal, operator cancel)."""
        return self._retire(lambda item: item.issue_number == issue_number)

    def retire_failure_investigations(
        self, issue_numbers: frozenset[int]
    ) -> tuple["PendingTechLeadReview", ...]:
        """End the individual investigations a storm health review now covers.

        Batch and health anchors may share an issue number with other tech-lead
        bookkeeping and are never ended by a problem-cohort transition.
        """
        return self._retire(
            lambda item: item.flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION
            and item.issue_number in issue_numbers
        )

    def retire_run_keys(self, run_keys: frozenset[str]) -> tuple["PendingTechLeadReview", ...]:
        """End the queued runs whose logical run another engine now owns."""
        from .tech_lead_run_admission import run_key_of_pending

        return self._retire(lambda item: run_key_of_pending(item) in run_keys)

    def _retire(
        self, matches: Callable[["PendingTechLeadReview"], bool]
    ) -> tuple["PendingTechLeadReview", ...]:
        queue = self.state.pending_tech_lead_reviews
        retired = tuple(item for item in queue if matches(item))
        if not retired:
            return retired
        queue[:] = [item for item in queue if not matches(item)]
        for item in retired:
            self.claims.retire_deferred_claim(
                PendingWorkClaim(PendingWorkKind.TECH_LEAD, item).work_key()
            )
            logger.info(
                "[TECH_LEAD] Retired queued %s for #%d and its durable claim",
                item.flavor.value,
                item.issue_number,
            )
        return retired


def withdraw_queued_tech_lead_run(
    retirement: TechLeadRunRetirement,
    ownership: "TechLeadRunOwnership",
    events: "EventSink",
    *,
    run_key: str,
    issue_number: int,
    reason: str,
    detail: str,
) -> None:
    """Withdraw one queued run whose subject stopped being worth investigating.

    The single withdrawal path for both deciders -- plan-time revalidation
    (``DropTechLeadAction``) and launch-time revalidation
    (``TechLeadLaunchAuthority``) -- so they cannot disagree about what a
    withdrawal ends: the queued run, its durable claim, and its shared run
    hold (left held, a peer would wait out the whole lease).
    """
    from ..events import EventName
    from ..ports import make_trace_event

    retirement.retire_issue(issue_number)
    ownership.release(run_key)
    events.publish(
        make_trace_event(
            EventName.TECH_LEAD_RUN_WITHDRAWN,
            {
                "run_key": run_key,
                "issue_number": issue_number,
                "reason": reason,
                "detail": detail,
            },
        )
    )


__all__ = ["TechLeadRunRetirement", "withdraw_queued_tech_lead_run"]
