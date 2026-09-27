"""Every way a queued tech-lead run ENDS retires its durable claim (#7348).

A queued run that was ever deferred (a provider stop, an unspawned launch, or
startup recovery) also lives as a DEFERRED row in the pending-work ledger, and
the per-tick recovery sweep re-admits every deferred row whose work is not
live. Porchpin #200 was withdrawn as ``issue_closed`` and re-admitted by the
sweep over a thousand times because the withdrawal cleared only the queue.

Each test below builds that exact situation with the REAL launcher, the REAL
SQLite claim store and the REAL sweep, ends the run through one production
terminal path, runs the sweep again (the next tick), and requires that the run
stays gone -- queue AND ledger.
"""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.actions import (
    CreateTechLeadIssueAction,
    DropTechLeadAction,
)
from issue_orchestrator.control.in_flight_work import InFlightWorkLedger
from issue_orchestrator.control.tech_lead_run_ownership import TechLeadRunOwnership
from issue_orchestrator.domain.models import DiscoveredFailure
from issue_orchestrator.ports.run_ledger_store import SingleInstanceRunLedgerStore
from issue_orchestrator.domain.tech_lead_run import (
    REASON_ISSUE_CLOSED,
    IssueInvestigationScope,
)
from issue_orchestrator.domain.tech_lead_session import (
    TechLeadCreationOrigin,
    TechLeadSessionFlavor,
)
from issue_orchestrator.events import EventName
from tests.unit.control.test_tech_lead_launch_authority import (
    FakeIssue,
    FakeRepositoryHost,
    RecordingEvents,
    _config,
)
from tests.unit.test_provider_readiness_boundary import (
    _pending_state,
    _quarantine,
    _ready_harness,
    _recover_with_no_terminals,
    _route,
)

SUBJECT = 7  # the failure investigation ``_pending_state("tech_lead")`` queues


def _queued_with_deferred_claim(tmp_path: Path):
    """A queued investigation backed by a deferred ledger row, as after a restart."""
    harness = _ready_harness(tmp_path)
    state = _pending_state("tech_lead")
    assert _route("tech_lead", state, harness) is not None
    restarted = _recover_with_no_terminals(state, harness)
    assert [i.issue_number for i in restarted.pending_tech_lead_reviews] == [SUBJECT]
    assert [(u.claim.work_key(), u.deferred) for u in harness.claims.list_unresolved_claims()] == [
        (f"tech_lead:{SUBJECT}", True)
    ]
    return harness, restarted


def _next_tick_sweep(state, harness) -> int:
    return InFlightWorkLedger(state, harness.claims).recover_unresolved(
        _quarantine(harness)
    )


def _ownership() -> TechLeadRunOwnership:
    return TechLeadRunOwnership(
        SingleInstanceRunLedgerStore(lease_seconds=900),
        lease_seconds=900,
        renew_before_expiry_seconds=300,
    )


def _plan_time_withdrawal(state, claims) -> None:
    from issue_orchestrator.control.tech_lead_run_wiring import (
        withdraw_revalidated_tech_lead_run,
    )

    tick = SimpleNamespace(
        state=state, events=RecordingEvents(), run_ownership=_ownership(),
        pending_work_claims=claims,
    )
    withdraw_revalidated_tech_lead_run(
        DropTechLeadAction(issue_number=SUBJECT, reason=REASON_ISSUE_CLOSED, detail="closed"),
        tick,  # type: ignore[arg-type]
    )


def _launch_time_withdrawal(state, claims) -> None:
    from issue_orchestrator.control.tech_lead_launch_authority import (
        TechLeadLaunchAuthority,
    )
    from issue_orchestrator.control.tech_lead_run_activity import in_memory_run_activity

    events = RecordingEvents()
    authority = TechLeadLaunchAuthority(
        state=state,
        config=_config(),
        ownership=_ownership(),
        repository_host=FakeRepositoryHost({SUBJECT: FakeIssue(SUBJECT, state="closed")}),  # type: ignore[arg-type]
        is_blocking_any=lambda labels: False,
        events=events,  # type: ignore[arg-type]
        launch=lambda _item: pytest.fail("a closed subject must not launch"),
        activity=in_memory_run_activity(),
        claims=claims,
    )
    assert authority.launch(state.pending_tech_lead_reviews[0]) is None
    assert [w["reason"] for w in events.payloads(EventName.TECH_LEAD_RUN_WITHDRAWN)] == [
        REASON_ISSUE_CLOSED
    ]


def _lost_to_a_peer_engine(state, claims) -> None:
    from issue_orchestrator.control.tech_lead_run_wiring import _withdraw_lost_queued_runs

    host = SimpleNamespace(state=state, deps=SimpleNamespace(pending_work_claims=claims))
    _withdraw_lost_queued_runs(host, {IssueInvestigationScope(SUBJECT).run_key})  # type: ignore[arg-type]


def _folded_into_a_storm_review(state, claims) -> None:
    from issue_orchestrator.control.health_review_trigger import (
        HEALTH_REVIEW_MARKER_LABEL,
        intake_created_tech_lead_anchor,
    )

    anchor = CreateTechLeadIssueAction(
        title="Health review: problem storm",
        labels=(HEALTH_REVIEW_MARKER_LABEL,),
        storm_problems=(DiscoveredFailure(SUBJECT, "Test Issue", "failed"),),
        flavor=TechLeadSessionFlavor.HEALTH_REVIEW,
        origin=TechLeadCreationOrigin.authors_anchor(),
    )
    intake_created_tech_lead_anchor(anchor, 900, state, None, MagicMock(), claims=claims)
    assert [i.issue_number for i in state.pending_tech_lead_reviews] == [900]


def _abandoned_by_an_operator(state, claims) -> None:
    from issue_orchestrator.control.abandoned_queued_work import (
        retire_abandoned_queued_work,
    )

    retire_abandoned_queued_work(
        state=state, claims=claims, tech_lead_authority=MagicMock(),
        issue_number=SUBJECT, ended_sessions=(),
    )


@pytest.mark.parametrize(
    "end_the_run",
    [
        _plan_time_withdrawal,
        _launch_time_withdrawal,
        _lost_to_a_peer_engine,
        _folded_into_a_storm_review,
        _abandoned_by_an_operator,
    ],
)
def test_an_ended_queued_run_is_not_readmitted_on_the_next_tick(tmp_path, end_the_run):
    harness, state = _queued_with_deferred_claim(tmp_path)

    end_the_run(state, harness.claims)

    assert SUBJECT not in [i.issue_number for i in state.pending_tech_lead_reviews]
    assert harness.claims.list_unresolved_claims() == ()
    assert _next_tick_sweep(state, harness) == 0
    assert SUBJECT not in [i.issue_number for i in state.pending_tech_lead_reviews]


def test_a_live_run_the_operator_stopped_is_not_readmitted_on_the_next_tick(tmp_path):
    """The HELD half: termination drops the session record without settling
    its claim, and the sweep re-admits a held row no live run holds."""
    from issue_orchestrator.control.abandoned_queued_work import (
        retire_abandoned_queued_work,
    )

    harness = _ready_harness(tmp_path)
    state = _pending_state("tech_lead")
    session = _route("tech_lead", state, harness)
    assert session is not None
    assert [u.deferred for u in harness.claims.list_unresolved_claims()] == [False]

    retire_abandoned_queued_work(
        state=state, claims=harness.claims, tech_lead_authority=MagicMock(),
        issue_number=SUBJECT, ended_sessions=(session,),
    )
    state.release_issue(SUBJECT)

    assert harness.claims.list_unresolved_claims() == ()
    assert _next_tick_sweep(state, harness) == 0
    assert state.pending_tech_lead_reviews == []


def _terminate_live_run(tmp_path, *, kill_fails: bool = False, settle_fails: bool = False):
    """Terminate a LIVE investigation through ``terminate_tech_lead_session`` --
    the owner both an ownership LOSS and an on-demand timeout reach."""
    from issue_orchestrator.control.tech_lead_termination import (
        terminate_tech_lead_session,
    )
    from issue_orchestrator.domain.validated_work_commands import (
        ValidatedWorkDispositionBatch,
    )

    harness = _ready_harness(tmp_path)
    state = _pending_state("tech_lead")
    session = _route("tech_lead", state, harness)
    assert session is not None

    def kill(_name: str) -> None:
        if kill_fails:
            raise RuntimeError("tmux is gone")

    claims = harness.claims
    if settle_fails:
        claims = MagicMock(wraps=harness.claims)
        claims.consume_pending_work_claim.side_effect = OSError("disk full")
    host = SimpleNamespace(
        state=state,
        deps=SimpleNamespace(pending_work_claims=claims, run_ownership=_ownership()),
        kill_session=kill,
        preserve_issue_work=lambda number, reason: ValidatedWorkDispositionBatch.no_work(number, reason),
    )
    outcome = terminate_tech_lead_session(host, session)  # type: ignore[arg-type]
    return harness, state, outcome


def test_a_live_run_terminated_by_its_owner_is_not_readmitted_on_the_next_tick(tmp_path):
    """Ownership LOSS and an on-demand timeout both end a live run through
    ``terminate_tech_lead_session``, which dropped the session record and left
    the claim HELD with no live holder -- so the sweep relaunched it."""
    harness, state, outcome = _terminate_live_run(tmp_path, kill_fails=False)

    assert outcome.work_settled is True
    assert harness.claims.list_unresolved_claims() == ()
    assert _next_tick_sweep(state, harness) == 0
    assert state.pending_tech_lead_reviews == []


@pytest.mark.parametrize("failure", ["kill_fails", "settle_fails"])
def test_a_run_that_did_not_end_cleanly_stays_tracked_and_is_not_duplicated(tmp_path, failure):
    """A terminal that would not stop may still be running, and a claim that
    could not be settled is still HELD. Either way the session record must stay:
    it is what tells the sweep the run is live, so dropping it let the next
    tick re-admit the run and launch a second one beside it."""
    harness, state, outcome = _terminate_live_run(tmp_path, **{failure: True})

    assert outcome.clean is False
    assert outcome.work_settled is False
    assert [s.issue.number for s in state.active_sessions] == [SUBJECT]
    assert [u.deferred for u in harness.claims.list_unresolved_claims()] == [False]
    assert _next_tick_sweep(state, harness) == 0
    assert state.pending_tech_lead_reviews == []


def test_a_live_run_whose_issue_claim_was_lost_is_not_readmitted_on_the_next_tick(tmp_path):
    """Another orchestrator took the issue: the session is stopped and flagged
    ``blocked:claim-lost``. Its HELD claim used to survive the dropped record,
    so the sweep relaunched work this engine no longer owns."""
    from issue_orchestrator.control.claim_loss_termination import (
        stop_claim_lost_session,
    )

    harness = _ready_harness(tmp_path)
    state = _pending_state("tech_lead")
    session = _route("tech_lead", state, harness)
    assert session is not None

    stop_claim_lost_session(
        session, state=state, claims=harness.claims, kill_session=lambda _n: None,
        state_machine_manager=MagicMock(),
    )

    assert state.active_sessions == []
    assert harness.claims.list_unresolved_claims() == ()
    assert _next_tick_sweep(state, harness) == 0
    assert state.pending_tech_lead_reviews == []


def test_a_completion_whose_settlement_fails_keeps_the_run_tracked(tmp_path):
    """#7348 review r4: completion dropped the session record BEFORE settling
    its claim. When the claim store then raised, the claim stayed HELD with no
    live holder and the next tick's sweep re-admitted the finished run."""
    from tests.unit.test_provider_readiness_boundary import _complete_session

    harness = _ready_harness(tmp_path)
    state = _pending_state("tech_lead")
    session = _route("tech_lead", state, harness)
    assert session is not None
    failing = MagicMock(wraps=harness.claims)
    failing.consume_pending_work_claim.side_effect = OSError("disk full")

    with pytest.raises(OSError):
        _complete_session(session, state, failing, provider_error_type=None)

    assert [s.terminal_id for s in state.active_sessions] == [session.terminal_id]
    assert _next_tick_sweep(state, harness) == 0
    assert state.pending_tech_lead_reviews == []


def test_the_sweep_still_readmits_a_run_nobody_ended(tmp_path):
    """The control: without a terminal decision the deferred row IS the work,
    and the sweep must keep bringing it back after the queue is lost."""
    harness, state = _queued_with_deferred_claim(tmp_path)
    state.pending_tech_lead_reviews.clear()  # the in-memory queue did not survive

    assert _next_tick_sweep(state, harness) == 1
    assert [i.issue_number for i in state.pending_tech_lead_reviews] == [SUBJECT]


def test_only_launch_routing_dequeues_a_tech_lead_run_without_retiring_its_claim():
    """``PendingSessionQueues.remove_tech_lead`` drops the queue entry ONLY.

    That is correct for launch routing, where the launch transaction takes the
    claim over. Any other caller ends a run and strands its deferred row -- the
    #7348 bug -- so it must go through ``TechLeadRunRetirement`` instead.
    """
    src = Path(__file__).resolve().parents[3] / "src" / "issue_orchestrator"
    callers = sorted(
        str(path.relative_to(src))
        for path in src.rglob("*.py")
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.Attribute) and node.attr == "remove_tech_lead"
        and path.name != "pending_session_queues.py"
    )
    assert callers == ["control/session_routing.py"]
