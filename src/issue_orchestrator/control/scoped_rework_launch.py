"""Fresh admission and durable consumption of scoped requests by normal rework.

The pending-work claim carries exact request keys. Receipt binding precedes the
provider spawn, and the ordinary launch compensation also releases that binding.
Restoration only adopts requests already bound to the same complete run identity.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Callable

from ..domain.models import PendingRework
from ..domain.pending_work import PendingWorkClaim
from ..domain.scoped_rework import ReworkReceipt
from ..domain.session_run import SessionRunAssets, SessionRunIdentity
from ..ports import RepositoryHost
from ..ports.tech_lead_authority import TechLeadAuthorityStore
from .actions import Action, RequestReworkAction
from .launch_transaction import LaunchWorkClaim, WorkDisposal, NO_LAUNCH_WORK_CLAIM
from .reconciliation import build_expected_for_mutation
from .scoped_rework_eligibility import rework_target_stale_reason
from .session_launch_types import LaunchDisposition, LaunchResult


def scoped_rework_request_keys(
    store: TechLeadAuthorityStore, pr_number: int
) -> tuple[str, ...]:
    """References survive the queue; rendered instructions are resolved at launch."""
    candidates = tuple(
        receipt
        for receipt in store.list_rework_receipts()
        if receipt.request.target.pr_number == pr_number
        and receipt.status not in {"completed", "forward_fix", "failed"}
    )
    current = tuple(receipt for receipt in candidates if not receipt.was_rejected)
    return tuple(receipt.request.key for receipt in current or candidates)


@dataclass(frozen=True)
class ScopedReworkInstruction:
    keys: tuple[str, ...]
    feedback: str


@dataclass(frozen=True)
class ScopedReworkLaunch:
    store: TechLeadAuthorityStore
    repository: RepositoryHost
    apply_actions: Callable[..., bool]

    def admit(
        self, rework: PendingRework, pr_number: int, *,
        work_claim: LaunchWorkClaim = NO_LAUNCH_WORK_CLAIM,
    ) -> ScopedReworkInstruction | LaunchResult:
        # Scanner keys are observations. Only the durable claim owner can
        # prove that this selection must be replayed exactly after deferral.
        keys = rework.scoped_request_keys if work_claim.can_reclaim_deferred() else tuple(dict.fromkeys((
            *rework.scoped_request_keys, *scoped_rework_request_keys(self.store, pr_number))))
        # The same request object is held by PendingWorkClaim, so the normal
        # claim serializer persists this exact selection before provider spawn.
        rework.scoped_request_keys = keys
        receipts = self._load(keys)
        for receipt in receipts:
            target = receipt.request.target
            if (
                target.pr_number != pr_number
                or target.issue_number != rework.resolve_issue_number()
            ):
                detail = (
                    "Scoped target disappeared or no longer matches the queued PR/issue"
                )
                self.store.save_rework_receipt(
                    replace(receipt, status="stale", detail=detail)
                )
                return self._refused(detail)
            if receipt.was_rejected or receipt.was_unsuccessful:
                return self._refused(
                    receipt.detail or "Scoped request is no longer eligible"
                )
            # A live run can advance its own head. Only exact restoration may
            # adopt it; binding refuses to give that request to another run.
            if receipt.attempt is None:
                if failure := self._fresh(receipt):
                    return failure
        return ScopedReworkInstruction(
            keys,
            "\n\n".join(
                f"## Approved tech-lead rework ({item.request.target.head_sha})\n\n{item.request.feedback}\n\n{item.request.report}"
                for item in receipts
            ),
        )

    def has_unselected_work(self, pr_number: int, keys: tuple[str, ...]) -> bool:
        """Excluded approvals keep their normal discovery trigger after launch."""
        return bool(set(scoped_rework_request_keys(self.store, pr_number)) - set(keys))

    def before_spawn(
        self, keys: tuple[str, ...], identity: SessionRunIdentity
    ) -> LaunchResult | None:
        for receipt in self._load(keys):
            if receipt.attempt != identity:
                return self._refused("Scoped request is bound to a different run")
            if failure := self._fresh(receipt, unspawned=True):
                return failure
        return None

    def bind(
        self,
        keys: tuple[str, ...],
        identity: SessionRunIdentity,
        *,
        reclaim_deferred: bool = False,
    ) -> None:
        receipts = self.validate_binding(keys, identity, reclaim_deferred=reclaim_deferred)
        self.store.save_rework_receipts(
            tuple(
                replace(
                    item,
                    status="executing",
                    attempt=identity,
                    detail="Run claimed before provider spawn",
                )
                for item in receipts
            )
        )

    def validate_binding(
        self, keys: tuple[str, ...], identity: SessionRunIdentity, *, reclaim_deferred: bool,
    ) -> tuple[ReworkReceipt, ...]:
        """Refuse a different claim BEFORE it can supersede the durable request."""
        receipts = self._load(keys)
        for receipt in receipts:
            if (
                receipt.attempt is not None
                and receipt.attempt != identity
                and not reclaim_deferred
            ):
                raise RuntimeError(
                    "Scoped request is already consumed by a different run; restore its claim"
                )
            if receipt.status not in {"queued", "executing"} and not (
                reclaim_deferred and receipt.status == "active"
            ):
                raise RuntimeError("Scoped request is not eligible for consumption")
        return receipts

    def release_unspawned(
        self, keys: tuple[str, ...], identity: SessionRunIdentity
    ) -> None:
        self.store.save_rework_receipts(
            tuple(
                replace(
                    item,
                    status="queued",
                    attempt=None,
                    detail="Provider did not start; request retained",
                )
                for item in self._load(keys)
                if item.consumed_by(identity)
            )
        )

    def claim(
        self, work: LaunchWorkClaim, keys: tuple[str, ...]
    ) -> ScopedReworkLaunchClaim:
        return ScopedReworkLaunchClaim(work, self, keys)

    def _load(self, keys: tuple[str, ...]) -> tuple[ReworkReceipt, ...]:
        receipts = tuple(self.store.load_rework_receipt(key) for key in keys)
        if any(item is None for item in receipts):
            raise RuntimeError("Queued scoped request has no durable authority")
        return tuple(item for item in receipts if item is not None)

    def _fresh(
        self, receipt: ReworkReceipt, *, unspawned: bool = False
    ) -> LaunchResult | None:
        request = receipt.request
        pr = self.repository.get_pr(request.target.pr_number)
        issue = self.repository.get_issue(request.target.issue_number)
        stale = rework_target_stale_reason(request, pr, issue)
        if stale:
            self.store.save_rework_receipt(
                replace(receipt, status="stale", detail=stale)
            )
            return self._refused(stale)
        assert pr is not None
        if pr.state == "merged":
            # This is still the original approved instruction. Conversion to a
            # forward fix goes through the SAME typed executor as approval.
            if unspawned:
                self.store.save_rework_receipt(
                    replace(receipt, status="queued", attempt=None)
                )
            actions: list[Action] = [
                RequestReworkAction(request=request, proposal_id=request.key, expected=build_expected_for_mutation())
            ]
            committed = self.apply_actions(
                actions, context="scoped_rework_merged_before_launch"
            )
            outcome = self.store.load_rework_receipt(request.key)
            if not committed or outcome is None or outcome.status != "forward_fix":
                return LaunchResult(
                    None,
                    False,
                    "Forward-fix reconciliation did not commit",
                    disposition=LaunchDisposition.RETRYABLE_FAILURE,
                )
            return self._refused(
                "PR merged; approved instruction became a forward-fix issue"
            )
        return None

    @staticmethod
    def _refused(detail: str) -> LaunchResult:
        return LaunchResult(
            None, False, detail, disposition=LaunchDisposition.PERMANENT_FAILURE
        )


@dataclass(frozen=True)
class ScopedReworkLaunchClaim:
    work: LaunchWorkClaim
    owner: ScopedReworkLaunch
    keys: tuple[str, ...]

    def can_reclaim_deferred(self) -> bool:
        return self.work.can_reclaim_deferred()

    def hold_before_spawn(
        self, run: SessionRunAssets, *, issue_number: int
    ) -> LaunchResult | None:
        reclaim = self.work.can_reclaim_deferred() if self.keys else False
        try:
            self.owner.validate_binding(self.keys, run.identity, reclaim_deferred=reclaim)
        except RuntimeError as exc:
            return LaunchResult(None, False, str(exc), disposition=LaunchDisposition.CLAIM_UNRECORDED)
        if failure := self.work.hold_before_spawn(run, issue_number=issue_number):
            return failure
        try:
            self.owner.bind(self.keys, run.identity, reclaim_deferred=reclaim)
        except Exception as exc:
            self.work.abandon_unspawned(run)
            return LaunchResult(
                None,
                False,
                f"Scoped request claim could not commit: {exc}",
                disposition=LaunchDisposition.CLAIM_UNRECORDED,
            )
        return None

    def abandon_unspawned(self, run: SessionRunAssets) -> None:
        self.owner.release_unspawned(self.keys, run.identity)
        self.work.abandon_unspawned(run)

    def settle_unspawned(
        self, disposal: WorkDisposal, claim: PendingWorkClaim | None = None
    ) -> None:
        self.work.settle_unspawned(disposal, claim)

    def spend_budget(self, claim: PendingWorkClaim) -> bool:
        return self.work.spend_budget(claim)
