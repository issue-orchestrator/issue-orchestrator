"""The rework twin of #7348: ending a rework must retire its durable claim (#7380).

A launched rework HOLDS a pending-work claim; a rework that was ever deferred
also lives as a DEFERRED row. The per-tick recovery sweep re-admits every such
row no live run holds. So every way a rework ENDS -- the issue-runtime boundary
stopping a live ``rework-N`` terminal (reset, stop, issue closed, kill), or an
operator clearing the queued item (cancel, scratch reset) -- must settle or
retire the claim, or the next tick puts the rework straight back.

Each test uses the REAL launcher, SQLite claim store and sweep.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.in_flight_work import InFlightWorkLedger
from issue_orchestrator.domain.validated_work_commands import (
    ValidatedWorkDispositionBatch,
)
from tests.unit.test_provider_readiness_boundary import (
    _pending_state,
    _quarantine,
    _ready_harness,
    _recover_with_no_terminals,
    _route,
)

SUBJECT = 7  # the rework ``_pending_state("rework")`` queues


@pytest.mark.parametrize("observed", [None, "owned", "peer", "unavailable"])
def test_ended_run_retires_its_verified_cas_lease_before_dropping_record(tmp_path, observed):
    from dataclasses import replace
    from types import SimpleNamespace
    harness, state, session = _live_rework(tmp_path)
    session.lease_id = "owned"
    core, _ = _core(state, harness)
    claims = MagicMock()
    if observed == "unavailable":
        claims.get_current_claim.side_effect = RuntimeError("claim read unavailable")
    else:
        claims.get_current_claim.return_value = None if observed is None else SimpleNamespace(lease_id=observed)
    core = replace(core, claim_manager=claims)
    if observed in ("owned", "unavailable"):
        with pytest.raises(RuntimeError):
            core.release_preserved(SUBJECT, "operator-terminated", ValidatedWorkDispositionBatch.no_work(SUBJECT, "t"))
        assert state.active_sessions == [session]
    else:
        core.release_preserved(SUBJECT, "operator-terminated", ValidatedWorkDispositionBatch.no_work(SUBJECT, "t"))
        assert state.active_sessions == []
    claims.release_claim.assert_called_once_with(SUBJECT, "owned")


def _next_tick_sweep(state, harness) -> int:
    return InFlightWorkLedger(state, harness.claims).recover_unresolved(
        _quarantine(harness)
    )


def _live_rework(tmp_path: Path):
    harness = _ready_harness(tmp_path)
    state = _pending_state("rework")
    session = _route("rework", state, harness)
    assert session is not None and session.terminal_id == f"rework-{SUBJECT}"
    assert [u.deferred for u in harness.claims.list_unresolved_claims()] == [False]
    return harness, state, session


def _core(state, harness, *, running: bool = True):
    from issue_orchestrator.control.review_exchange_lifecycle import (
        CoreIssueRuntimeOwners,
    )

    sessions = MagicMock()
    sessions.exists.return_value = running
    retry = MagicMock()
    retry.has_active_retry.return_value = False
    return CoreIssueRuntimeOwners(
        sessions,
        state.active_sessions,
        None,
        None,
        retry,
        InFlightWorkLedger(state, harness.claims),
        MagicMock(),
    ), sessions


def test_the_issue_runtime_boundary_settles_a_stopped_rework_claim(tmp_path):
    """Reset, stop-session, issue-completed and kill all end a live rework at
    ``release_preserved``, which dropped the record and left the claim HELD."""
    harness, state, _session = _live_rework(tmp_path)
    core, sessions = _core(state, harness)

    termination = core.release_preserved(
        SUBJECT, "reset-retry", ValidatedWorkDispositionBatch.no_work(SUBJECT, "t")
    )

    assert f"rework-{SUBJECT}" in termination.stopped_session_ids
    assert state.active_sessions == []
    assert harness.claims.list_unresolved_claims() == ()
    assert _next_tick_sweep(state, harness) == 0
    assert state.pending_reworks == []


def test_a_stop_that_commits_then_raises_still_settles_the_claim(tmp_path):
    """codex #7380 r2: the terminal was killed, then ``stop`` raised (e.g.
    publishing its event). The failure propagates, but the ended rework's
    claim must not be left HELD beside no live run."""
    harness, state, _session = _live_rework(tmp_path)
    core, sessions = _core(state, harness)
    stopped: list[str] = []
    sessions.exists.side_effect = lambda ref: (
        ref.name == f"rework-{SUBJECT}" and ref.name not in stopped
    )

    def stop(ref):
        stopped.append(ref.name)
        raise RuntimeError("event publish failed after the kill")

    sessions.stop.side_effect = stop

    with pytest.raises(RuntimeError):
        core.release_preserved(
            SUBJECT, "reset-retry", ValidatedWorkDispositionBatch.no_work(SUBJECT, "t")
        )

    assert state.active_sessions == []
    assert harness.claims.list_unresolved_claims() == ()
    assert _next_tick_sweep(state, harness) == 0
    assert state.pending_reworks == []


def test_an_already_dead_rework_terminal_is_settled_too(tmp_path):
    """A stale record (terminal gone) is cleared by the same boundary."""
    harness, state, _session = _live_rework(tmp_path)
    core, _sessions = _core(state, harness, running=False)

    core.release_preserved(
        SUBJECT, "issue-completed", ValidatedWorkDispositionBatch.no_work(SUBJECT, "t")
    )

    assert state.active_sessions == []
    assert _next_tick_sweep(state, harness) == 0
    assert state.pending_reworks == []


def test_a_failed_settlement_keeps_the_rework_record(tmp_path):
    """The record is what tells the sweep the run is live: it goes only after
    its claim settled, so a raising claim store cannot strand a HELD claim."""
    harness, state, session = _live_rework(tmp_path)
    failing = MagicMock(wraps=harness.claims)
    failing.consume_pending_work_claim.side_effect = OSError("disk full")
    core, _ = _core(state, harness)
    object.__setattr__(core, "work", InFlightWorkLedger(state, failing))

    try:
        core.release_preserved(
            SUBJECT, "reset-retry", ValidatedWorkDispositionBatch.no_work(SUBJECT, "t")
        )
    except OSError:
        pass

    assert [s.terminal_id for s in state.active_sessions] == [session.terminal_id]
    assert _next_tick_sweep(state, harness) == 0
    assert state.pending_reworks == []


def _terminate_generation(tmp_path, *, stop_raises_after_commit: bool):
    """A tech lead's kill_hung_session: stop the EXACT rework generation."""
    from issue_orchestrator.domain.tech_lead_session import TechLeadSessionGeneration
    from tests.runtime_lifecycle_helpers import runtime_owners

    harness, state, session = _live_rework(tmp_path)
    retry = MagicMock()
    retry.has_active_retry.return_value = False
    lifecycle = runtime_owners(
        active_sessions=state.active_sessions, pair_registry=None,
        job_supervisor=None, publish_recovery=retry,
    )
    object.__setattr__(lifecycle.core, "work", InFlightWorkLedger(state, harness.claims))

    killed: list[str] = []

    def kill(name: str) -> None:
        killed.append(name)  # the stop COMMITTED...
        if stop_raises_after_commit:
            raise RuntimeError("tmux answered late")  # ...but reported failure

    target = TechLeadSessionGeneration(
        SUBJECT, session.key.kind, session.terminal_id, session.run_assets.run_id
    )
    try:
        lifecycle.terminate_generation(
            target, "tech-lead kill", session_exists=lambda name: name not in killed,
            kill_session=kill,
        )
    except Exception:
        assert stop_raises_after_commit
    return harness, state


@pytest.mark.parametrize("stop_raises_after_commit", [False, True])
def test_a_killed_rework_generation_settles_its_claim(tmp_path, stop_raises_after_commit):
    """codex #7380 r1: the generation-bound kill dropped the exact rework's
    record without settling its HELD claim, so the sweep relaunched it."""
    harness, state = _terminate_generation(
        tmp_path, stop_raises_after_commit=stop_raises_after_commit
    )

    assert state.active_sessions == []
    assert harness.claims.list_unresolved_claims() == ()
    assert _next_tick_sweep(state, harness) == 0
    assert state.pending_reworks == []


@pytest.mark.parametrize(
    ("queue", "attribute"),
    [
        ("rework", "pending_reworks"),
        ("review", "pending_reviews"),
        ("retrospective_review", "pending_retrospective_reviews"),
        ("tech_lead", "pending_tech_lead_reviews"),
    ],
)
def test_abandoned_queued_work_of_every_kind_is_not_readmitted(tmp_path, queue, attribute):
    """Operator cancel / kill / scratch reset clear the QUEUED item, which
    after a restart is backed by a deferred row -- for every dequeued kind."""
    from issue_orchestrator.control.abandoned_queued_work import (
        retire_abandoned_queued_work,
    )

    harness = _ready_harness(tmp_path)
    state = _pending_state(queue)
    assert _route(queue, state, harness) is not None
    restarted = _recover_with_no_terminals(state, harness)
    assert len(getattr(restarted, attribute)) == 1

    retire_abandoned_queued_work(
        state=restarted, claims=harness.claims, tech_lead_authority=MagicMock(),
        issue_number=SUBJECT, ended_sessions=(),
    )

    assert getattr(restarted, attribute) == []
    assert harness.claims.list_unresolved_claims() == ()
    assert _next_tick_sweep(restarted, harness) == 0
    assert getattr(restarted, attribute) == []


def test_a_scratch_reset_retires_the_review_of_a_superseded_pr(tmp_path):
    """A scratch reset supersedes the issue's PRs; a queued review of one is
    ended with them, durable claim included."""
    from issue_orchestrator.control.queued_work_retirement import QueuedWorkRetirement

    harness = _ready_harness(tmp_path)
    state = _pending_state("review")
    review = state.pending_reviews[0]
    assert _route("review", state, harness) is not None
    restarted = _recover_with_no_terminals(state, harness)

    QueuedWorkRetirement(restarted, harness.claims).retire_issue(
        999, superseded_prs=(review.pr_number,)
    )

    assert restarted.pending_reviews == []
    assert _next_tick_sweep(restarted, harness) == 0


def test_the_sweep_still_readmits_a_rework_nobody_ended(tmp_path):
    """Control: without a terminal decision the row IS the work."""
    harness = _ready_harness(tmp_path)
    state = _pending_state("rework")
    assert _route("rework", state, harness) is not None
    restarted = _recover_with_no_terminals(state, harness)
    restarted.pending_reworks.clear()

    assert _next_tick_sweep(restarted, harness) == 1
    assert len(restarted.pending_reworks) == 1
