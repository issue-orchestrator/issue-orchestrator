"""Ending QUEUED work for good: queue entry AND durable claim (#7348, #7380).

A queued request can exist in two places at once: its in-memory pending queue,
and -- once any launch of it was deferred, or startup recovery re-admitted it --
a *deferred* row in the pending-work ledger. The per-tick recovery sweep
(``InFlightWorkLedger.recover_unresolved``) re-admits every deferred row whose
work is not live, because an in-memory queue is not durable and a crash must
not lose the work.

So removing a request from its queue is only HALF of ending it. Every path that
did only that half left the deferred row behind, and the sweep put the work
straight back on the queue next tick: a tech-lead withdrawal, a run lost to a
peer engine, an investigation folded into a storm review (#7348 -- on porchpin
``tech_lead:200`` was withdrawn and re-admitted over a thousand times), and an
operator cancel / kill / scratch reset clearing a queued review or rework
(#7380).

This module is the one owner of that decision, for every queue whose item is
dequeued at launch (validation retries have their own owner,
``ValidationRetryRetirement``, because they also carry on-disk artifacts and a
launch grant). It removes the queued items and retires their deferred rows
together, in that order: once the queue projection is gone this process cannot
launch the work, and a claim-store fault then propagates with the row still
enumerable, so an incomplete retirement is visible rather than half-done.

Launch routing is the one other queue writer, and it does NOT go through here:
a launch hands the claim to the launch transaction, which holds it (superseding
the deferred row) or settles it (``LaunchTransaction.settle_unspawned``). A
LIVE run's held claim is settled where the run ends (the issue-runtime
boundary, tech-lead termination, completion), never here.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable, Iterable

from ..domain.pending_work import PendingWorkClaim, PendingWorkKind, PendingWorkRequest
from ..domain.tech_lead_session import TechLeadSessionFlavor

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState, PendingTechLeadReview
    from ..ports import EventSink
    from ..ports.pending_work_claim_store import PendingWorkClaimStore
    from .tech_lead_run_ownership import TechLeadRunOwnership

logger = logging.getLogger(__name__)

#: The queues this owner retires from, by claim kind: the state list each lives
#: in, and the issue each item belongs to. Validation retries are absent on
#: purpose (see the module docstring).
_QUEUES: dict[PendingWorkKind, tuple[str, Callable[[Any], "int | None"]]] = {
    PendingWorkKind.REVIEW: ("pending_reviews", lambda item: item.issue_number),
    PendingWorkKind.RETROSPECTIVE_REVIEW: (
        "pending_retrospective_reviews", lambda item: item.issue_number),
    PendingWorkKind.REWORK: ("pending_reworks", lambda item: item.resolve_issue_number()),
    PendingWorkKind.TECH_LEAD: ("pending_tech_lead_reviews", lambda item: item.issue_number),
}


@dataclass(frozen=True, slots=True)
class QueuedWorkRetirement:
    """Single owner for ending queued work without launching it."""

    state: "OrchestratorState"
    claims: "PendingWorkClaimStore"

    def retire_issue(
        self, issue_number: int, *, superseded_prs: Iterable[int] = ()
    ) -> tuple[PendingWorkRequest, ...]:
        """End every queued review, retrospective review, rework and tech-lead
        run of ``issue_number`` -- and any review or rework on a superseded PR
        (operator cancel, kill, scratch reset)."""
        prs = frozenset(superseded_prs)
        retired: list[PendingWorkRequest] = []
        for kind, (_, issue_of) in _QUEUES.items():
            retired.extend(self._retire(
                kind,
                lambda item, issue_of=issue_of: issue_of(item) == issue_number
                or getattr(item, "pr_number", None) in prs,
            ))
        return tuple(retired)

    def retire_tech_lead_issue(self, issue_number: int) -> tuple["PendingTechLeadReview", ...]:
        """End the queued tech-lead runs of ``issue_number`` (withdrawal)."""
        return self._retire(
            PendingWorkKind.TECH_LEAD, lambda item: item.issue_number == issue_number
        )

    def retire_failure_investigations(
        self, issue_numbers: frozenset[int]
    ) -> tuple["PendingTechLeadReview", ...]:
        """End the individual investigations a storm health review now covers.

        Batch and health anchors may share an issue number with other tech-lead
        bookkeeping and are never ended by a problem-cohort transition.
        """
        return self._retire(
            PendingWorkKind.TECH_LEAD,
            lambda item: item.flavor is TechLeadSessionFlavor.FAILURE_INVESTIGATION
            and item.issue_number in issue_numbers,
        )

    def retire_tech_lead_run_keys(
        self, run_keys: frozenset[str]
    ) -> tuple["PendingTechLeadReview", ...]:
        """End the queued tech-lead runs whose logical run a peer now owns."""
        from .tech_lead_run_admission import run_key_of_pending

        return self._retire(
            PendingWorkKind.TECH_LEAD, lambda item: run_key_of_pending(item) in run_keys
        )

    def _retire(self, kind: PendingWorkKind, matches: Callable[[Any], bool]) -> tuple[Any, ...]:
        attribute, _ = _QUEUES[kind]
        queue = getattr(self.state, attribute)
        retired = tuple(item for item in queue if matches(item))
        if not retired:
            return retired
        queue[:] = [item for item in queue if not matches(item)]
        for item in retired:
            self.claims.retire_deferred_claim(PendingWorkClaim(kind, item).work_key())
            logger.info(
                "[WORK] Retired queued %s work for #%s and its durable claim",
                kind.value,
                _QUEUES[kind][1](item),
            )
        return retired


def withdraw_queued_tech_lead_run(
    retirement: QueuedWorkRetirement,
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

    retirement.retire_tech_lead_issue(issue_number)
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


__all__ = ["QueuedWorkRetirement", "withdraw_queued_tech_lead_run"]
