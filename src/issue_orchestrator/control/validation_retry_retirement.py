"""Retire every durable owner of an explicitly ABANDONED validation retry.

Round 11 made a provider deferral retain the tech-lead authority row, because
the requeued claim still names it. That is right for a deferral, where the work
is coming back. It is wrong for abandonment: an operator who cancels or resets
an issue removed only the in-memory queue entry, so the durable claim and the
grant both survived -- and startup recovery then requeued the cancelled
investigation and could now launch it SUCCESSFULLY, which is new damage that
round 11's retention introduced (round 12 finding 1).

A validation retry exists in FOUR places: the in-memory queue, the durable
validation artifacts on disk, the durable pending-work ledger, and, for
tech-lead work, the launch-authority ledger. Nothing owned all four, so each
abandonment path cleared whichever it knew about -- and startup scans the
artifacts independently of the claim store, so leaving them behind resurrected
the cancelled retry anyway (round 14 finding 1).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ..domain.pending_work import PendingWorkClaim, PendingWorkKind
from ..infra.validation_state import retire_pending_retry_artifacts

if TYPE_CHECKING:
    from ..domain.models import OrchestratorState
    from ..ports.pending_work_claim_store import PendingWorkClaimStore
    from ..ports.tech_lead_authority import TechLeadAuthorityStore


@dataclass(slots=True)
class ValidationRetryRetirement:
    """Single owner for abandoning queued validation-retry work."""

    state: "OrchestratorState"
    claims: "PendingWorkClaimStore"
    tech_lead_authority: "TechLeadAuthorityStore"

    def retire_issue(self, issue_number: int) -> None:
        """Drop every durable trace of this issue's queued retries."""
        retries = tuple(
            retry
            for retry in self.state.pending_validation_retries
            if retry.issue_number == issue_number
        )
        for retry in retries:
            authority_run = retry.authority_run
            if authority_run is not None:
                authority = self.tech_lead_authority.load(
                    run_id=authority_run.run_id,
                    session_name=authority_run.session_name,
                )
                # Authority goes FIRST. If the claim store then faults, startup
                # may recover the claim -- but it cannot execute abandoned
                # tech-lead work, because the grant its completion is admitted
                # against is already gone.
                self.tech_lead_authority.discard(
                    run_id=authority_run.run_id,
                    session_name=authority_run.session_name,
                )
                if authority is not None:
                    self.tech_lead_authority.discard_storm_cohort(
                        anchor_issue_number=authority.anchor_issue_number
                    )
            # Startup scans these artifacts independently of the claim store,
            # so they go before the claim: a crash between the two writes must
            # not leave a recoverable retry the operator already abandoned.
            # Called through the same infra seam `ValidationRetryRecovery` reads
            # them from, rather than a new port method -- one owner for the
            # artifact shape, and it is the exact inverse of the read.
            retire_pending_retry_artifacts(Path(retry.worktree_path))

            # Once artifact retirement commits, drop the live queue projection
            # before touching the claim. If claim retirement then faults, this
            # process cannot launch work the operator just abandoned, and a
            # surviving claim still makes the incomplete cancellation visible
            # to restart recovery.
            self.state.pending_validation_retries[:] = [
                queued
                for queued in self.state.pending_validation_retries
                if queued.issue_number != issue_number
            ]

            claim = PendingWorkClaim(PendingWorkKind.VALIDATION_RETRY, retry)
            self.claims.retire_deferred_claim(claim.work_key())


__all__ = ["ValidationRetryRetirement"]
