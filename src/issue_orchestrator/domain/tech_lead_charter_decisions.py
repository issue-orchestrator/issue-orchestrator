"""The persisted per-action charter decision record (#7330, read by #7331).

For every tech-lead action the orchestrator decides, one
:class:`TechLeadCharterDecision` records what was decided and WHY: the action,
what it targets, the role and depth the orchestrator classified it under, both
dials in force at decision time (the role's charter and the per-action
ceiling), the outcome, and a stable reason code plus plain words. A UI explains
an item's state by READING this record — it never recomputes the charter,
because the charter it would recompute against may have changed since.

Where the existing gated-proposal lifecycle reports what happened next, the
record carries it as :class:`CharterProposalLifecycle` (approved and applied,
approved but stale, or declined). An action the charter let run directly
carries what its applier actually did as :class:`CharterExecutionResult`
(#7362): the verdict says it was ALLOWED to execute, never that it worked.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import Enum
from typing import Any

from .tech_lead_charter import (
    CharterAuthority,
    CharterBinding,
    CharterDepth,
    CharterOutcome,
    CharterReason,
    CharterRole,
    CharterVerdict,
)


class CharterDecisionSource(str, Enum):
    """Where the decided action came from."""

    #: A proposed action in a tech-lead run's decision artifact.
    DECISION = "decision"
    #: The orchestrator's finding-promotion lane.
    PROMOTION = "promotion"


class CharterProposalLifecycle(str, Enum):
    """What became of an action that went through the approval gate."""

    AWAITING_APPROVAL = "awaiting_approval"
    #: Approved, and the stored op executed after re-validation.
    APPROVED_APPLIED = "approved_applied"
    #: Approved, but re-validation found its preconditions no longer held.
    APPROVED_STALE = "approved_stale"
    #: The proposal issue closed without being approved.
    DECLINED = "declined"


class CharterExecutionResult(str, Enum):
    """What the applier did with an action the charter let run directly (#7362).

    Linked back after the attempt, by the same owner that links a gated
    proposal's approval. Until then an executed decision has no result, and
    nothing reads it as having taken effect.
    """

    #: The applier committed the effect.
    APPLIED = "applied"
    #: The applier's own re-validation refused it before any write (a typed
    #: refusal or stale precondition).
    REFUSED = "refused"
    #: The applier tried and failed, or its result is unknown (the apply raised).
    FAILED = "failed"
    #: Never attempted: its completion withheld it, and the record's reason
    #: says why (a mandated action that did not commit, a subject refused
    #: earlier in the completion, or a raise that stopped the apply).
    WITHHELD = "withheld"
    #: The action liveness owner stopped retrying it (#7350).
    PARKED = "parked"


@dataclass(frozen=True)
class TechLeadCharterDecision:
    """One tech-lead action and the charter decision that allowed or downgraded it."""

    #: Stable identity: ``decision_key(run_id, action_id)`` or a promotion key.
    decision_id: str
    source: CharterDecisionSource
    #: The deciding tech-lead run (empty for orchestrator-originated kinds).
    run_id: str
    #: The decision artifact's action id (``A<n>``), or the promotion signature.
    action_id: str
    #: The tech-lead run's anchor issue (or the case file, for a promotion).
    anchor_issue_number: int
    #: The issue/PR the action targets; None when it targets none (a new issue).
    target_number: int | None
    target_is_pr: bool
    action_kind: str
    role: CharterRole
    required_depth: CharterDepth
    binding: CharterBinding
    #: The role's dials at decision time.
    role_enabled: bool
    role_depth: CharterDepth
    role_authority: CharterAuthority
    #: The per-action ceiling at decision time, and the setting it came from.
    action_ceiling: CharterAuthority
    ceiling_source: str
    outcome: CharterOutcome
    reason_code: CharterReason
    reason: str
    decided_at: str
    lifecycle: CharterProposalLifecycle | None = None
    lifecycle_updated_at: str | None = None
    #: The gated proposal issue, once known.
    proposal_issue_number: int | None = None
    #: Set when this action coalesced into an earlier same-(op, target)
    #: proposal of the SAME run; that action's proposal lifecycle is this one's.
    proposal_origin_action_id: str | None = None
    #: What the applier did with an EXECUTED action (#7362); None until linked.
    execution: CharterExecutionResult | None = None
    #: The applier's words for a result that did not apply (its refusal,
    #: error, or park reason); None when it applied.
    execution_reason: str | None = None
    execution_at: str | None = None

    def __post_init__(self) -> None:
        # What became of a decision is told by the path its verdict took: an
        # approval lifecycle only for a gated verdict, an applier result only
        # for an executed one, so neither can vouch for the other (#7362).
        if self.execution is not None and self.outcome is not CharterOutcome.EXECUTED:
            raise ValueError(
                f"only an executed decision has an execution result, not {self.outcome.value}"
            )
        if self.lifecycle is not None and not self.outcome.awaits_approval:
            raise ValueError(
                f"only a gated decision has a proposal lifecycle, not {self.outcome.value}"
            )

    @classmethod
    def from_verdict(
        cls,
        verdict: CharterVerdict,
        *,
        decision_id: str,
        source: CharterDecisionSource,
        run_id: str,
        action_id: str,
        anchor_issue_number: int,
        target_number: int | None,
        target_is_pr: bool,
        decided_at: str,
        tracks_proposal: bool,
        proposal_issue_number: int | None = None,
        proposal_origin_action_id: str | None = None,
    ) -> "TechLeadCharterDecision":
        """Freeze *verdict* into a record.

        ``tracks_proposal`` is True when the gated action is backed by the
        stored-op proposal lifecycle, whose approval and decline the ledger can
        later link back here; other outcomes carry no lifecycle.
        """
        lifecycle = (
            CharterProposalLifecycle.AWAITING_APPROVAL
            if tracks_proposal and verdict.awaits_approval
            else None
        )
        return cls(
            decision_id=decision_id,
            source=source,
            run_id=run_id,
            action_id=action_id,
            anchor_issue_number=anchor_issue_number,
            target_number=target_number,
            target_is_pr=target_is_pr,
            action_kind=verdict.kind,
            role=verdict.role,
            required_depth=verdict.required_depth,
            binding=verdict.action_class.binding,
            role_enabled=verdict.role_charter.enabled,
            role_depth=verdict.role_charter.depth,
            role_authority=verdict.role_charter.authority,
            action_ceiling=verdict.action_ceiling,
            ceiling_source=verdict.ceiling_source,
            outcome=verdict.outcome,
            reason_code=verdict.reason_code,
            reason=verdict.reason,
            decided_at=decided_at,
            lifecycle=lifecycle,
            lifecycle_updated_at=decided_at if lifecycle is not None else None,
            proposal_issue_number=proposal_issue_number,
            proposal_origin_action_id=proposal_origin_action_id,
        )

    def is_about_issue(self, issue_number: int) -> bool:
        """Whether this decision says something about *issue_number* (#7331).

        Aimed at it, or taken by a run anchored on it without another target
        (a follow-up issue filed for it). A decision an anchored run took about
        a DIFFERENT issue is not about the anchor.
        """
        return self.target_number == issue_number or (
            self.target_number is None and self.anchor_issue_number == issue_number
        )

    @property
    def is_remedy(self) -> bool:
        """Whether the action meant to MOVE its target (not advice or a floor)."""
        return self.binding in (CharterBinding.APPROVABLE, CharterBinding.DESTRUCTIVE)

    @property
    def took_effect(self) -> bool:
        """Whether the action ran: its applier committed it, or it was approved and applied.

        The verdict alone is not enough (#7362): an executed decision whose
        applier refused, failed, withheld or parked it, or whose result is not
        linked yet, did not take effect.
        """
        return (
            self.outcome is CharterOutcome.EXECUTED
            and self.execution is CharterExecutionResult.APPLIED
        ) or self.lifecycle is CharterProposalLifecycle.APPROVED_APPLIED

    @property
    def effect(self) -> str:
        """What became of the action, in one word: the applier's linked result
        for an executed decision (``applied``, ``withheld``, ...), the proposal
        lifecycle for a gated one, ``unlinked`` until either is recorded, and
        ``advice`` for advice-only."""
        if self.execution is not None:
            return self.execution.value
        if self.lifecycle is not None:
            return self.lifecycle.value
        if self.outcome is CharterOutcome.ADVICE_ONLY:
            return "advice"
        return "unlinked"

    @property
    def effect_at(self) -> str:
        """When it took effect (or last tried to): as linked, else its decision."""
        if self.lifecycle is CharterProposalLifecycle.APPROVED_APPLIED and self.lifecycle_updated_at:
            return self.lifecycle_updated_at
        if self.execution is not None and self.execution_at:
            return self.execution_at
        return self.decided_at

    def with_execution(
        self, link: "CharterExecutionLink", *, at: str
    ) -> "TechLeadCharterDecision":
        """Link what the applier did with this executed action (#7362)."""
        if link.decision_id != self.decision_id:
            raise ValueError(f"link for {link.decision_id} applied to {self.decision_id}")
        return replace(
            self, execution=link.result, execution_reason=link.reason, execution_at=link.at or at
        )

    def with_lifecycle(
        self,
        lifecycle: CharterProposalLifecycle,
        *,
        at: str,
        proposal_issue_number: int,
    ) -> "TechLeadCharterDecision":
        return replace(
            self,
            lifecycle=lifecycle,
            lifecycle_updated_at=at,
            proposal_issue_number=proposal_issue_number,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "source": self.source.value,
            "run_id": self.run_id,
            "action_id": self.action_id,
            "anchor_issue_number": self.anchor_issue_number,
            "target_number": self.target_number,
            "target_is_pr": self.target_is_pr,
            "action_kind": self.action_kind,
            "role": self.role.value,
            "required_depth": self.required_depth.value,
            "binding": self.binding.value,
            "role_enabled": self.role_enabled,
            "role_depth": self.role_depth.value,
            "role_authority": self.role_authority.value,
            "action_ceiling": self.action_ceiling.value,
            "ceiling_source": self.ceiling_source,
            "outcome": self.outcome.value,
            "reason_code": self.reason_code.value,
            "reason": self.reason,
            "decided_at": self.decided_at,
            "lifecycle": self.lifecycle.value if self.lifecycle else None,
            "lifecycle_updated_at": self.lifecycle_updated_at,
            "proposal_issue_number": self.proposal_issue_number,
            "proposal_origin_action_id": self.proposal_origin_action_id,
            "execution": self.execution.value if self.execution else None,
            "execution_reason": self.execution_reason,
            "execution_at": self.execution_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TechLeadCharterDecision":
        """Rehydrate a stored record; unknown enum values raise (orchestrator-owned)."""
        lifecycle = data.get("lifecycle")
        execution = data.get("execution")
        target = data.get("target_number")
        proposal = data.get("proposal_issue_number")
        return cls(
            decision_id=str(data["decision_id"]),
            source=CharterDecisionSource(data["source"]),
            run_id=str(data["run_id"]),
            action_id=str(data["action_id"]),
            anchor_issue_number=int(data["anchor_issue_number"]),
            target_number=int(target) if target is not None else None,
            target_is_pr=bool(data["target_is_pr"]),
            action_kind=str(data["action_kind"]),
            role=CharterRole(data["role"]),
            required_depth=CharterDepth(data["required_depth"]),
            binding=CharterBinding(data["binding"]),
            role_enabled=bool(data["role_enabled"]),
            role_depth=CharterDepth(data["role_depth"]),
            role_authority=CharterAuthority(data["role_authority"]),
            action_ceiling=CharterAuthority(data["action_ceiling"]),
            ceiling_source=str(data["ceiling_source"]),
            outcome=CharterOutcome(data["outcome"]),
            reason_code=CharterReason(data["reason_code"]),
            reason=str(data["reason"]),
            decided_at=str(data["decided_at"]),
            lifecycle=CharterProposalLifecycle(lifecycle) if lifecycle else None,
            lifecycle_updated_at=data.get("lifecycle_updated_at"),
            proposal_issue_number=int(proposal) if proposal is not None else None,
            proposal_origin_action_id=data.get("proposal_origin_action_id"),
            execution=CharterExecutionResult(execution) if execution else None,
            execution_reason=data.get("execution_reason"),
            execution_at=data.get("execution_at"),
        )


@dataclass(frozen=True)
class CharterExecutionLink:
    """One executed decision's applier result, to link back onto its record (#7362)."""

    decision_id: str
    result: CharterExecutionResult
    #: The applier's words for a result that did not apply; None when it applied.
    reason: str | None = None
    #: When the result landed, if the linker knows it more precisely than the
    #: link's own time (a completion links a whole batch at once, in order).
    at: str | None = None

    def __post_init__(self) -> None:
        if not self.decision_id:
            raise ValueError("an execution link names its decision")
        if (self.result is CharterExecutionResult.APPLIED) != (self.reason is None):
            raise ValueError("an applied result has no reason; every other result needs one")


def decision_key(run_id: str, action_id: str) -> str:
    """Identity of a run's decided action: one record per (run, action id)."""
    if not run_id or not action_id:
        raise ValueError("a charter decision key needs a run id and an action id")
    return f"{CharterDecisionSource.DECISION.value}:{run_id}:{action_id}"


def promotion_decision_key(signature: str) -> str:
    """Identity of a promotion decision: one record per pattern signature."""
    if not signature:
        raise ValueError("a promotion charter decision key needs a signature")
    return f"{CharterDecisionSource.PROMOTION.value}:{signature}"
