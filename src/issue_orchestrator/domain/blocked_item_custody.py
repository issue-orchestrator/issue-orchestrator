"""Custody of a blocked item: who owns it now, why, and for how long (#7331).

A blocked card on the board used to say only "blocked". Whether nobody had
looked at it, the tech lead was mid-investigation, a proposal was waiting on
the operator, or a remedy had just been applied all looked the same. This
module is the vocabulary the ONE custody owner
(:mod:`..control.blocked_item_custody`) answers in: a :class:`CustodyState`,
the plain-words reason, the clock that measures time in that state, whether
that time is past the state's staleness threshold, and — when a tech-lead
charter decision put the item there — that decision as it was recorded.

Pure values. Nothing here derives a state: the owner does, from facts the
engine already holds, and every surface (dashboard card, header count, issue
drawer) renders what it derived. That is what keeps two surfaces from ever
disagreeing about the same item.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from typing import TYPE_CHECKING, Mapping

if TYPE_CHECKING:
    from .tech_lead_charter_decisions import TechLeadCharterDecision


class CustodyState(str, Enum):
    """Who is responsible for a blocked item right now."""

    #: Nobody. Always needs attention, whatever its age.
    UNOWNED = "unowned"
    #: In the tech lead's queue (a pending investigation or a stuck-sweep
    #: recovery that has not run yet).
    QUEUED_FOR_TECH_LEAD = "queued_for_tech_lead"
    #: A tech-lead session is analysing it now.
    INVESTIGATING = "investigating"
    #: The operator has to act: a proposal awaits approval, or someone asked
    #: for a human.
    WAITING_ON_YOU = "waiting_on_you"
    #: A coder, reviewer, recovery publication or tracked fix is working on it.
    BEING_FIXED = "being_fixed"
    #: The environment has to move first: a provider outage, a rate limit, CI,
    #: or a dependency.
    WAITING_ON_WORLD = "waiting_on_world"
    #: Parked or escalated on purpose, by a policy or by a person.
    HELD = "held"
    #: A remedy ran; nothing yet confirms it worked.
    VERIFY = "verify"

    @property
    def label(self) -> str:
        """Operator-facing name. One owner, so no surface invents another."""
        return _LABELS[self]

    @property
    def owner(self) -> str:
        """Who holds custody in this state, in words."""
        return _OWNERS[self]


_LABELS: Mapping[CustodyState, str] = {
    CustodyState.UNOWNED: "Unowned",
    CustodyState.QUEUED_FOR_TECH_LEAD: "Queued for tech lead",
    CustodyState.INVESTIGATING: "Investigating",
    CustodyState.WAITING_ON_YOU: "Waiting on you",
    CustodyState.BEING_FIXED: "Being fixed",
    CustodyState.WAITING_ON_WORLD: "Waiting on world",
    CustodyState.HELD: "Held",
    CustodyState.VERIFY: "Verify",
}

_OWNERS: Mapping[CustodyState, str] = {
    CustodyState.UNOWNED: "nobody",
    CustodyState.QUEUED_FOR_TECH_LEAD: "tech lead (pending)",
    CustodyState.INVESTIGATING: "tech lead (active)",
    CustodyState.WAITING_ON_YOU: "you",
    CustodyState.BEING_FIXED: "an agent or PR",
    CustodyState.WAITING_ON_WORLD: "the environment",
    CustodyState.HELD: "a policy or a person",
    CustodyState.VERIFY: "tech lead",
}

#: Every state that has an owner, and so a staleness threshold. UNOWNED has
#: none: it needs attention at any age.
OWNED_CUSTODY_STATES: tuple[CustodyState, ...] = tuple(
    state for state in CustodyState if state is not CustodyState.UNOWNED
)


@dataclass(frozen=True)
class CustodyStaleThresholds:
    """How long an item may sit in each owned state before it is stale.

    Complete by construction: a state added to :class:`CustodyState` without a
    threshold fails here, at startup, rather than rendering as never stale.
    """

    by_state: Mapping[CustodyState, timedelta]

    def __post_init__(self) -> None:
        missing = [s.value for s in OWNED_CUSTODY_STATES if s not in self.by_state]
        if missing:
            raise ValueError(f"custody stale thresholds missing state(s): {missing}")
        if CustodyState.UNOWNED in self.by_state:
            raise ValueError("UNOWNED has no staleness threshold: it always needs attention")
        bad = [s.value for s, limit in self.by_state.items() if limit <= timedelta(0)]
        if bad:
            raise ValueError(f"custody stale thresholds must be positive: {bad}")

    def for_state(self, state: CustodyState) -> timedelta:
        return self.by_state[state]


@dataclass(frozen=True)
class CustodyClock:
    """When the item entered its custody state, and what that time measures.

    ``basis`` says which fact the time comes from ("tech-lead session
    started", "proposal filed"). ``lower_bound`` is True when the fact can
    only bound the entry time from one side — the issue's last activity, for a
    label nobody timestamps — so the true time in state is at LEAST the age
    shown. Staleness judged on a lower bound can never over-flag.
    """

    since: datetime
    basis: str
    lower_bound: bool = False

    def __post_init__(self) -> None:
        if self.since.tzinfo is None:
            raise ValueError("a custody clock needs an aware timestamp")


@dataclass(frozen=True)
class CustodyCharterBasis:
    """The recorded charter decision that put an item in its state (#7330).

    Copied from the persisted :class:`TechLeadCharterDecision` exactly as
    decided; nothing is recomputed against today's charter, which may have
    changed since.
    """

    decision_id: str
    run_id: str
    action_kind: str
    role: str
    required_depth: str
    role_enabled: bool
    role_depth: str
    role_authority: str
    action_ceiling: str
    ceiling_source: str
    outcome: str
    reason_code: str
    reason: str
    decided_at: str
    lifecycle: str | None
    proposal_issue_number: int | None

    @classmethod
    def from_decision(cls, decision: "TechLeadCharterDecision") -> "CustodyCharterBasis":
        return cls(
            decision_id=decision.decision_id,
            run_id=decision.run_id,
            action_kind=decision.action_kind,
            role=decision.role.value,
            required_depth=decision.required_depth.value,
            role_enabled=decision.role_enabled,
            role_depth=decision.role_depth.value,
            role_authority=decision.role_authority.value,
            action_ceiling=decision.action_ceiling.value,
            ceiling_source=decision.ceiling_source,
            outcome=decision.outcome.value,
            reason_code=decision.reason_code.value,
            reason=decision.reason,
            decided_at=decision.decided_at,
            lifecycle=decision.lifecycle.value if decision.lifecycle else None,
            proposal_issue_number=decision.proposal_issue_number,
        )


@dataclass(frozen=True)
class BlockedItemCustody:
    """One blocked item's custody, as derived by the owner."""

    issue_number: int
    state: CustodyState
    #: Plain words: why the item is in this state.
    reason: str
    #: None when no fact dates the state; the age is then shown as unknown and
    #: the item is never judged stale on a guess.
    clock: CustodyClock | None
    #: The state's threshold; None for UNOWNED, which has none.
    stale_after: timedelta | None
    stale: bool
    charter: CustodyCharterBasis | None = None

    def __post_init__(self) -> None:
        if not self.reason.strip():
            raise ValueError("every custody state carries a reason")
        if (self.state is CustodyState.UNOWNED) != (self.stale_after is None):
            raise ValueError("only UNOWNED lacks a staleness threshold")
        if self.stale and (self.clock is None or self.stale_after is None):
            raise ValueError("an item is stale only against a clock and a threshold")

    @property
    def needs_attention(self) -> bool:
        """The board's "is it under control?" test: unowned, or stale."""
        return self.state is CustodyState.UNOWNED or self.stale


@dataclass(frozen=True)
class BlockedCustodyBoard:
    """Every blocked item's custody, plus the one number the header shows."""

    items: tuple[BlockedItemCustody, ...]

    def for_issue(self, issue_number: int) -> BlockedItemCustody:
        for item in self.items:
            if item.issue_number == issue_number:
                return item
        raise KeyError(f"no custody derived for blocked issue #{issue_number}")

    @property
    def needs_attention_count(self) -> int:
        return sum(1 for item in self.items if item.needs_attention)

    @property
    def unowned_count(self) -> int:
        return sum(1 for item in self.items if item.state is CustodyState.UNOWNED)

    @property
    def stale_count(self) -> int:
        return sum(1 for item in self.items if item.stale)

    def count(self, state: CustodyState) -> int:
        return sum(1 for item in self.items if item.state is state)


def age_of(clock: CustodyClock, now: datetime) -> timedelta:
    """Time in state; never negative (a clock slightly ahead reads as zero)."""
    return max(timedelta(0), now - clock.since)


__all__ = [
    "OWNED_CUSTODY_STATES",
    "BlockedCustodyBoard",
    "BlockedItemCustody",
    "CustodyCharterBasis",
    "CustodyClock",
    "CustodyStaleThresholds",
    "CustodyState",
    "age_of",
]
