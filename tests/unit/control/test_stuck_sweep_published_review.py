"""The stuck sweep hands a PR of published validated work to its review (#7293).

porchpin 2026-09-23: eight halted issues whose validated work the recovery
pipeline had already published as CI-green PRs were re-injected as
``timed_out`` failures, investigated, and escalated to needs-human after three
cycles. Nothing reviewed them either: review discovery drops a PR whose issue is
blocked. These drive the real sweep through ``FactGatherer`` and the real
``Planner`` with the real ownership owner over fake store/PR ports, with a
control case beside each so none can pass vacuously.
"""

from __future__ import annotations

from issue_orchestrator.control.actions import ReleasePublishedReviewAction
from issue_orchestrator.control.fact_gatherer import FactGatherer
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.planner import Planner
from issue_orchestrator.control.review_validity import evaluate_review_validity
from issue_orchestrator.control.scheduler import Scheduler
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
    gatherer = FactGatherer(
        config=_config(max_recovery_attempts=max_recovery_attempts),
        repository_host=_RecordingHost(issues),
        events=events,
        published_review=custody(DispositionStore(records), PullRequests(prs)),
    )
    return gatherer


def _failed(number: int, *extra: str):
    return _issue(number, labels=["agent:web", "blocked-failed", *extra])


def _published(number: int, *, pr_state: str = "open"):
    return (
        {number: (disposition(number, ValidatedWorkState.RECOVERED, pr_number=500),)},
        {number: [pr(number, 500, state=pr_state)]},
    )


def _release_actions(snapshot, number):
    planner = Planner(config=_config(), scheduler=Scheduler(_config()))
    return [
        action
        for action in planner.plan(snapshot).actions
        if isinstance(action, ReleasePublishedReviewAction) and action.issue_number == number
    ]


def test_a_failure_block_on_published_work_releases_its_review():
    records, prs = _published(HELD)
    events = InMemoryEventSink()
    gatherer = _gatherer(
        [_failed(HELD), _failed(PLAIN)], records=records, prs=prs, events=events
    )
    state = OrchestratorState()

    snapshot = gatherer.create_snapshot(state, issues=[])

    injected = {failure.issue_number for failure in snapshot.discovered_failures}
    assert HELD not in injected, "published work is not a failure to investigate"
    assert snapshot.stuck_sweep_review_releases == (HELD,)
    assert state.recovery_attempts[HELD] == 0, "the release is a budgeted cycle"
    # Control: the same label without published work is still investigated.
    assert PLAIN in injected
    (event,) = [e for e in events.events if e.name == "tech_lead.stuck_sweep"]
    assert event.data["released_for_review"] == [HELD]

    (release,) = _release_actions(snapshot, HELD)
    assert release == ReleasePublishedReviewAction(
        issue_number=HELD, code_review_label=_config().code_review_label or "")


def test_the_released_issue_passes_review_validity():
    """The release is what lets review discovery stop dropping the PR."""
    lm = LabelManager(_config())
    before = _failed(HELD, lm.pr_pending)
    after = _issue(HELD, labels=["agent:web", lm.pr_pending])
    review_pr = pr(HELD, 500)

    config = _config()
    blocked = evaluate_review_validity(config=config, label_manager=lm, issue=before,
                                       pr=review_pr, review_label_confirmed=True)
    released = evaluate_review_validity(config=config, label_manager=lm, issue=after,
                                        pr=review_pr, review_label_confirmed=True)

    assert blocked.reason == "issue_blocked"
    assert released.valid, released.reason


def test_a_release_that_does_not_stick_exhausts_like_any_remedy():
    """Re-blocked after release: a failed cycle, then needs-human as before."""
    records, prs = _published(HELD)
    gatherer = _gatherer([_failed(HELD)], records=records, prs=prs)
    state = OrchestratorState()
    state.recovery_attempts = {HELD: 2}  # two releases already failed to stick
    state.review_release_budgets = {HELD}

    snapshot = gatherer.create_snapshot(state, issues=[])

    assert snapshot.stuck_sweep_escalations == (HELD,)
    assert snapshot.stuck_sweep_review_releases == ()


def test_an_exhausted_investigation_does_not_block_the_release_that_supersedes_it():
    """Investigations ran out, THEN recovery published the green PR (#7293).

    The investigation's budget and its unlanded escalation say nothing about
    the new remedy: the sweep releases the review instead of landing
    needs-human on reviewable work.
    """
    from issue_orchestrator.control.published_review_release import PublishedReviewRelease
    from tests.unit.control.test_published_review_release import _Labels

    records, prs = _published(HELD)
    gatherer = _gatherer([_failed(HELD)], records=records, prs=prs)
    state = OrchestratorState()
    state.recovery_attempts = {HELD: 3}
    state.pending_stuck_sweep_escalations = {HELD}

    snapshot = gatherer.create_snapshot(state, issues=[])

    assert snapshot.stuck_sweep_escalations == ()
    (release,) = _release_actions(snapshot, HELD)
    lm = LabelManager(_config())
    assert not [
        a for a in Planner(config=_config(), scheduler=Scheduler(_config())).plan(snapshot).actions
        if getattr(a, "label", None) == lm.needs_human
    ]
    assert state.review_release_budgets == {HELD}
    assert state.recovery_attempts[HELD] == 0

    labels = _Labels(["agent:web", lm.blocked_failed])
    owner = PublishedReviewRelease(
        custody=gatherer.published_review, labels=lm, read_labels=labels.read, apply=labels.apply,
        review_label="needs-code-review",
    )
    assert owner.release(release.issue_number).released
    assert labels.live == {"agent:web", lm.pr_pending}
    assert labels.pr_live == {"needs-code-review"}


def test_the_remedy_a_budget_counts_survives_a_restart():
    from issue_orchestrator.control.stuck_sweep import (
        hydrate_stuck_sweep_state,
        persist_stuck_sweep_state,
    )
    from tests.unit.control.test_stuck_sweep import _InMemoryQueueCache

    store = _InMemoryQueueCache()
    before = OrchestratorState()
    before.recovery_attempts = {HELD: 1}
    before.review_release_budgets = {HELD}
    persist_stuck_sweep_state(before, store)

    after = OrchestratorState()
    hydrate_stuck_sweep_state(after, store)

    assert after.review_release_budgets == {HELD}


def test_a_block_with_its_own_owner_is_held_not_released():
    """A human's ``blocked`` beside the failure label is never lifted here."""
    lm = LabelManager(_config())
    records, prs = _published(HELD)
    gatherer = _gatherer([_failed(HELD, lm.blocked)], records=records, prs=prs)
    state = OrchestratorState()
    state.recovery_attempts = {HELD: 3}
    state.pending_stuck_sweep_escalations = {HELD}

    snapshot = gatherer.create_snapshot(state, issues=[])

    assert snapshot.stuck_sweep_review_releases == ()
    assert HELD not in {f.issue_number for f in snapshot.discovered_failures}
    # Its premise (nobody owns this issue) no longer holds: withdrawn.
    assert snapshot.stuck_sweep_escalations == ()
    assert state.pending_stuck_sweep_escalations == set()


def test_closing_the_pr_returns_the_issue_to_investigation():
    """Closing the PR is the operator's abandonment; the issue is stuck again."""
    records, prs = _published(HELD, pr_state="closed")
    gatherer = _gatherer([_failed(HELD)], records=records, prs=prs)

    snapshot = gatherer.create_snapshot(OrchestratorState(), issues=[])

    assert HELD in {failure.issue_number for failure in snapshot.discovered_failures}
    assert snapshot.stuck_sweep_review_releases == ()


def test_unresolved_work_is_not_review_ownership_and_costs_no_pr_read():
    records = {HELD: (disposition(HELD, ValidatedWorkState.FAILED),)}
    pulls = PullRequests({HELD: [pr(HELD, 500)]})
    gatherer = FactGatherer(
        config=_config(),
        repository_host=_RecordingHost([_failed(HELD)]),
        published_review=custody(DispositionStore(records), pulls),
    )

    snapshot = gatherer.create_snapshot(OrchestratorState(), issues=[])

    assert HELD in {failure.issue_number for failure in snapshot.discovered_failures}
    assert pulls.reads == []


def test_a_pr_with_its_own_block_is_held_not_released():
    """#7293 round 5: the PR's own block (a terminated review) owns it too."""
    lm = LabelManager(_config())
    records = {HELD: (disposition(HELD, ValidatedWorkState.RECOVERED, pr_number=500),)}
    blocked_pr = pr(HELD, 500, labels=(lm.blocked_failed,))
    gatherer = _gatherer([_failed(HELD)], records=records, prs={HELD: [blocked_pr]})
    state = OrchestratorState()

    snapshot = gatherer.create_snapshot(state, issues=[])

    assert snapshot.stuck_sweep_review_releases == ()
    assert HELD not in {f.issue_number for f in snapshot.discovered_failures}
    validity = evaluate_review_validity(
        config=_config(), label_manager=lm, issue=_issue(HELD, labels=["agent:web"]),
        pr=blocked_pr, review_label_confirmed=True)
    assert validity.reason == "pr_blocked", "lifting the issue block alone would not help"


def test_a_hold_that_ends_starts_a_fresh_investigation_budget():
    """#7293 round 6: held work supersedes an exhausted investigation.

    Exhausted investigations, then recovery publishes under a PR that carries
    its own block (held). When that PR closes, the issue must be investigated
    afresh - not skipped forever behind the old, already-withdrawn escalation.
    """
    lm = LabelManager(_config())
    records = {HELD: (disposition(HELD, ValidatedWorkState.RECOVERED, pr_number=500),)}
    pulls = PullRequests({HELD: [pr(HELD, 500, labels=(lm.blocked_failed,))]})
    gatherer = FactGatherer(
        config=_config(), repository_host=_RecordingHost([_failed(HELD)]),
        published_review=custody(DispositionStore(records), pulls),
    )
    state = OrchestratorState()
    state.recovery_attempts = {HELD: 3}
    state.pending_stuck_sweep_escalations = {HELD}

    held = gatherer.create_snapshot(state, issues=[])
    assert held.stuck_sweep_review_releases == ()
    assert held.stuck_sweep_escalations == ()
    assert HELD not in {f.issue_number for f in held.discovered_failures}

    pulls.by_issue[HELD] = [pr(HELD, 500, state="closed", labels=(lm.blocked_failed,))]
    state.last_stuck_sweep_at = 0.0
    state.discovered_failures.clear()
    state.stuck_sweep_escalations.clear()

    reopened = gatherer.create_snapshot(state, issues=[])
    assert HELD in {f.issue_number for f in reopened.discovered_failures}
