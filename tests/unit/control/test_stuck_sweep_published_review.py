"""The stuck sweep leaves a PR of published validated work to its review (#7293).

porchpin 2026-09-23: eight halted issues whose validated work recovery had
already published as CI-green PRs were re-injected as ``timed_out`` failures,
investigated, and escalated to needs-human after three cycles. These drive the
real sweep through ``FactGatherer`` with the real ownership owner over fake
store/PR ports, and keep a control case beside each so none can pass vacuously.
"""

from __future__ import annotations

from issue_orchestrator.control.fact_gatherer import FactGatherer
from issue_orchestrator.domain.models import OrchestratorState
from issue_orchestrator.domain.validated_work import ValidatedWorkState
from issue_orchestrator.ports.event_sink import InMemoryEventSink

from tests.unit.control.published_review_support import (
    DispositionStore,
    PullRequests,
    custody,
    disposition,
    pr,
)
from tests.unit.control.test_stuck_sweep import _config, _issue, _RecordingHost

HELD = 382
PLAIN = 390


def _gatherer(issues, *, records, prs, events=None, max_recovery_attempts=3):
    store = DispositionStore(records)
    pulls = PullRequests(prs)
    gatherer = FactGatherer(
        config=_config(max_recovery_attempts=max_recovery_attempts),
        repository_host=_RecordingHost(issues),
        events=events,
        published_review=custody(store, pulls),
    )
    return gatherer, pulls


def _blocked(number: int):
    return _issue(number, labels=["agent:web", "blocked-failed", "pr-pending"])


def _published(number: int, *, pr_state: str = "open"):
    return (
        {number: (disposition(number, ValidatedWorkState.RECOVERED, pr_number=500),)},
        {number: [pr(number, 500, state=pr_state)]},
    )


def test_a_published_pr_owns_its_blocked_issue():
    records, prs = _published(HELD)
    events = InMemoryEventSink()
    gatherer, _ = _gatherer(
        [_blocked(HELD), _blocked(PLAIN)], records=records, prs=prs, events=events
    )
    state = OrchestratorState()

    snapshot = gatherer.create_snapshot(state, issues=[])

    injected = {failure.issue_number for failure in snapshot.discovered_failures}
    assert HELD not in injected
    assert HELD not in state.recovery_attempts, "no recovery cycle was opened"
    # Control: the same labels without published work are still recovered.
    assert PLAIN in injected
    (event,) = [
        e for e in events.events if e.name == "tech_lead.stuck_sweep"
    ]
    assert event.data["held_for_review"] == [HELD]


def test_a_published_pr_is_never_escalated_even_on_an_exhausted_budget():
    """The porchpin outcome: three cycles, then needs-human on reviewable work."""
    records, prs = _published(HELD)
    gatherer, _ = _gatherer([_blocked(HELD)], records=records, prs=prs)
    state = OrchestratorState()
    state.recovery_attempts = {HELD: 2}  # one failed cycle short of the ceiling

    snapshot = gatherer.create_snapshot(state, issues=[])

    assert HELD not in snapshot.stuck_sweep_escalations
    assert HELD not in state.pending_stuck_sweep_escalations
    assert state.recovery_attempts[HELD] == 2, "an owned issue spends no budget"


def test_an_unlanded_escalation_is_withdrawn_once_a_published_pr_holds_it():
    records, prs = _published(HELD)
    gatherer, _ = _gatherer([_blocked(HELD)], records=records, prs=prs)
    state = OrchestratorState()
    state.recovery_attempts = {HELD: 3}
    state.pending_stuck_sweep_escalations = {HELD}

    snapshot = gatherer.create_snapshot(state, issues=[])

    assert HELD not in snapshot.stuck_sweep_escalations
    assert state.pending_stuck_sweep_escalations == set()


def test_closing_the_pr_returns_the_issue_to_the_sweep():
    """Closing the PR is the operator's abandonment; the issue is stuck again."""
    records, prs = _published(HELD, pr_state="closed")
    gatherer, _ = _gatherer([_blocked(HELD)], records=records, prs=prs)
    state = OrchestratorState()

    snapshot = gatherer.create_snapshot(state, issues=[])

    assert HELD in {failure.issue_number for failure in snapshot.discovered_failures}


def test_unresolved_work_is_not_review_ownership_and_costs_no_pr_read():
    records = {HELD: (disposition(HELD, ValidatedWorkState.FAILED),)}
    gatherer, pulls = _gatherer(
        [_blocked(HELD)], records=records, prs={HELD: [pr(HELD, 500)]}
    )
    state = OrchestratorState()

    snapshot = gatherer.create_snapshot(state, issues=[])

    assert HELD in {failure.issue_number for failure in snapshot.discovered_failures}
    assert pulls.reads == []
