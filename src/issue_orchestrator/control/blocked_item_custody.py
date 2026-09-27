"""The ONE owner of blocked-item custody (#7331): who owns each item, and why.

"Is the board under control?" is a question about every blocked item: has
anything picked it up, and is it moving? This module answers it from facts
the engine already holds — the tech lead's queue and live sessions, the
approval backlog, the charter decision ledger (#7330), the shared needs-human
block's recorded causes, the stuck sweep's budget, the rate-limit window and
provider circuits, dependency and CI waits, and the liveness owner's parked
actions (#7350). It keeps no bookkeeping of its own.

Two halves, one owner:

* :func:`derive_item_custody` is the POLICY — a pure, ordered rule table from
  one item's facts to a :class:`BlockedItemCustody`. The first rule that claims
  the item decides its state; UNOWNED is what is left when none does.
* :mod:`.blocked_item_custody_reader` GATHERS those facts from the engine's
  owners and calls the policy. Nothing else derives custody: the dashboard
  renders what this returns, so a card, the header count and the issue drawer
  can never disagree.

Fail visible, never hide. An item whose facts could not all be read, or whose
labels were not observed when a label decides the answer, is UNOWNED with the
reason "custody unknown" — it counts toward the attention number instead of
borrowing a state it may not be in.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Callable

from ..domain.blocked_item_custody import (
    BlockedItemCustody,
    CustodyCharterBasis,
    CustodyClock,
    CustodyStaleThresholds,
    CustodyState,
    age_of,
)
from ..domain.human_block import NeedsHumanCause
from ..domain.tech_lead_charter import CharterBinding, CharterOutcome, CharterReason
from ..domain.tech_lead_charter_decisions import CharterProposalLifecycle

if TYPE_CHECKING:
    from ..domain.tech_lead_charter_decisions import TechLeadCharterDecision
    from ..ports.blocked_item_custody import ParkedActionFact

CUSTODY_UNKNOWN = "custody unknown"


# ---------------------------------------------------------------------------
# Facts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ObservedLabels:
    """The item's labels as this refresh observed them, interpreted once."""

    blocking: tuple[str, ...]
    needs_human: bool = False
    #: The tech lead's own needs-human provenance marker.
    tech_lead_escalated: bool = False
    provider_unavailable: bool = False
    cross_milestone: bool = False
    recovery_pending: bool = False


@dataclass(frozen=True)
class ActiveWork:
    """Work in flight on the item: what it is, and since when (if known)."""

    what: str
    since: datetime | None = None


@dataclass(frozen=True)
class OpenProposal:
    """A gated proposal targeting the item that awaits operator approval."""

    proposal_issue_number: int
    op_type: str
    created_at: datetime | None


@dataclass(frozen=True)
class TrackedFix:
    """A completed investigation parked the item on an open fix tracker (#6971)."""

    tracker_issue_number: int
    recorded_at: datetime | None
    reassess_at: datetime


@dataclass(frozen=True)
class ProviderWait:
    """The item's provider circuit is open; the resilience manager resumes it."""

    provider: str
    open_until: datetime | None
    since: datetime | None


@dataclass(frozen=True)
class RateLimitWait:
    """The repository host's rate-limit window is holding launches back (#7297)."""

    resets_at: datetime
    since: datetime


@dataclass(frozen=True)
class StuckSweepSchedule:
    """The stuck sweep's cadence and budget, board-wide (#6823)."""

    enabled: bool
    max_attempts: int
    last_swept_at: datetime | None
    next_due_at: datetime | None


@dataclass(frozen=True)
class ItemCustodyFacts:
    """Everything the policy may consult about one blocked item.

    Collections are "none observed" when empty, and every source that could
    NOT be read names itself in ``unreadable`` — so "nothing found" and
    "could not look" are never the same value.
    """

    issue_number: int
    #: None when this refresh did not observe the issue's labels.
    labels: ObservedLabels | None
    #: The issue's last activity on the host: a lower bound on when a label
    #: nobody timestamps was set.
    last_activity_at: datetime | None = None
    #: When this engine saw its last session on the item end blocked.
    blocked_at: datetime | None = None
    history_status: str | None = None
    tech_lead_session: ActiveWork | None = None
    active_fix: ActiveWork | None = None
    queued_fix: ActiveWork | None = None
    tech_lead_queue: ActiveWork | None = None
    proposals: tuple[OpenProposal, ...] = ()
    #: Newest first, exactly as recorded.
    decisions: tuple["TechLeadCharterDecision", ...] = ()
    needs_human_causes: frozenset[NeedsHumanCause] = frozenset()
    parked: tuple["ParkedActionFact", ...] = ()
    tracked_fix: TrackedFix | None = None
    provider_wait: ProviderWait | None = None
    checks_pending_since: datetime | None = None
    dependency_summary: str | None = None
    #: The stuck sweep's recorded failed-cycle count; None when it holds none.
    sweep_attempts: int | None = None
    sweep_escalation_pending: bool = False
    unreadable: tuple[str, ...] = ()


@dataclass(frozen=True)
class BoardCustodyFacts:
    """Board-wide facts every item's derivation shares."""

    now: datetime
    sweep: StuckSweepSchedule
    rate_limit: RateLimitWait | None = None


# ---------------------------------------------------------------------------
# Policy
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Claim:
    """One rule's answer: the state, why, since when, and the deciding record."""

    state: CustodyState
    reason: str
    clock: CustodyClock | None = None
    charter: CustodyCharterBasis | None = None


_Rule = Callable[[ItemCustodyFacts, BoardCustodyFacts], "_Claim | None"]
_LabelRule = Callable[[ItemCustodyFacts, ObservedLabels, BoardCustodyFacts], "_Claim | None"]


def derive_item_custody(
    item: ItemCustodyFacts,
    board: BoardCustodyFacts,
    thresholds: CustodyStaleThresholds,
) -> BlockedItemCustody:
    """The item's custody: the first rule that claims it, else UNOWNED.

    Rules run in order of "who acts next". Live work outranks everything; an
    open proposal outranks a label, because approving it is the next move; a
    human's attention outranks queued work that cannot launch past the block;
    the environment outranks the tech lead's queue it is holding back.
    """
    if item.unreadable:
        return _unowned(item, f"{CUSTODY_UNKNOWN}: {'; '.join(item.unreadable)}")
    for rule in _LABEL_FREE_RULES:
        claim = rule(item, board)
        if claim is not None:
            return _custody(item, claim, board.now, thresholds)
    labels = item.labels
    if labels is None:
        return _unowned(
            item, f"{CUSTODY_UNKNOWN}: this refresh did not observe the issue's labels"
        )
    for label_rule in _LABEL_RULES:
        claim = label_rule(item, labels, board)
        if claim is not None:
            return _custody(item, claim, board.now, thresholds)
    return _unowned(item, _nobody_reason(item, labels, board))


def _custody(
    item: ItemCustodyFacts,
    claim: _Claim,
    now: datetime,
    thresholds: CustodyStaleThresholds,
) -> BlockedItemCustody:
    limit = thresholds.for_state(claim.state)
    stale = claim.clock is not None and age_of(claim.clock, now) > limit
    return BlockedItemCustody(
        issue_number=item.issue_number,
        state=claim.state,
        reason=claim.reason,
        clock=claim.clock,
        stale_after=limit,
        stale=stale,
        charter=claim.charter,
    )


def _unowned(item: ItemCustodyFacts, reason: str) -> BlockedItemCustody:
    return BlockedItemCustody(
        issue_number=item.issue_number,
        state=CustodyState.UNOWNED,
        reason=reason,
        clock=_blocked_clock(item),
        stale_after=None,
        stale=False,
    )


def _clock(at: datetime | None, basis: str, *, lower_bound: bool = False) -> CustodyClock | None:
    return CustodyClock(since=at, basis=basis, lower_bound=lower_bound) if at else None


def _blocked_clock(item: ItemCustodyFacts) -> CustodyClock | None:
    """How long the item has been blocked with nobody on it, as best known."""
    if item.blocked_at is not None:
        return CustodyClock(since=item.blocked_at, basis="its last session ended")
    return _clock(item.last_activity_at, "last issue activity", lower_bound=True)


# -- 1. live work -------------------------------------------------------------


def _investigating(item: ItemCustodyFacts, board: BoardCustodyFacts) -> _Claim | None:
    del board
    work = item.tech_lead_session
    if work is None:
        return None
    return _Claim(
        CustodyState.INVESTIGATING,
        f"A {work.what} is analysing it now.",
        _clock(work.since, "tech-lead session started"),
    )


def _fix_running(item: ItemCustodyFacts, board: BoardCustodyFacts) -> _Claim | None:
    del board
    work = item.active_fix
    if work is None:
        return None
    return _Claim(
        CustodyState.BEING_FIXED,
        f"A {work.what} is working on it now.",
        _clock(work.since, "session started"),
    )


# -- 2. the operator ------------------------------------------------------------


def _awaiting_approval(item: ItemCustodyFacts, board: BoardCustodyFacts) -> _Claim | None:
    """An open gated proposal on the item: approving it is the next move.

    The op ledger is the authority on what is still open: a row exists from
    filing until approval executes it or cleanup discards it. The charter
    decision that filed it is attached as the reason it was proposed rather
    than run; a decision alone never claims the item, because its lifecycle is
    only as current as the last link-back.
    """
    del board
    if not item.proposals:
        return None
    proposal = min(item.proposals, key=_proposal_age_order)
    decision = next(
        (
            record
            for record in item.decisions
            if record.proposal_issue_number == proposal.proposal_issue_number
        ),
        None,
    ) or next(
        (
            record
            for record in item.decisions
            if record.lifecycle is CharterProposalLifecycle.AWAITING_APPROVAL
            and record.action_kind == proposal.op_type
        ),
        None,
    )
    action = proposal.op_type.replace("_", " ")
    return _Claim(
        CustodyState.WAITING_ON_YOU,
        f"Proposal #{proposal.proposal_issue_number} ({action}) awaits your"
        " approval; remove its proposed-tech-lead label to approve it.",
        _clock(proposal.created_at, "proposal filed"),
        _basis(decision),
    )


#: Needs-human causes that are a request FOR a person's answer.
_ASKS_FOR_A_PERSON: dict[NeedsHumanCause, str] = {
    NeedsHumanCause.TECH_LEAD_ESCALATION: "The tech lead escalated it to you.",
    NeedsHumanCause.AGENT_COMPLETION: "An agent asked for a human (needs-human).",
    NeedsHumanCause.MERGE_ESCALATION: "Its PR was escalated out of the merge lifecycle.",
    NeedsHumanCause.CLAIM_QUARANTINE: (
        "Its run's work claim could not be read; a person must reconcile it."
    ),
    NeedsHumanCause.VALIDATED_WORK_DISPOSITION: (
        "Preserved validated work could not be published; a person must decide what to do with it."
    ),
}


def _asked_for_you(
    item: ItemCustodyFacts, labels: ObservedLabels, board: BoardCustodyFacts
) -> _Claim | None:
    del board
    if not labels.needs_human:
        return None
    causes = set(item.needs_human_causes)
    if labels.tech_lead_escalated:
        causes.add(NeedsHumanCause.TECH_LEAD_ESCALATION)
    for cause, reason in _ASKS_FOR_A_PERSON.items():
        if cause in causes:
            return _Claim(
                CustodyState.WAITING_ON_YOU,
                reason,
                _clock(item.last_activity_at, "last issue activity", lower_bound=True),
            )
    return None


# -- 3. held on purpose -----------------------------------------------------------


def _held(
    item: ItemCustodyFacts, labels: ObservedLabels, board: BoardCustodyFacts
) -> _Claim | None:
    if item.parked:
        parked = min(item.parked, key=lambda fact: fact.parked_since)
        return _Claim(
            CustodyState.HELD,
            f"The orchestrator stopped retrying {parked.action.replace('_', ' ')}"
            f" ({parked.outcome}): {parked.reason}",
            CustodyClock(since=parked.parked_since, basis="action parked"),
        )
    if not labels.needs_human:
        return None
    swept = item.sweep_attempts
    # Only a LANDED escalation is a hold: until needs-human is observed, the
    # sweep is still retrying the label (see _queued_for_tech_lead).
    if item.sweep_escalation_pending or (
        swept is not None and swept >= board.sweep.max_attempts
    ):
        return _Claim(
            CustodyState.HELD,
            f"The stuck sweep spent its {board.sweep.max_attempts} recovery"
            " attempt(s) without unblocking it and escalated it for a person.",
            _clock(board.sweep.last_swept_at, "last stuck sweep"),
        )
    if NeedsHumanCause.SESSION_LIFECYCLE in item.needs_human_causes:
        return _Claim(
            CustodyState.HELD,
            "Escalated by policy: the orchestrator stopped on it and set needs-human.",
            _clock(item.last_activity_at, "last issue activity", lower_bound=True),
        )
    return None


def _needs_human_on_its_own(
    item: ItemCustodyFacts, labels: ObservedLabels, board: BoardCustodyFacts
) -> _Claim | None:
    """A needs-human no orchestrator lifecycle recorded: a person asked for one.

    Checked after the tech lead's own queue, because the stuck sweep treats
    such an issue as eligible for re-examination (#6824 F2): while it is queued
    or being re-checked, that is who holds it.
    """
    if not labels.needs_human:
        return None
    sweep = (
        " The stuck sweep may re-examine it."
        if board.sweep.enabled
        else " The stuck sweep is off, so only a person will."
    )
    return _Claim(
        CustodyState.WAITING_ON_YOU,
        "needs-human is set with no orchestrator cause on record, so a person"
        f" asked for one.{sweep}",
        _clock(item.last_activity_at, "last issue activity", lower_bound=True),
    )


# -- 4. a fix on its way ------------------------------------------------------------


def _fix_pending(
    item: ItemCustodyFacts, labels: ObservedLabels, board: BoardCustodyFacts
) -> _Claim | None:
    if item.queued_fix is not None:
        return _launch_claim(
            CustodyState.BEING_FIXED,
            f"A {item.queued_fix.what} is queued.",
            item.queued_fix.since,
            board,
        )
    if item.tracked_fix is not None:
        fix = item.tracked_fix
        return _Claim(
            CustodyState.BEING_FIXED,
            f"Diagnosed: waiting on fix issue #{fix.tracker_issue_number}"
            f" (reassessed by {fix.reassess_at.isoformat(timespec='minutes')}).",
            _clock(fix.recorded_at, "investigation recorded its disposition"),
        )
    if labels.recovery_pending:
        return _Claim(
            CustodyState.BEING_FIXED,
            "Validated-work recovery is publishing its preserved work.",
            _clock(item.last_activity_at, "last issue activity", lower_bound=True),
        )
    return None


# -- 5. the environment ---------------------------------------------------------------


def _world(
    item: ItemCustodyFacts, labels: ObservedLabels, board: BoardCustodyFacts
) -> _Claim | None:
    del board
    wait = item.provider_wait
    if labels.provider_unavailable and wait is not None:
        until = (
            f"; its circuit retries at {wait.open_until.isoformat(timespec='minutes')}"
            if wait.open_until
            else ""
        )
        return _Claim(
            CustodyState.WAITING_ON_WORLD,
            f"Provider {wait.provider} is unavailable{until}.",
            _clock(wait.since, "provider circuit opened"),
        )
    if item.checks_pending_since is not None:
        return _Claim(
            CustodyState.WAITING_ON_WORLD,
            "Waiting for its PR's required CI checks.",
            CustodyClock(since=item.checks_pending_since, basis="checks pending since"),
        )
    if labels.cross_milestone or item.dependency_summary:
        detail = item.dependency_summary or "a dependency in another milestone"
        return _Claim(
            CustodyState.WAITING_ON_WORLD,
            f"Waiting on a dependency: {detail}.",
            _clock(item.last_activity_at, "last issue activity", lower_bound=True),
        )
    return None


# -- 6. the tech lead's queue ------------------------------------------------------------


def _queued_for_tech_lead(
    item: ItemCustodyFacts, labels: ObservedLabels, board: BoardCustodyFacts
) -> _Claim | None:
    del labels
    if item.tech_lead_queue is not None:
        return _launch_claim(
            CustodyState.QUEUED_FOR_TECH_LEAD,
            f"Queued for a {item.tech_lead_queue.what}.",
            item.tech_lead_queue.since,
            board,
        )
    if item.sweep_escalation_pending:
        # Exhausted, and the needs-human escalation has not landed yet (the
        # hold above claims it once it has): the sweep re-asserts it each run.
        return _Claim(
            CustodyState.QUEUED_FOR_TECH_LEAD,
            "The stuck sweep exhausted its recovery attempts; its escalation to"
            " a person has not landed yet and is retried on every sweep.",
            _clock(board.sweep.last_swept_at, "last stuck sweep"),
        )
    attempts = item.sweep_attempts
    # A recorded budget is only a queue while a sweep will run again: with the
    # sweep off, nothing re-checks the item, and saying so would be a promise.
    if board.sweep.enabled and attempts is not None and attempts < board.sweep.max_attempts:
        return _Claim(
            CustodyState.QUEUED_FOR_TECH_LEAD,
            "The stuck sweep is tracking its recovery (failed cycles"
            f" {attempts} of {board.sweep.max_attempts}); the next sweep"
            " re-checks it.",
            _clock(board.sweep.last_swept_at, "last stuck sweep"),
        )
    return None


def _launch_claim(
    state: CustodyState, reason: str, since: datetime | None, board: BoardCustodyFacts
) -> _Claim:
    """A queued launch, unless the host's rate limit is holding launches back."""
    limit = board.rate_limit
    if limit is not None:
        return _Claim(
            CustodyState.WAITING_ON_WORLD,
            f"{reason} Launches are deferred by a GitHub rate limit until"
            f" {limit.resets_at.isoformat(timespec='minutes')}.",
            CustodyClock(since=limit.since, basis="rate limit hit"),
        )
    return _Claim(state, reason, _clock(since, "queued"))


# -- 7. what the tech lead last decided ------------------------------------------------------


def _latest_remedy(
    item: ItemCustodyFacts, labels: ObservedLabels, board: BoardCustodyFacts
) -> _Claim | None:
    """The newest decision that meant to MOVE the item, and what came of it.

    Advice and floors are not remedies: a comment does not unblock anything,
    and an escalation or a tracker disposition is already visible through the
    needs-human block and the disposition ledger above.
    """
    del labels, board
    decision = next(
        (
            record
            for record in item.decisions
            if record.binding in (CharterBinding.APPROVABLE, CharterBinding.DESTRUCTIVE)
            # Aimed AT this item: a follow-up filed for it (an untargeted
            # create_issue) is not a remedy of its block.
            and record.target_number == item.issue_number
        ),
        None,
    )
    if decision is None:
        return None
    action = decision.action_kind.replace("_", " ")
    if decision.outcome is CharterOutcome.ADVICE_ONLY and decision.reason_code in (
        CharterReason.ROLE_DISABLED,
        CharterReason.BEYOND_DEPTH,
    ):
        return _Claim(
            CustodyState.HELD,
            f"The charter kept the tech lead from acting ({action}); it recorded"
            " advice only.",
            _clock(_parse(decision.decided_at), "charter decision recorded"),
            _basis(decision),
        )
    if decision.outcome is CharterOutcome.EXECUTED or (
        decision.lifecycle is CharterProposalLifecycle.APPROVED_APPLIED
    ):
        applied = _parse(
            (decision.lifecycle_updated_at if decision.lifecycle else None)
            or decision.decided_at
        )
        if applied is None or item.blocked_at is None or applied < item.blocked_at:
            # Only a remedy provably applied to THIS block is verifiable. One
            # that ran before the block began was for an earlier incident, and
            # without a block time nothing ties it to this one: the item falls
            # through to Unowned, whose reason names the remedy it saw.
            return None
        return _Claim(
            CustodyState.VERIFY,
            f"The tech lead applied a remedy ({action}); nothing yet confirms it"
            " unblocked the item.",
            _clock(applied, "remedy applied"),
            _basis(decision),
        )
    return None


def _nobody_reason(
    item: ItemCustodyFacts, labels: ObservedLabels, board: BoardCustodyFacts
) -> str:
    if labels.blocking:
        what = f"Blocked by {', '.join(labels.blocking)}"
    elif item.history_status:
        what = f"Its last session ended {item.history_status.replace('_', ' ')}"
    else:
        what = "Blocked"
    return f"{what}; nothing has picked it up. {_untied_remedy(item)}{_sweep_hint(board)}"


def _untied_remedy(item: ItemCustodyFacts) -> str:
    """Name a remedy the tech lead applied that nothing ties to this block."""
    decision = next(
        (
            record
            for record in item.decisions
            if record.target_number == item.issue_number
            and (
                record.outcome is CharterOutcome.EXECUTED
                or record.lifecycle is CharterProposalLifecycle.APPROVED_APPLIED
            )
            and record.binding in (CharterBinding.APPROVABLE, CharterBinding.DESTRUCTIVE)
        ),
        None,
    )
    if decision is None:
        return ""
    return (
        f"The tech lead last applied {decision.action_kind.replace('_', ' ')} at"
        f" {decision.decided_at}, but nothing ties that remedy to this block. "
    )


def _sweep_hint(board: BoardCustodyFacts) -> str:
    sweep = board.sweep
    if not sweep.enabled:
        return "The stuck sweep is off, so nothing will pick it up on its own."
    if sweep.next_due_at is None or sweep.next_due_at <= board.now:
        return "The stuck sweep is due now and re-examines blocked items on its next tick."
    return (
        "The next stuck sweep is due at"
        f" {sweep.next_due_at.isoformat(timespec='minutes')}."
    )


#: Rules that need no labels: live work and the approval ledger are certain
#: whatever the labels say.
_LABEL_FREE_RULES: tuple[_Rule, ...] = (
    _investigating,
    _fix_running,
    _awaiting_approval,
)

#: Past this point labels decide, so an item whose labels were not observed is
#: "custody unknown" rather than guessed into a state.
_LABEL_RULES: tuple[_LabelRule, ...] = (
    _asked_for_you,
    _held,
    _fix_pending,
    _world,
    _queued_for_tech_lead,
    _needs_human_on_its_own,
    _latest_remedy,
)


def _basis(decision: "TechLeadCharterDecision | None") -> CustodyCharterBasis | None:
    return CustodyCharterBasis.from_decision(decision) if decision is not None else None


def _parse(value: str) -> datetime | None:
    """A recorded ISO timestamp; None (age unknown) when it will not parse."""
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else None


def _proposal_age_order(proposal: OpenProposal) -> tuple[bool, float, int]:
    """Oldest dated proposal first; undated ones after, by issue number."""
    at = proposal.created_at
    return (at is None, at.timestamp() if at else 0.0, proposal.proposal_issue_number)


__all__ = [
    "CUSTODY_UNKNOWN",
    "ActiveWork",
    "BoardCustodyFacts",
    "ItemCustodyFacts",
    "ObservedLabels",
    "OpenProposal",
    "ProviderWait",
    "RateLimitWait",
    "StuckSweepSchedule",
    "TrackedFix",
    "derive_item_custody",
]
