"""The custody owner's FACT GATHERING: engine owners -> each item's custody (#7331).

Drives :class:`StateBlockedItemCustodyReader` over a real ``OrchestratorState``,
a real in-memory tech-lead authority store (op ledger, charter ledger,
dispositions) and real config, and asserts the custody each item comes out
with. The policy's rule table is covered on its own in
``test_blocked_item_custody.py``; here the question is whether every fact
reaches it from the owner that holds it.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

import pytest

from issue_orchestrator.control.blocked_item_custody_reader import (
    StateBlockedItemCustodyReader,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.domain.blocked_item_custody import CustodyState
from issue_orchestrator.domain.host_rate_limit import HostRateLimit
from issue_orchestrator.domain.human_block import NeedsHumanCause
from issue_orchestrator.domain.models import (
    DependencyProblem,
    DiscoveredFailure,
    Issue,
    OrchestratorState,
    PendingTechLeadReview,
    PendingValidationRetry,
    SessionHistoryEntry,
)
from issue_orchestrator.domain.session_key import TaskKind
from issue_orchestrator.domain.tech_lead_charter import (
    CharterAuthority,
    TechLeadCharter,
    decide_charter,
)
from issue_orchestrator.domain.tech_lead_charter_decisions import (
    CharterDecisionSource,
    TechLeadCharterDecision,
    decision_key,
)
from issue_orchestrator.domain.tech_lead_session import (
    StoredTechLeadOp,
    TechLeadDisposition,
    TechLeadLaunchScope,
    TechLeadSessionFlavor,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.blocked_item_custody import (
    NO_ACTION_LIVENESS_OWNER,
    ParkedActionFact,
)
from issue_orchestrator.ports.provider_resilience import (
    ProviderCircuitStatus,
    StaticProviderCircuitStatusReader,
)
from issue_orchestrator.ports.tech_lead_authority import InMemoryTechLeadAuthorityStore

NOW = datetime(2026, 9, 27, 12, 0, tzinfo=timezone.utc)
HOUR = timedelta(hours=1)
TECH_LEAD = "agent:tech-lead"


def _config() -> Config:
    config = Config()
    config.repo = "test/repo"
    config.tech_lead_review_agent = TECH_LEAD
    config.tech_lead.stuck_sweep.enabled = True
    return config


def _ended_blocked(number: int, at: datetime) -> SessionHistoryEntry:
    """This engine saw the item's last session end blocked at *at*."""
    return SessionHistoryEntry(
        issue_number=number,
        title="t",
        agent_type="agent:web",
        status="failed",
        runtime_minutes=3,
        completed_at=at,
    )


def _blocked(number: int, *labels: str, updated_at: str = "2026-09-27T09:00:00Z") -> Issue:
    return Issue(
        number=number,
        title=f"Issue {number}",
        labels=["agent:web", *labels],
        updated_at=updated_at,
    )


class _Parked:
    def __init__(self, facts: dict[int, tuple[ParkedActionFact, ...]]) -> None:
        self._facts = facts

    def parked_for_issue(self, issue_number: int) -> tuple[ParkedActionFact, ...]:
        return self._facts.get(issue_number, ())


def _causes_reader(
    causes: dict[int, frozenset[NeedsHumanCause]], *, broken: bool
) -> Callable[[Sequence[int]], Mapping[int, frozenset[NeedsHumanCause]]]:
    def read(numbers: Sequence[int]) -> Mapping[int, frozenset[NeedsHumanCause]]:
        if broken:
            raise RuntimeError("cause store locked")
        return {number: causes.get(number, frozenset()) for number in numbers}

    return read


def _reader(
    state: OrchestratorState,
    *,
    config: Config | None = None,
    authority: InMemoryTechLeadAuthorityStore | None = None,
    causes: dict[int, frozenset[NeedsHumanCause]] | None = None,
    broken_causes: bool = False,
    lanes: Callable[[str | None], tuple[str, ...]] = lambda _a: (),
    circuits: tuple[ProviderCircuitStatus, ...] = (),
    parked: _Parked | None = None,
) -> StateBlockedItemCustodyReader:
    config = config or _config()
    return StateBlockedItemCustodyReader(
        config=config,
        state=lambda: state,
        labels=LabelManager(config),
        authority=authority or InMemoryTechLeadAuthorityStore(),
        needs_human_causes=_causes_reader(causes or {}, broken=broken_causes),
        provider_lanes=lanes,
        provider_circuits=StaticProviderCircuitStatusReader(statuses=circuits),
        parked_actions=parked or NO_ACTION_LIVENESS_OWNER,
        clock=lambda: NOW,
    )


def _decision(
    kind: str,
    *,
    target: int | None,
    anchor: int,
    ceiling: CharterAuthority = CharterAuthority.EXECUTE,
    action_id: str = "A1",
    proposal_issue_number: int | None = None,
    tracks_proposal: bool = False,
) -> TechLeadCharterDecision:
    verdict = decide_charter(
        kind,
        TechLeadCharter.default(),
        action_ceiling=ceiling,
        ceiling_source=f"tech_lead.authority.{kind}",
    )
    return TechLeadCharterDecision.from_verdict(
        verdict,
        decision_id=decision_key("run-1", action_id),
        source=CharterDecisionSource.DECISION,
        run_id="run-1",
        action_id=action_id,
        anchor_issue_number=anchor,
        target_number=target,
        target_is_pr=False,
        decided_at=(NOW - HOUR).isoformat(),
        tracks_proposal=tracks_proposal,
        proposal_issue_number=proposal_issue_number,
    )


# -- live sessions: the one place a tech-lead session is recognised ------------


def test_a_tech_lead_session_by_agent_label_is_investigating(make_session) -> None:
    session = replace(
        make_session(issue_number=5), agent_label=TECH_LEAD, started_at=NOW - 30 * timedelta(minutes=1)
    )
    state = OrchestratorState(
        active_sessions=[session], cached_scope_issues=[_blocked(5, "blocked-failed")]
    )

    custody = _reader(state).read([5]).for_issue(5)

    assert custody.state is CustodyState.INVESTIGATING
    assert custody.clock is not None and custody.clock.since == NOW - 30 * timedelta(minutes=1)


def test_a_health_review_cohort_is_investigating_every_problem_it_owns(make_session) -> None:
    review = replace(
        make_session(issue_number=900),
        agent_label=TECH_LEAD,
        started_at=NOW - HOUR,
        tech_lead_scope=TechLeadLaunchScope(
            flavor=TechLeadSessionFlavor.HEALTH_REVIEW, problem_issue_numbers=(11, 12)
        ),
    )
    state = OrchestratorState(
        active_sessions=[review],
        cached_scope_issues=[_blocked(11, "blocked-failed"), _blocked(13, "blocked-failed")],
    )

    board = _reader(state).read([11, 13])

    assert board.for_issue(11).state is CustodyState.INVESTIGATING
    assert board.for_issue(13).state is CustodyState.UNOWNED


def test_a_coding_session_is_being_fixed_not_investigated(make_session) -> None:
    session = replace(make_session(issue_number=6, task=TaskKind.REWORK), started_at=NOW - HOUR)
    state = OrchestratorState(active_sessions=[session], cached_scope_issues=[_blocked(6, "blocked-failed")])

    custody = _reader(state).read([6]).for_issue(6)

    assert custody.state is CustodyState.BEING_FIXED
    assert custody.reason == "A rework session is working on it now."


# -- queues ----------------------------------------------------------------------


def test_queued_investigations_and_discovered_failures_are_queued_for_tech_lead() -> None:
    failure = DiscoveredFailure(
        issue_number=21, issue_title="t", failure_reason="failed", observed_at=(NOW - HOUR).timestamp()
    )
    state = OrchestratorState(
        pending_tech_lead_reviews=[
            PendingTechLeadReview(
                issue_number=21,
                title="t",
                flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
                failure=failure,
            )
        ],
        discovered_failures=[replace(failure, issue_number=22)],
        cached_scope_issues=[_blocked(21, "blocked-failed"), _blocked(22, "blocked-failed")],
    )

    board = _reader(state).read([21, 22])

    for number in (21, 22):
        custody = board.for_issue(number)
        assert custody.state is CustodyState.QUEUED_FOR_TECH_LEAD
        assert custody.clock is not None and custody.clock.since == NOW - HOUR


def test_a_queued_validation_retry_is_being_fixed(tmp_path: Path) -> None:
    retry = PendingValidationRetry(
        issue_number=31,
        issue_title="t",
        agent_label="agent:web",
        worktree_path=str(tmp_path),
        branch_name="b",
        original_prompt=None,
        validation_error="boom",
        validation_error_file=None,
        retry_count=1,
        source_task=TaskKind.CODE,
    )
    state = OrchestratorState(pending_validation_retries=[retry], cached_scope_issues=[_blocked(31, "blocked-failed")])

    custody = _reader(state).read([31]).for_issue(31)

    assert custody.state is CustodyState.BEING_FIXED
    assert custody.reason == "A validation retry (attempt 2) is queued."


def _queued_investigation(number: int) -> PendingTechLeadReview:
    return PendingTechLeadReview(
        issue_number=number,
        title="t",
        flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
        failure=DiscoveredFailure(issue_number=number, issue_title="t", failure_reason="failed"),
    )


def test_a_rate_limit_dates_each_queued_launch_by_its_own_episode() -> None:
    """One item's old refusal must not age another item's fresh wait (#7297)."""
    state = OrchestratorState(
        pending_tech_lead_reviews=[_queued_investigation(23), _queued_investigation(24)],
        cached_scope_issues=[_blocked(23, "blocked-failed"), _blocked(24, "blocked-failed")],
    )
    limit = HostRateLimit(resets_at=NOW + HOUR, kind="primary")
    live = frozenset({"tech_lead:23", "tech_lead:24"})
    state.host_rate_limit.observe(limit, NOW - 100 * timedelta(minutes=1), "tech_lead:23", live=live)
    state.host_rate_limit.observe(limit, NOW - 10 * timedelta(minutes=1), "tech_lead:24", live=live)

    config = _config()
    config.tech_lead.custody.stale_after_minutes.waiting_on_world = 60
    board = _reader(state, config=config).read([23, 24])

    old, fresh = board.for_issue(23), board.for_issue(24)
    assert old.state is fresh.state is CustodyState.WAITING_ON_WORLD
    assert old.clock is not None and old.clock.since == NOW - 100 * timedelta(minutes=1)
    assert old.stale
    assert fresh.clock is not None and fresh.clock.since == NOW - 10 * timedelta(minutes=1)
    assert not fresh.stale


def test_a_launch_past_the_deferral_bound_is_no_longer_waiting_on_the_world() -> None:
    """The window's one hold rule: past the bound the gate attempts the launch."""
    from issue_orchestrator.domain.host_rate_limit import RATE_LIMIT_DEFERRAL_BOUND

    state = OrchestratorState(
        pending_tech_lead_reviews=[_queued_investigation(28)],
        cached_scope_issues=[_blocked(28, "blocked-failed")],
    )
    live = frozenset({"tech_lead:28"})
    state.host_rate_limit.observe(
        HostRateLimit(resets_at=NOW + HOUR, kind="primary"),
        NOW - RATE_LIMIT_DEFERRAL_BOUND - timedelta(minutes=1),
        "tech_lead:28",
        live=live,
    )

    assert state.host_rate_limit.holding(NOW, "tech_lead:28", live=live) is None
    assert state.host_rate_limit.open_at(NOW, "tech_lead:28", live=live) is not None
    custody = _reader(state).read([28]).for_issue(28)
    assert custody.state is CustodyState.QUEUED_FOR_TECH_LEAD
    assert "rate limit" not in custody.reason


def test_a_completed_session_does_not_date_a_later_block() -> None:
    """Only a session that ENDED blocked dates the current block."""
    authority = InMemoryTechLeadAuthorityStore()
    authority.charter_ledger.record_decisions(
        [_decision("recover_validated_work", target=29, anchor=29)]  # decided NOW - 1h
    )
    completed = replace(_ended_blocked(29, NOW - 3 * HOUR), status="completed", pr_url="u")
    state = OrchestratorState(
        cached_scope_issues=[_blocked(29, "blocked-failed", updated_at="2026-09-27T11:30:00Z")],
        session_history=[completed],
    )

    custody = _reader(state, authority=authority).read([29]).for_issue(29)

    assert custody.state is CustodyState.UNOWNED
    assert custody.charter is None


def test_a_just_discovered_failure_is_not_held_by_the_rate_limit() -> None:
    """It is not a launch candidate yet; only queued launches wait on the window."""
    state = OrchestratorState(
        discovered_failures=[
            DiscoveredFailure(issue_number=25, issue_title="t", failure_reason="failed")
        ],
        cached_scope_issues=[_blocked(25, "blocked-failed")],
    )
    state.host_rate_limit.observe(
        HostRateLimit(resets_at=NOW + HOUR, kind="primary"),
        NOW - HOUR,
        "issue:25",
        live=frozenset({"issue:25"}),
    )

    assert _reader(state).read([25]).for_issue(25).state is CustodyState.QUEUED_FOR_TECH_LEAD


def test_a_differently_cased_needs_human_is_the_same_label() -> None:
    state = OrchestratorState(cached_scope_issues=[_blocked(26, "Needs-Human")])
    causes = {26: frozenset({NeedsHumanCause.AGENT_COMPLETION})}

    assert _reader(state, causes=causes).read([26]).for_issue(26).state is (
        CustodyState.WAITING_ON_YOU
    )


def test_advice_recorded_before_a_later_block_does_not_explain_it() -> None:
    from issue_orchestrator.domain.tech_lead_charter import CharterDepth, CharterRole, RoleCharter

    narrow = TechLeadCharter(
        roles={
            **TechLeadCharter.default().roles,
            CharterRole.FLOW: RoleCharter(
                depth=CharterDepth.WORKAROUND, authority=CharterAuthority.EXECUTE
            ),
        }
    )
    verdict = decide_charter(
        "create_issue", narrow, action_ceiling=CharterAuthority.EXECUTE,
        ceiling_source="tech_lead.authority.create_issue",
    )
    advice = TechLeadCharterDecision.from_verdict(
        verdict,
        decision_id=decision_key("run-1", "A9"),
        source=CharterDecisionSource.DECISION,
        run_id="run-1",
        action_id="A9",
        anchor_issue_number=27,
        target_number=27,
        target_is_pr=False,
        decided_at=(NOW - 5 * HOUR).isoformat(),
        tracks_proposal=False,
    )
    authority = InMemoryTechLeadAuthorityStore()
    authority.charter_ledger.record_decisions([advice])
    state = OrchestratorState(
        cached_scope_issues=[_blocked(27, "blocked-failed")],
        session_history=[_ended_blocked(27, NOW - HOUR)],
    )

    custody = _reader(state, authority=authority).read([27]).for_issue(27)

    assert custody.state is CustodyState.UNOWNED
    assert custody.charter is None


# -- the approval backlog and the charter ledger --------------------------------------


def test_an_open_op_is_waiting_on_you_with_its_filing_decision() -> None:
    authority = InMemoryTechLeadAuthorityStore()
    authority.record_op(
        issue_number=700,
        op=StoredTechLeadOp(
            op_type="kill_hung_session",
            target_issue_number=41,
            rationale="hung",
            source_run_id="run-1",
            source_session_name="issue-41",
            source_action_id="A1",
            created_at=(NOW - 2 * HOUR).isoformat(),
            target_session_id="s",
            target_terminal_id="t",
            target_session_type="issue",
        ),
    )
    filed = _decision(
        "kill_hung_session",
        target=41,
        anchor=41,
        ceiling=CharterAuthority.PROPOSE,
        tracks_proposal=True,
        proposal_issue_number=700,
    )
    authority.charter_ledger.record_decisions([filed])
    state = OrchestratorState(cached_scope_issues=[_blocked(41, "blocked-failed")])

    custody = _reader(state, authority=authority).read([41]).for_issue(41)

    assert custody.state is CustodyState.WAITING_ON_YOU
    assert custody.clock is not None and custody.clock.since == NOW - 2 * HOUR
    assert custody.charter is not None and custody.charter.decision_id == filed.decision_id


def test_only_decisions_about_the_item_explain_it() -> None:
    """A run anchored on #50 that acted on #51 says nothing about #50."""
    authority = InMemoryTechLeadAuthorityStore()
    authority.charter_ledger.record_decisions(
        [_decision("recover_validated_work", target=51, anchor=50)]
    )
    state = OrchestratorState(
        cached_scope_issues=[_blocked(50, "blocked-failed"), _blocked(51, "blocked-failed")],
        session_history=[_ended_blocked(50, NOW - 3 * HOUR), _ended_blocked(51, NOW - 3 * HOUR)],
    )

    board = _reader(state, authority=authority).read([50, 51])

    assert board.for_issue(50).state is CustodyState.UNOWNED
    assert board.for_issue(51).state is CustodyState.VERIFY


def test_a_busy_anchored_run_cannot_crowd_out_the_item_s_own_remedy() -> None:
    """The ledger filters to decisions ABOUT the item before its read limit."""
    from issue_orchestrator.control.blocked_item_custody_reader import DECISIONS_PER_ITEM

    authority = InMemoryTechLeadAuthorityStore()
    remedy = _decision("recover_validated_work", target=53, anchor=53, action_id="A0")
    others = [
        replace(
            _decision("kill_hung_session", target=1000 + i, anchor=53, action_id=f"A{i + 1}"),
            decided_at=(NOW - timedelta(minutes=30 - i)).isoformat(),
        )
        for i in range(DECISIONS_PER_ITEM + 5)
    ]
    authority.charter_ledger.record_decisions([remedy, *others])
    state = OrchestratorState(
        cached_scope_issues=[_blocked(53, "blocked-failed")],
        session_history=[_ended_blocked(53, NOW - 3 * HOUR)],
    )

    custody = _reader(state, authority=authority).read([53]).for_issue(53)

    assert custody.state is CustodyState.VERIFY
    assert custody.charter is not None and custody.charter.decision_id == remedy.decision_id


def test_a_follow_up_filed_by_a_run_anchored_on_the_item_is_not_its_remedy() -> None:
    """The ledger returns it as ABOUT the item; the policy knows it is no remedy."""
    authority = InMemoryTechLeadAuthorityStore()
    follow_up = _decision("create_issue", target=None, anchor=52)
    authority.charter_ledger.record_decisions([follow_up])
    state = OrchestratorState(
        cached_scope_issues=[_blocked(52, "blocked-failed")],
        session_history=[_ended_blocked(52, NOW - 3 * HOUR)],
    )

    assert authority.charter_ledger.list_about_issue(52) == (follow_up,)
    assert (
        _reader(state, authority=authority).read([52]).for_issue(52).state
        is CustodyState.UNOWNED
    )


def test_a_live_fix_tracker_is_being_fixed_until_its_reassess_deadline() -> None:
    authority = InMemoryTechLeadAuthorityStore()
    for number, recorded in ((60, NOW - HOUR), (61, NOW - 30 * HOUR)):
        authority.transition_disposition(
            previous=None,
            disposition=TechLeadDisposition(
                issue_number=number,
                tracker_issue_number=800,
                rationale="tracked",
                source_run_id="run-1",
                source_session_name="issue-60",
                source_action_id="A1",
                recorded_at=recorded.isoformat(),
            ),
        )
    state = OrchestratorState(
        cached_scope_issues=[_blocked(60, "blocked-failed"), _blocked(61, "blocked-failed")]
    )

    board = _reader(state, authority=authority).read([60, 61])

    assert board.for_issue(60).state is CustodyState.BEING_FIXED
    assert "#800" in board.for_issue(60).reason
    assert board.for_issue(61).state is CustodyState.UNOWNED  # past its 24h deadline


# -- labels and the needs-human block ---------------------------------------------


def test_needs_human_reads_its_recorded_cause() -> None:
    state = OrchestratorState(
        cached_scope_issues=[_blocked(70, "needs-human"), _blocked(71, "needs-human")]
    )
    causes = {70: frozenset({NeedsHumanCause.AGENT_COMPLETION})}

    causes[71] = frozenset({NeedsHumanCause.SESSION_LIFECYCLE})

    board = _reader(state, causes=causes).read([70, 71])

    assert board.for_issue(70).state is CustodyState.WAITING_ON_YOU
    held = board.for_issue(71)
    assert held.state is CustodyState.HELD
    assert held.clock is not None and held.clock.lower_bound
    assert held.clock.since == datetime(2026, 9, 27, 9, 0, tzinfo=timezone.utc)


def test_the_tech_lead_marker_label_is_its_escalation() -> None:
    state = OrchestratorState(
        cached_scope_issues=[_blocked(72, "needs-human", "tech-lead-needs-human")]
    )

    custody = _reader(state).read([72]).for_issue(72)

    assert custody.state is CustodyState.WAITING_ON_YOU
    assert custody.reason == "The tech lead escalated it to you."


def test_a_blocked_history_entry_without_observed_labels_is_custody_unknown() -> None:
    state = OrchestratorState(
        session_history=[
            SessionHistoryEntry(
                issue_number=80,
                title="t",
                agent_type="agent:web",
                status="failed",
                runtime_minutes=3,
                completed_at=NOW - HOUR,
            )
        ]
    )

    custody = _reader(state).read([80]).for_issue(80)

    assert custody.state is CustodyState.UNOWNED
    assert custody.reason.startswith("custody unknown: this refresh did not observe")


def test_a_provider_outage_waits_on_the_world_until_its_circuit_retries() -> None:
    state = OrchestratorState(cached_scope_issues=[_blocked(90, "blocked:provider-unavailable")])
    circuit = ProviderCircuitStatus(
        provider="codex:metered",
        is_open=True,
        open_until=NOW + HOUR,
        cooldown_remaining_seconds=3600,
        consecutive_outages=2,
        last_error_summary="quota",
        updated_at=NOW - HOUR,
    )

    custody = (
        _reader(
            state,
            lanes=lambda agent: ("codex:metered",) if agent == "agent:web" else (),
            circuits=(circuit,),
        )
        .read([90])
        .for_issue(90)
    )

    assert custody.state is CustodyState.WAITING_ON_WORLD
    assert "codex:metered" in custody.reason and "2026-09-27T13:00" in custody.reason


def test_ci_and_dependency_waits_are_waiting_on_world() -> None:
    state = OrchestratorState(
        cached_scope_issues=[_blocked(91, "blocked-failed"), _blocked(92, "blocked-cross-milestone")],
        awaiting_merge_checks_pending_since={91: (NOW - HOUR).timestamp()},
        dependency_problems={
            92: DependencyProblem(issue_number=92, issue_title="t", blocked_by=[], summary="blocked by #3")
        },
    )

    board = _reader(state).read([91, 92])

    assert board.for_issue(91).state is CustodyState.WAITING_ON_WORLD
    assert board.for_issue(92).reason == "Waiting on a dependency: blocked by #3."


# -- the stuck sweep and the liveness seam ----------------------------------------------


def test_the_stuck_sweep_budget_decides_queued_versus_held() -> None:
    state = OrchestratorState(
        cached_scope_issues=[
            _blocked(100, "blocked-failed"),
            _blocked(101, "blocked-failed", "needs-human"),
            _blocked(102, "blocked-failed"),
        ],
        recovery_attempts={100: 1, 101: 3},
        pending_stuck_sweep_escalations={102},
        last_stuck_sweep_at=(NOW - HOUR).timestamp(),
    )

    board = _reader(state).read([100, 101, 102])

    assert board.for_issue(100).state is CustodyState.QUEUED_FOR_TECH_LEAD
    assert board.for_issue(101).state is CustodyState.HELD  # escalation landed
    assert board.for_issue(102).state is CustodyState.QUEUED_FOR_TECH_LEAD  # still retrying it


def test_unowned_says_when_the_next_sweep_is_due() -> None:
    state = OrchestratorState(
        cached_scope_issues=[_blocked(103, "blocked-failed")],
        last_stuck_sweep_at=(NOW - HOUR).timestamp(),
    )

    custody = _reader(state).read([103]).for_issue(103)

    assert custody.state is CustodyState.UNOWNED
    assert "next stuck sweep is due at 2026-09-27T15:00" in custody.reason  # 240 min cadence


def test_unowned_names_the_sweep_s_own_retry_deadline_after_a_failed_sweep() -> None:
    """One deadline rule: the board says exactly when stuck_sweep_due turns true."""
    from issue_orchestrator.control.stuck_sweep import stuck_sweep_due

    config = _config()
    state = OrchestratorState(
        cached_scope_issues=[_blocked(104, "blocked-failed")],
        last_stuck_sweep_at=(NOW - 4 * HOUR - 5 * timedelta(minutes=1)).timestamp(),
        last_stuck_sweep_failure_at=(NOW - 5 * timedelta(minutes=1)).timestamp(),
    )

    custody = _reader(state, config=config).read([104]).for_issue(104)

    assert "next stuck sweep is due at 2026-09-27T12:10" in custody.reason
    assert not stuck_sweep_due(config, state, NOW.timestamp())
    assert stuck_sweep_due(config, state, (NOW + 10 * timedelta(minutes=1)).timestamp())


def test_an_action_the_liveness_owner_parked_holds_its_item() -> None:
    state = OrchestratorState(cached_scope_issues=[_blocked(110, "blocked-failed")])
    parked = _Parked(
        {110: (ParkedActionFact("remove_label", "needs_human", "pause label forbidden", NOW - HOUR),)}
    )

    custody = _reader(state, parked=parked).read([110]).for_issue(110)

    assert custody.state is CustodyState.HELD
    assert "remove label" in custody.reason


# -- fail visible -----------------------------------------------------------------------


def test_a_source_that_raises_makes_its_item_custody_unknown() -> None:
    state = OrchestratorState(cached_scope_issues=[_blocked(120, "needs-human")])

    custody = _reader(state, broken_causes=True).read([120]).for_issue(120)

    assert custody.state is CustodyState.UNOWNED
    assert custody.reason == "custody unknown: needs-human causes could not be read"


def test_an_unreadable_backlog_makes_every_item_custody_unknown(make_session) -> None:
    class _BrokenLedger(InMemoryTechLeadAuthorityStore):
        def list_ops(self):  # type: ignore[override]
            raise RuntimeError("database is locked")

    session = replace(make_session(issue_number=130), agent_label=TECH_LEAD)
    state = OrchestratorState(
        active_sessions=[session], cached_scope_issues=[_blocked(130, "blocked-failed")]
    )

    custody = _reader(state, authority=_BrokenLedger()).read([130]).for_issue(130)

    assert custody.state is CustodyState.UNOWNED
    assert custody.reason == "custody unknown: approval backlog could not be read"


# -- config -------------------------------------------------------------------------------


def test_stale_thresholds_come_from_config() -> None:
    config = _config()
    config.tech_lead.custody.stale_after_minutes.held = 30
    state = OrchestratorState(cached_scope_issues=[_blocked(140, "needs-human")])

    custody = (
        _reader(state, config=config, causes={140: frozenset({NeedsHumanCause.SESSION_LIFECYCLE})})
        .read([140])
        .for_issue(140)
    )

    assert custody.state is CustodyState.HELD
    assert custody.stale_after == timedelta(minutes=30)
    assert custody.stale  # last activity three hours ago, at least


def test_a_bad_threshold_fails_at_composition_not_on_render() -> None:
    config = _config()
    config.tech_lead.custody.stale_after_minutes.verify = 0

    with pytest.raises(ValueError, match="stale_after_minutes.verify"):
        _reader(OrchestratorState(), config=config)


def test_a_circuit_that_closed_between_reads_holds_nothing() -> None:
    state = OrchestratorState(cached_scope_issues=[_blocked(150, "blocked:provider-unavailable")])
    closed = ProviderCircuitStatus(
        provider="codex",
        is_open=False,
        open_until=None,
        cooldown_remaining_seconds=0,
        consecutive_outages=1,
        last_error_summary=None,
        updated_at=NOW - HOUR,
    )

    custody = (
        _reader(state, lanes=lambda _agent: ("codex",), circuits=(closed,))
        .read([150])
        .for_issue(150)
    )

    assert custody.state is CustodyState.UNOWNED
    assert "nothing has picked it up" in custody.reason  # an orphan, not an unreadable source


def test_a_provider_wait_is_dated_as_a_lower_bound() -> None:
    state = OrchestratorState(cached_scope_issues=[_blocked(151, "blocked:provider-unavailable")])
    open_ = ProviderCircuitStatus(
        provider="codex",
        is_open=True,
        open_until=NOW + HOUR,
        cooldown_remaining_seconds=3600,
        consecutive_outages=3,
        last_error_summary="quota",
        updated_at=NOW - 5 * timedelta(minutes=1),
    )

    custody = (
        _reader(state, lanes=lambda _agent: ("codex",), circuits=(open_,))
        .read([151])
        .for_issue(151)
    )

    assert custody.state is CustodyState.WAITING_ON_WORLD
    assert custody.clock is not None and custody.clock.lower_bound


def _comments(target: int, count: int, *, first: datetime) -> list[TechLeadCharterDecision]:
    return [
        replace(
            _decision("post_comment", target=target, anchor=target, action_id=f"C{i}"),
            decided_at=(first + i * timedelta(seconds=1)).isoformat(),
        )
        for i in range(count)
    ]


def test_an_old_decision_whose_approval_applied_later_still_explains_verify() -> None:
    from issue_orchestrator.domain.tech_lead_charter_decisions import CharterProposalLifecycle

    authority = InMemoryTechLeadAuthorityStore()
    filed = replace(
        _decision(
            "kill_hung_session",
            target=160,
            anchor=160,
            ceiling=CharterAuthority.PROPOSE,
            tracks_proposal=True,
            action_id="A1",
        ),
        decided_at=(NOW - 4 * HOUR).isoformat(),
    ).with_lifecycle(
        CharterProposalLifecycle.APPROVED_APPLIED, at=(NOW - HOUR).isoformat(), proposal_issue_number=700
    )
    authority.charter_ledger.record_decisions([filed, *_comments(160, 20, first=NOW - 3 * HOUR)])
    state = OrchestratorState(
        cached_scope_issues=[_blocked(160, "blocked-failed")],
        session_history=[_ended_blocked(160, NOW - 5 * HOUR)],
    )

    custody = _reader(state, authority=authority).read([160]).for_issue(160)

    assert custody.state is CustodyState.VERIFY
    assert custody.charter is not None and custody.charter.decision_id == filed.decision_id
    assert custody.clock is not None and custody.clock.since == NOW - HOUR


def test_an_open_proposal_keeps_its_filing_decision_however_much_came_after() -> None:
    from issue_orchestrator.control.blocked_item_custody_reader import DECISIONS_PER_ITEM

    authority = InMemoryTechLeadAuthorityStore()
    authority.record_op(
        issue_number=701,
        op=StoredTechLeadOp(
            op_type="kill_hung_session",
            target_issue_number=161,
            rationale="hung",
            source_run_id="run-1",
            source_session_name="issue-161",
            source_action_id="A1",
            created_at=(NOW - 4 * HOUR).isoformat(),
            target_session_id="s",
            target_terminal_id="t",
            target_session_type="issue",
        ),
    )
    filed = replace(
        _decision(
            "kill_hung_session",
            target=161,
            anchor=161,
            ceiling=CharterAuthority.PROPOSE,
            tracks_proposal=True,
            proposal_issue_number=701,
        ),
        decided_at=(NOW - 4 * HOUR).isoformat(),
    )
    newer = _comments(161, DECISIONS_PER_ITEM + 3, first=NOW - 3 * HOUR)
    authority.charter_ledger.record_decisions([filed, *newer])
    state = OrchestratorState(cached_scope_issues=[_blocked(161, "blocked-failed")])

    custody = _reader(state, authority=authority).read([161]).for_issue(161)

    assert custody.state is CustodyState.WAITING_ON_YOU
    assert custody.charter is not None and custody.charter.decision_id == filed.decision_id


def test_a_sweep_releasing_a_published_pr_s_review_is_being_fixed() -> None:
    state = OrchestratorState(
        cached_scope_issues=[_blocked(170, "blocked-failed")],
        recovery_attempts={170: 0},
        review_release_budgets={170},
        stuck_sweep_review_releases=[170],
    )

    custody = _reader(state).read([170]).for_issue(170)

    assert custody.state is CustodyState.BEING_FIXED
    assert "releasing that PR's review" in custody.reason


def test_an_applied_remedy_outlasts_any_amount_of_later_history() -> None:
    from issue_orchestrator.control.blocked_item_custody_reader import DECISIONS_PER_ITEM

    authority = InMemoryTechLeadAuthorityStore()
    remedy = replace(
        _decision("recover_validated_work", target=171, anchor=171, action_id="A0"),
        decided_at=(NOW - 4 * HOUR).isoformat(),
    )
    later = _comments(171, DECISIONS_PER_ITEM + 10, first=NOW - 3 * HOUR)
    authority.charter_ledger.record_decisions([remedy, *later])
    state = OrchestratorState(
        cached_scope_issues=[_blocked(171, "blocked-failed")],
        session_history=[_ended_blocked(171, NOW - 5 * HOUR)],
    )

    custody = _reader(state, authority=authority).read([171]).for_issue(171)

    assert custody.state is CustodyState.VERIFY
    assert custody.charter is not None and custody.charter.decision_id == remedy.decision_id


def test_an_open_proposal_s_filing_survives_history_about_the_proposal_itself() -> None:
    authority = InMemoryTechLeadAuthorityStore()
    authority.record_op(
        issue_number=702,
        op=StoredTechLeadOp(
            op_type="kill_hung_session",
            target_issue_number=172,
            rationale="hung",
            source_run_id="run-1",
            source_session_name="issue-172",
            source_action_id="A1",
            created_at=(NOW - 4 * HOUR).isoformat(),
            target_session_id="s",
            target_terminal_id="t",
            target_session_type="issue",
        ),
    )
    filed = replace(
        _decision(
            "kill_hung_session",
            target=172,
            anchor=172,
            ceiling=CharterAuthority.PROPOSE,
            tracks_proposal=True,
            proposal_issue_number=702,
        ),
        decided_at=(NOW - 4 * HOUR).isoformat(),
    )
    about_proposal = _comments(702, 15, first=NOW - 3 * HOUR)
    authority.charter_ledger.record_decisions([filed, *about_proposal])
    state = OrchestratorState(cached_scope_issues=[_blocked(172, "blocked-failed")])

    custody = _reader(state, authority=authority).read([172]).for_issue(172)

    assert custody.state is CustodyState.WAITING_ON_YOU
    assert custody.charter is not None and custody.charter.decision_id == filed.decision_id
    assert custody.charter.action_ceiling == "propose"
