"""A reset-retry never trips its own reconciliation pause (#8219).

porchpin #364, 2026-10-04: the dashboard's from-scratch reset ran on the web
thread while a tick was mid-pass. The tick had observed PR #379 closed while
``pr-pending`` was still on the issue and planned the closed-PR drift repair
(``SyncLabelsAction`` requiring ``pr-pending``). The reset removed
``pr-pending``; the tick then applied its plan, the mutation gate refused it as
drift, and the issue was paused behind ``io:needs-reconcile`` until a person
cleared it eleven hours later.

These tests pin the three ways a reset's own effects reached that pause:

* the reset straddling a tick's observe-plan-apply pass (it now runs under the
  facade's state lock, the lock the tick holds for its whole pass);
* a fact observed before the reset outliving it - a paused tick retains its
  facts, and a non-scratch reset used to keep them;
* a pause or park the liveness owner still owed the issue from before, and
  the stuck sweep's recovery budget and unlanded needs-human escalation.
"""

from __future__ import annotations

import threading
from unittest.mock import MagicMock, Mock, patch

from fastapi.testclient import TestClient

from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.maintenance import ResetResult
from issue_orchestrator.domain.models import (
    DiscoveredAwaitingMergeDrift,
    DiscoveredRetrospectiveReview,
)
from issue_orchestrator.entrypoints.web import app, set_orchestrator
from tests.unit.control.liveness_doubles import (
    TICK,
    InMemoryActionLivenessStore,
    RecordingEscalation,
    liveness_owner,
)
from tests.unit.test_web import create_issue, create_mock_orchestrator

ISSUE = 364
OTHER = 999


def _drift(issue_number: int) -> DiscoveredAwaitingMergeDrift:
    return DiscoveredAwaitingMergeDrift(
        issue_number=issue_number,
        pr_number=379 if issue_number == ISSUE else 1000,
        pr_url=f"https://github.com/o/r/pull/{issue_number}",
        status_reason="PR closed; issue remains open",
    )


def _dashboard():
    """A dashboard over a mocked engine whose reset collaborators succeed."""
    orchestrator = create_mock_orchestrator()
    labels = LabelManager(orchestrator.config)
    orchestrator.deps.label_manager = labels
    orchestrator.deps.action_applier = MagicMock()
    orchestrator.deps.action_applier.apply.return_value = Mock(success=True, error=None)
    orchestrator.deps.events = MagicMock()
    orchestrator.deps.queue_cache_store = MagicMock()
    orchestrator.repository_host.get_issue_labels.return_value = ["agent:web", labels.pr_pending]
    orchestrator.repository_host.get_issue.return_value = create_issue(
        ISSUE, labels=["agent:web", labels.reset_retry_pending]
    )
    set_orchestrator(orchestrator)
    return orchestrator, labels


def _reset_result(labels: LabelManager) -> ResetResult:
    return ResetResult(success=True, issue_number=ISSUE, labels_removed=[labels.pr_pending])


def test_a_reset_waits_for_the_tick_that_holds_the_state_lock():
    """The incident's straddle: the reset may not run while a tick is mid-pass.

    The test holds the facade's state lock as the tick does for its whole
    observe-plan-apply pass. Nothing of the reset - not even its fresh label
    read - may run until the tick lets go; then the whole reset runs.
    """
    orchestrator, labels = _dashboard()
    reset_began = threading.Event()
    asked_for_lock = threading.Event()
    orchestrator.repository_host.get_issue_labels.side_effect = lambda _n: (
        reset_began.set() or ["agent:web", labels.pr_pending]
    )

    def run_locked(fn):
        asked_for_lock.set()
        with orchestrator.state_lock:
            return fn()

    orchestrator.run_locked = run_locked
    responses: list = []

    with patch("issue_orchestrator.control.maintenance.reset_issue") as reset_issue:
        reset_issue.return_value = _reset_result(labels)
        client = TestClient(app)
        with orchestrator.state_lock:  # a tick is mid-pass
            request = threading.Thread(
                target=lambda: responses.append(
                    client.post("/api/reset-retry", json={"issues": [ISSUE]})
                )
            )
            request.start()
            # Deterministic: once the reset has asked for the lock this thread
            # holds, nothing of it can run until the "tick" lets go.
            assert asked_for_lock.wait(timeout=10), "the reset never waited for the tick"
            assert not reset_began.is_set(), "the reset ran inside the tick's pass"
            reset_issue.assert_not_called()
        request.join(timeout=30)

    assert not request.is_alive()
    assert responses[0].status_code == 200
    assert [entry["issue"] for entry in responses[0].json()["reset"]] == [ISSUE]
    reset_issue.assert_called_once()


def test_every_step_of_the_reset_runs_under_the_runner_it_was_given():
    """The tech-lead path shares the pipeline, so the lock is the pipeline's own.

    A runner that records whether it is inside its callable: every collaborator
    the reset touches - the runtime termination, the label read, the reset, the
    pending labels - must see it inside.
    """
    from issue_orchestrator.control.queue_cache import QueueCache
    from issue_orchestrator.entrypoints.web_retry_history_routes import reset_and_retry_issue

    orchestrator, labels = _dashboard()
    inside = {"now": False}
    seen: list[tuple[str, bool]] = []

    def run_locked(fn):
        inside["now"] = True
        try:
            return fn()
        finally:
            inside["now"] = False

    def observing(name, result):
        def call(*_args, **_kwargs):
            seen.append((name, inside["now"]))
            return result

        return call

    orchestrator.repository_host.get_issue_labels.side_effect = observing(
        "read_labels", ["agent:web", labels.pr_pending]
    )
    orchestrator.deps.action_applier.apply.side_effect = observing(
        "pending_label", Mock(success=True, error=None)
    )
    success, failure = reset_and_retry_issue(
        issue_number=ISSUE,
        from_scratch=True,
        pending_label=labels.reset_retry_pending,
        scratch_pending_label=labels.reset_retry_scratch_pending,
        repository_host=orchestrator.repository_host,
        queue_cache=QueueCache(
            orchestrator.config, orchestrator.state, orchestrator.deps.queue_cache_store
        ),
        state=orchestrator.state,
        deps=orchestrator.deps,
        config=orchestrator.config,
        reset_issue_fn=observing("reset", _reset_result(labels)),
        run_locked=run_locked,
    )

    assert failure is None and success is not None
    assert [name for name, _ in seen] == [
        "read_labels", "reset", "pending_label", "pending_label",
    ]
    assert all(locked for _, locked in seen), seen


def test_a_reset_forgets_every_fact_observed_before_it():
    """A paused tick keeps its facts; a reset of either mode must not.

    The closed-PR drift fact for the issue was observed while ``pr-pending``
    was still on. Planned after the reset removed it, its repair is refused as
    drift and pauses the issue. A plain (non-scratch) reset used to keep it.
    """
    orchestrator, labels = _dashboard()
    orchestrator.state.discovered_awaiting_merge_drifts = [_drift(ISSUE), _drift(OTHER)]
    orchestrator.state.discovered_retrospective_reviews = [
        DiscoveredRetrospectiveReview(
            issue_number=number,
            issue_title="t",
            agent_label="agent:web",
            trigger_label="lack-of-review-redo",
        )
        for number in (ISSUE, OTHER)
    ]

    with patch("issue_orchestrator.control.maintenance.reset_issue") as reset_issue:
        reset_issue.return_value = _reset_result(labels)
        response = TestClient(app).post("/api/reset-retry", json={"issues": [ISSUE]})

    assert response.status_code == 200
    assert response.json()["failed"] == []
    assert [d.issue_number for d in orchestrator.state.discovered_awaiting_merge_drifts] == [
        OTHER
    ]
    assert [
        d.issue_number for d in orchestrator.state.discovered_retrospective_reviews
    ] == [OTHER]


def test_a_reset_settles_the_pause_the_liveness_owner_still_owed():
    """A pause owed from before the reset must not land after it.

    The owed pause was decided on pre-reset facts; GitHub refused it, so the
    owner would retry it on a later planning cycle. The reset is the answer to it, as an
    operator's retry is: once the reset commits, nothing re-pauses the issue.
    """
    orchestrator, labels = _dashboard()
    store = InMemoryActionLivenessStore()
    escalation = RecordingEscalation(pause_commits=False)
    owner = liveness_owner(store=store, escalation=escalation)
    orchestrator.deps.action_liveness.owner = owner
    owner.owe_pause(ISSUE, "Missing required labels: frozenset({'pr-pending'})", TICK)
    owner.owe_pause(OTHER, "drift elsewhere", TICK)
    assert [pause.issue_number for pause in store.pending_pauses()] == [ISSUE, OTHER]

    with patch("issue_orchestrator.control.maintenance.reset_issue") as reset_issue:
        reset_issue.return_value = _reset_result(labels)
        response = TestClient(app).post("/api/reset-retry", json={"issues": [ISSUE]})

    assert response.status_code == 200
    # Forgotten, not merely deferred: no later planning cycle can land it.
    assert [pause.issue_number for pause in store.pending_pauses()] == [OTHER]


def test_a_reset_ends_the_stuck_sweeps_record_of_the_old_attempt():
    """Review r1 F1: an unlanded sweep escalation must not block the fresh retry.

    The sweep exhausted the old attempt's recovery budget and owes it a
    needs-human escalation, re-emitted every plan until it lands. After the
    reset, the next plan would block the fresh retry behind ``needs-human``.
    The record is forgotten and persisted, so a restart cannot hydrate it back.
    """
    from issue_orchestrator.control.stuck_sweep import build_stuck_sweep_escalation_actions

    orchestrator, labels = _dashboard()
    state = orchestrator.state
    state.recovery_attempts = {ISSUE: 3, OTHER: 3}
    state.pending_stuck_sweep_escalations = {ISSUE, OTHER}
    state.review_release_budgets = {ISSUE, OTHER}
    state.stuck_sweep_escalations = [ISSUE, OTHER]
    state.stuck_sweep_review_releases = [ISSUE, OTHER]
    state.stuck_sweep_held_for_review = frozenset({ISSUE, OTHER})

    with patch("issue_orchestrator.control.maintenance.reset_issue") as reset_issue:
        reset_issue.return_value = _reset_result(labels)
        response = TestClient(app).post("/api/reset-retry", json={"issues": [ISSUE]})

    assert response.status_code == 200
    assert state.recovery_attempts == {OTHER: 3}
    assert state.pending_stuck_sweep_escalations == {OTHER}
    assert state.review_release_budgets == {OTHER}
    assert state.stuck_sweep_escalations == [OTHER]
    assert state.stuck_sweep_review_releases == [OTHER]
    assert state.stuck_sweep_held_for_review == frozenset({OTHER})
    store = orchestrator.deps.queue_cache_store
    store.save_pending_escalations.assert_called_with({OTHER})
    store.save_recovery_attempts.assert_called_with({OTHER: 3})
    escalations = build_stuck_sweep_escalation_actions(
        tuple(state.stuck_sweep_escalations), labels.needs_human
    )
    assert [action.issue_number for action in escalations] == [OTHER]
