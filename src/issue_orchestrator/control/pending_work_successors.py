"""Durable proof that retained work owns an exact request after its run ends.

The ledger, not queue snapshots or receipt status, grants a deferred transfer.
Exact launch replay and scoped successor lookup share this owner, including
restarts between recording a replacement claim and binding its receipts.
"""

from __future__ import annotations

from dataclasses import dataclass
from ..domain.models import PendingRework
from ..domain.pending_work import PendingWorkClaim, PendingWorkKind
from ..domain.scoped_rework import ReworkRequest
from ..ports.pending_work_claim_store import PendingWorkClaimStore, UnresolvedClaim


@dataclass(frozen=True)
class PendingWorkSuccessors:
    claims: PendingWorkClaimStore

    def retains_exact(self, claim: PendingWorkClaim) -> bool:
        """Only the complete durable request authorizes an unchanged replay."""
        return any(item.claim == claim for item in self._deferred())

    def owns_scoped(self, request: ReworkRequest) -> bool:
        """Prove retained rework names this immutable instruction and target."""
        target = request.target
        return any(
            row.issue_number == target.issue_number
            and claim.kind is PendingWorkKind.REWORK
            and isinstance(claim.request, PendingRework)
            and claim.request.resolve_issue_number() == target.issue_number
            and claim.request.issue_key.scope() == target.repository
            and claim.request.pr_number == target.pr_number
            and request.key in claim.request.scoped_request_keys
            for row in self._deferred()
            for claim in (row.claim,)
        )

    def _deferred(self) -> tuple[UnresolvedClaim, ...]:
        return tuple(
            row for row in self.claims.list_unresolved_claims() if row.deferred
        )
