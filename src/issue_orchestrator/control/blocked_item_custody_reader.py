"""Gather each blocked item's custody facts from the engine's owners (#7331).

The fact-gathering half of the custody owner (the policy half is
:mod:`.blocked_item_custody`). Every source here is ALREADY owned elsewhere;
this module only asks each owner its read-only question and hands the answers
to :func:`~.blocked_item_custody.derive_item_custody`:

========================  ====================================================
Fact                      Owner asked
========================  ====================================================
labels, last activity     the tick's cached issue snapshot (no GitHub read)
live and queued work      orchestrator state: sessions, pending queues
tech lead's queue         pending tech-lead reviews, discovered failures
approval backlog          the tech-lead authority store's op ledger
charter decisions         the charter decision ledger (#7330)
needs-human causes        the shared needs-human block's records
fix trackers              the disposition ledger (#6971), read only
stuck-sweep budget        the sweep's durable counters and cadence (#6823)
provider circuits         the provider availability policy (#6824 F2)
rate limit                the host rate-limit window (#7297)
CI and dependencies       state's checks-pending clock and dependency problems
parked actions            the action liveness owner (#7350) via its seam
========================  ====================================================

It makes no GitHub call and writes nothing, so the dashboard can ask on every
refresh. A source that raises is named on the item (``unreadable``), which the
policy turns into "custody unknown" — never into a guessed state.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Callable, Iterable, Mapping, Sequence, TypeVar

from ..domain.blocked_item_custody import BlockedCustodyBoard, CustodyStaleThresholds
from ..domain.session_key import TaskKind
from .blocked_item_custody import (
    ActiveWork,
    BoardCustodyFacts,
    ItemCustodyFacts,
    ObservedLabels,
    OpenProposal,
    ProviderWait,
    RateLimitWait,
    StuckSweepSchedule,
    TrackedFix,
    derive_item_custody,
)
from .tech_lead_session_policy import is_tech_lead_session

if TYPE_CHECKING:
    from ..domain.human_block import NeedsHumanCause
    from ..domain.models import OrchestratorState, Session, SessionHistoryEntry
    from ..ports.issue import Issue
    from ..domain.tech_lead_charter_decisions import TechLeadCharterDecision
    from ..infra.config import Config
    from .orchestrator_deps import OrchestratorDeps
    from ..ports.blocked_item_custody import ParkedActionReader
    from ..ports.provider_resilience import ProviderCircuitStatusReader
    from ..ports.tech_lead_authority import TechLeadAuthorityStore
    from .label_manager import LabelManager

logger = logging.getLogger(__name__)

T = TypeVar("T")

#: How many recorded decisions per item the policy may consult. The newest
#: few decide; a longer history changes nothing it reads.
DECISIONS_PER_ITEM = 20

#: What a live session is doing, in the words a card shows. Keyed on the task
#: kind the session was launched as; tech-lead sessions are recognised
#: separately (see :func:`_session_work`).
_FIX_WORK: Mapping[TaskKind, str] = {
    TaskKind.CODE: "coding session",
    TaskKind.REWORK: "rework session",
    TaskKind.REVIEW: "code review",
    TaskKind.RETROSPECTIVE_REVIEW: "retrospective review",
    TaskKind.TECH_LEAD: "tech-lead session",
}


@dataclass(frozen=True)
class _SessionWork:
    tech_lead: bool
    what: str
    issues: frozenset[int]
    since: datetime


def _session_work(session: "Session", tech_lead_agent: str | None) -> _SessionWork:
    """What one live session is doing, and to which issues.

    THE one place custody identifies a tech-lead session. Tech leads launch as
    ``TaskKind.CODE`` today and are recognised by their launch scope or agent
    label, exactly as the rest of the engine does; #7347 replaces that with one
    authoritative ``SessionKind``, and only this function changes when it lands.
    """
    scope = session.tech_lead_scope
    since = _aware(session.started_at)
    if scope is not None or is_tech_lead_session(tech_lead_agent, session.agent_label):
        issues = {session.issue.number}
        if scope is not None:
            issues.update(scope.problem_issue_numbers)
        return _SessionWork(True, "tech-lead session", frozenset(issues), since)
    return _SessionWork(
        False, _FIX_WORK[session.key.task], frozenset({session.issue.number}), since
    )


class StateBlockedItemCustodyReader:
    """The engine's :class:`~..ports.blocked_item_custody.BlockedItemCustodyReader`."""

    def __init__(
        self,
        *,
        config: "Config",
        state: Callable[[], "OrchestratorState"],
        labels: "LabelManager",
        authority: "TechLeadAuthorityStore",
        needs_human_causes: Callable[
            [Sequence[int]], Mapping[int, "frozenset[NeedsHumanCause]"]
        ],
        provider_lanes: Callable[[str | None], tuple[str, ...]],
        provider_circuits: "ProviderCircuitStatusReader",
        parked_actions: "ParkedActionReader",
        clock: Callable[[], datetime],
    ) -> None:
        self._config = config
        self._state = state
        self._labels = labels
        self._authority = authority
        self._needs_human_causes = needs_human_causes
        self._provider_lanes = provider_lanes
        self._provider_circuits = provider_circuits
        self._parked = parked_actions
        self._clock = clock
        # Validated at composition, so a bad threshold fails startup, not a render.
        self._thresholds: CustodyStaleThresholds = (
            config.tech_lead.custody.stale_after_minutes.to_thresholds()
        )

    def read(self, issue_numbers: Sequence[int]) -> BlockedCustodyBoard:
        now = self._clock()
        state = self._state()
        board = BoardCustodyFacts(
            now=now,
            sweep=self._sweep_schedule(state),
            rate_limit=_rate_limit(state, now),
        )
        shared = self._shared_facts(state, issue_numbers)
        return BlockedCustodyBoard(
            items=tuple(
                derive_item_custody(
                    self._item_facts(number, state, shared, now), board, self._thresholds
                )
                for number in issue_numbers
            )
        )

    # -- per item ---------------------------------------------------------------

    def _item_facts(
        self,
        number: int,
        state: "OrchestratorState",
        shared: "_SharedFacts",
        now: datetime,
    ) -> ItemCustodyFacts:
        unreadable = list(shared.unreadable)
        decisions = _guard(unreadable, "charter decision ledger", lambda: self._decisions(number), ())
        causes = shared.needs_human_causes.get(number, frozenset())
        parked = _guard(
            unreadable, "action liveness owner", lambda: self._parked.parked_for_issue(number), ()
        )
        issue = shared.issues.get(number)
        labels = self._observed_labels(issue.labels) if issue is not None else None
        provider = (
            _guard(
                unreadable, "provider circuits", lambda: self._provider_wait(issue.agent_type), None
            )
            if labels is not None and labels.provider_unavailable and issue is not None
            else None
        )
        sweep = state.recovery_attempts.get(number)
        problem = state.dependency_problems.get(number)
        checks = state.awaiting_merge_checks_pending_since.get(number)
        history = shared.history.get(number)
        return ItemCustodyFacts(
            issue_number=number,
            labels=labels,
            last_activity_at=_parse_iso(issue.updated_at) if issue is not None else None,
            blocked_at=_aware(history.completed_at) if history and history.completed_at else None,
            history_status=history.status if history else None,
            tech_lead_session=shared.investigations.get(number),
            active_fix=shared.fixes.get(number),
            queued_fix=shared.queued_fixes.get(number),
            tech_lead_queue=shared.tech_lead_queue.get(number),
            proposals=shared.proposals.get(number, ()),
            decisions=decisions,
            needs_human_causes=causes,
            parked=parked,
            tracked_fix=shared.tracked_fixes.get(number),
            provider_wait=provider,
            checks_pending_since=_epoch(checks),
            dependency_summary=problem.summary if problem is not None else None,
            sweep_attempts=sweep,
            sweep_escalation_pending=number in state.pending_stuck_sweep_escalations,
            unreadable=tuple(unreadable),
        )

    def _decisions(self, number: int) -> tuple["TechLeadCharterDecision", ...]:
        """Decisions ABOUT this item, filtered by the ledger before its limit."""
        return self._authority.charter_ledger.list_about_issue(
            number, limit=DECISIONS_PER_ITEM
        )

    def _observed_labels(self, raw: Iterable[str]) -> ObservedLabels:
        lm = self._labels
        names = tuple(raw)
        folded = {name.casefold() for name in names}
        return ObservedLabels(
            blocking=tuple(lm.get_blocking(names)),
            needs_human=lm.requires_human_any(names),
            tech_lead_escalated=lm.tech_lead_needs_human.casefold() in folded,
            provider_unavailable=lm.provider_unavailable.casefold() in folded,
            cross_milestone=lm.blocked_cross_milestone.casefold() in folded,
            recovery_pending=lm.recovery_pending.casefold() in folded,
        )

    def _provider_wait(self, agent_type: str | None) -> ProviderWait | None:
        lanes = self._provider_lanes(agent_type)
        if not lanes:
            return None
        statuses = [
            status
            for status in self._provider_circuits.snapshot(self._clock())
            if status.provider in lanes and status.is_open
        ]
        until = [status.open_until for status in statuses if status.open_until is not None]
        return ProviderWait(
            provider=", ".join(lanes),
            open_until=max(until) if until else None,
            since=min((status.updated_at for status in statuses), default=None),
        )

    def _sweep_schedule(self, state: "OrchestratorState") -> StuckSweepSchedule:
        sweep = self._config.tech_lead.stuck_sweep
        enabled = bool(
            sweep.enabled
            and self._config.tech_lead_enabled
            and self._config.tech_lead_review_on_failure
        )
        last = _epoch(state.last_stuck_sweep_at)
        interval = timedelta(minutes=sweep.interval_minutes)
        return StuckSweepSchedule(
            enabled=enabled,
            max_attempts=sweep.max_recovery_attempts,
            last_swept_at=last,
            next_due_at=(last + interval) if (enabled and last is not None) else None,
        )

    def _shared_facts(
        self, state: "OrchestratorState", issue_numbers: Sequence[int]
    ) -> "_SharedFacts":
        """Facts read once per board, indexed by issue number."""
        unreadable: list[str] = []
        investigations: dict[int, ActiveWork] = {}
        fixes: dict[int, ActiveWork] = {}
        for session in state.active_sessions:
            work = _session_work(session, self._config.tech_lead_review_agent)
            target = investigations if work.tech_lead else fixes
            for number in work.issues:
                target.setdefault(number, ActiveWork(work.what, work.since))
        proposals: dict[int, tuple[OpenProposal, ...]] = _guard(
            unreadable, "approval backlog", lambda: _proposals(self._authority), {}
        )
        causes: Mapping[int, frozenset[NeedsHumanCause]] = _guard(
            unreadable,
            "needs-human causes",
            lambda: self._needs_human_causes(issue_numbers),
            {},
        )
        tracked: dict[int, TrackedFix] = _guard(
            unreadable,
            "disposition ledger",
            lambda: _tracked_fixes(self._authority, self._clock()),
            {},
        )
        return _SharedFacts(
            issues={issue.number: issue for issue in _scope_issues(state)},
            history={entry.issue_number: entry for entry in state.session_history},
            investigations=investigations,
            fixes=fixes,
            queued_fixes=_queued_fixes(state),
            tech_lead_queue=_tech_lead_queue(state),
            proposals=proposals,
            tracked_fixes=tracked,
            needs_human_causes=causes,
            unreadable=tuple(unreadable),
        )


def _guard(unreadable: list[str], source: str, read: Callable[[], T], empty: T) -> T:
    """Read one source; on failure, name it on the item instead of guessing."""
    try:
        return read()
    except Exception as error:
        logger.warning("[CUSTODY] %s unreadable: %s", source, error, exc_info=True)
        unreadable.append(f"{source} could not be read")
        return empty


@dataclass(frozen=True)
class _SharedFacts:
    """Facts read once per board and indexed by issue number."""

    issues: Mapping[int, "Issue"]
    history: Mapping[int, "SessionHistoryEntry"]
    investigations: Mapping[int, ActiveWork]
    fixes: Mapping[int, ActiveWork]
    queued_fixes: Mapping[int, ActiveWork]
    tech_lead_queue: Mapping[int, ActiveWork]
    proposals: Mapping[int, tuple[OpenProposal, ...]]
    tracked_fixes: Mapping[int, TrackedFix]
    needs_human_causes: Mapping[int, "frozenset[NeedsHumanCause]"]
    unreadable: tuple[str, ...]


def _scope_issues(state: "OrchestratorState") -> Sequence["Issue"]:
    """The same issue snapshot the dashboard's blocked lane is built from."""
    return state.cached_scope_issues or state.cached_queue_issues


def _queued_fixes(state: "OrchestratorState") -> dict[int, ActiveWork]:
    queued: dict[int, ActiveWork] = {}
    for retry in state.pending_validation_retries:
        queued.setdefault(
            retry.issue_number,
            ActiveWork(f"validation retry (attempt {retry.retry_count + 1})"),
        )
    for rework in state.pending_reworks:
        if rework.issue_number is not None:
            queued.setdefault(rework.issue_number, ActiveWork("rework of its PR"))
    for review in state.pending_reviews:
        queued.setdefault(review.issue_number, ActiveWork("code review of its PR"))
    return queued


def _tech_lead_queue(state: "OrchestratorState") -> dict[int, ActiveWork]:
    """Every issue a queued tech-lead review or a just-discovered failure covers."""
    queue: dict[int, ActiveWork] = {}
    for item in state.pending_tech_lead_reviews:
        if item.failure is not None:
            queue.setdefault(
                item.failure.issue_number,
                ActiveWork("tech-lead failure investigation", _epoch(item.failure.observed_at)),
            )
        for problem in item.problem_cohort:
            queue.setdefault(
                problem.issue_number,
                ActiveWork("tech-lead health review", _epoch(problem.observed_at)),
            )
    for failure in state.discovered_failures:
        queue.setdefault(
            failure.issue_number,
            ActiveWork("tech-lead failure investigation", _epoch(failure.observed_at)),
        )
    return queue


def _proposals(authority: "TechLeadAuthorityStore") -> dict[int, tuple[OpenProposal, ...]]:
    by_target: dict[int, list[OpenProposal]] = {}
    for proposal_issue, op in authority.list_ops():
        by_target.setdefault(op.target_issue_number, []).append(
            OpenProposal(
                proposal_issue_number=proposal_issue,
                op_type=op.op_type,
                created_at=_parse_iso(op.created_at),
            )
        )
    return {target: tuple(items) for target, items in by_target.items()}


def _tracked_fixes(authority: "TechLeadAuthorityStore", now: datetime) -> dict[int, TrackedFix]:
    """Live disposition bindings, read only (the sweep owns their lifecycle)."""
    return {
        row.issue_number: TrackedFix(
            tracker_issue_number=row.tracker_issue_number,
            recorded_at=_parse_iso(row.recorded_at),
            reassess_at=row.reassess_at,
        )
        for row in authority.list_dispositions()
        if row.phase in {"prepared", "waiting"} and now < row.reassess_at
    }


def _rate_limit(state: "OrchestratorState", now: datetime) -> RateLimitWait | None:
    episode = state.host_rate_limit.holding_at(now)
    if episode is None:
        return None
    return RateLimitWait(resets_at=episode.limit.resets_at, since=episode.limited_since)


def _aware(value: datetime) -> datetime:
    """Session clocks are naive local time; interpret them as such."""
    return value if value.tzinfo is not None else value.astimezone()


def _epoch(value: float | None) -> datetime | None:
    if not value or value <= 0:
        return None
    return datetime.fromtimestamp(value, tz=timezone.utc)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)


def build_blocked_item_custody_reader(
    config: "Config",
    deps: "OrchestratorDeps",
    state: Callable[[], "OrchestratorState"],
) -> StateBlockedItemCustodyReader:
    """Bind the custody owner to the engine's read-only owners.

    ``parked_actions`` is :data:`NO_ACTION_LIVENESS_OWNER` until the action
    liveness owner (#7350) is composed into ``deps``; that is the only line
    that changes when it is.
    """
    from ..ports.blocked_item_custody import NO_ACTION_LIVENESS_OWNER
    from .label_manager import LabelManager
    from .provider_availability import ProviderAvailabilityPolicy

    labels = LabelManager(config)
    providers = ProviderAvailabilityPolicy(
        config,
        deps.provider_resilience,
        labels,
        readiness_probe=deps.provider_readiness_probe,
    )
    return StateBlockedItemCustodyReader(
        config=config,
        state=state,
        labels=labels,
        authority=deps.tech_lead_authority,
        needs_human_causes=deps.needs_human_block.recorded_causes,
        provider_lanes=providers.open_lanes_for_agent,
        provider_circuits=deps.provider_resilience,
        parked_actions=NO_ACTION_LIVENESS_OWNER,
        clock=lambda: datetime.now(timezone.utc),
    )


__all__ = [
    "DECISIONS_PER_ITEM",
    "StateBlockedItemCustodyReader",
    "build_blocked_item_custody_reader",
]
