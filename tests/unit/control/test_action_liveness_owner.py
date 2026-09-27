"""The action liveness owner at its store and escalation ports (#7350)."""

from __future__ import annotations

from datetime import timedelta

from issue_orchestrator.domain.action_liveness import (
    ActionIdentity,
    ActionOutcome,
    Admission,
    LivenessKey,
    LivenessPolicy,
)
from issue_orchestrator.execution.action_liveness_store import SQLiteActionLivenessStore
from tests.unit.control.liveness_doubles import (
    InMemoryActionLivenessStore,
    ManualClock,
    RecordingEscalation,
    liveness_owner,
)

POLICY = LivenessPolicy(max_attempts=3)
KEY = LivenessKey(ActionIdentity("issue:229", "settle_tech_lead_promotion"), "a" * 32, 229)


def test_a_transient_failure_backs_off_then_readmits() -> None:
    clock = ManualClock()
    owner = liveness_owner(clock=clock, policy=POLICY)

    owner.record(KEY, ActionOutcome.transient("boom"))

    assert owner.admit(KEY).admission is Admission.BACKING_OFF
    clock.advance(POLICY.base_backoff)
    assert owner.admit(KEY).admitted


def test_budget_exhaustion_parks_and_escalates_exactly_once() -> None:
    clock = ManualClock()
    escalation = RecordingEscalation()
    store = InMemoryActionLivenessStore()
    owner = liveness_owner(store=store, escalation=escalation, clock=clock, policy=POLICY)

    for _ in range(POLICY.max_attempts):
        assert owner.admit(KEY).admitted
        owner.record(KEY, ActionOutcome.transient("registry is terminal"))
        clock.advance(timedelta(hours=1))
        owner.reconcile_effects()

    decision = owner.admit(KEY)
    assert decision.admission is Admission.PARKED
    assert "registry is terminal" in decision.describe()
    assert len(escalation.parked) == 1
    assert len(escalation.blocks) == 1
    assert store.row(KEY).escalated is True
    clock.advance(timedelta(days=30))
    owner.reconcile_effects()
    assert owner.admit(KEY).admission is Admission.PARKED
    assert len(escalation.blocks) == 1


def test_a_block_github_refused_is_retried_until_it_lands() -> None:
    """The park is durable; so is the debt of showing it to a person."""
    clock = ManualClock()
    escalation = RecordingEscalation(commits=False)
    store = InMemoryActionLivenessStore()
    owner = liveness_owner(store=store, escalation=escalation, clock=clock, policy=POLICY)

    owner.record(KEY, ActionOutcome.permanent("422"))
    assert store.row(KEY).escalated is False

    owner.reconcile_effects()  # not yet due: paced at max_backoff
    assert len(escalation.blocks) == 1
    escalation.commits = True
    clock.advance(POLICY.max_backoff)
    owner.reconcile_effects()

    assert store.row(KEY).escalated is True
    assert [committed for _row, committed in escalation.blocks] == [False, True]
    assert len(escalation.parked) == 1, "announced once, however often the block is retried"


def test_a_block_github_never_accepts_stops_being_retried() -> None:
    clock = ManualClock()
    escalation = RecordingEscalation(commits=False)
    owner = liveness_owner(escalation=escalation, clock=clock, policy=POLICY)

    owner.record(KEY, ActionOutcome.permanent("422"))
    for _ in range(20):
        clock.advance(POLICY.max_backoff)
        owner.reconcile_effects()

    assert len(escalation.blocks) == POLICY.max_attempts
    assert [row.key for row in owner.parked()] == [KEY], "still held and on the board"


def test_a_comment_github_refused_is_retried_without_relabelling() -> None:
    """The block landed, its explanation did not: only the comment is owed."""
    clock = ManualClock()
    escalation = RecordingEscalation(explain_commits=False)
    store = InMemoryActionLivenessStore()
    owner = liveness_owner(store=store, escalation=escalation, clock=clock, policy=POLICY)

    owner.record(KEY, ActionOutcome.permanent("422"))
    row = store.row(KEY)
    assert row.escalated is True and row.explained is False

    escalation.explain_commits = True
    clock.advance(POLICY.max_backoff)
    owner.reconcile_effects()
    clock.advance(POLICY.max_backoff)
    owner.reconcile_effects()

    assert len(escalation.blocks) == 1
    assert [committed for _row, committed in escalation.explanations] == [False, True]
    assert store.row(KEY).explained is True


def test_changed_facts_are_a_new_question() -> None:
    owner = liveness_owner(policy=POLICY)
    owner.record(KEY, ActionOutcome.needs_human("paused"))
    changed = LivenessKey(KEY.identity, "b" * 32, 229)

    assert owner.admit(KEY).admission is Admission.PARKED
    assert owner.admit(changed).admitted


def test_success_under_any_fingerprint_clears_the_identity_and_releases_the_block() -> None:
    escalation = RecordingEscalation()
    store = InMemoryActionLivenessStore()
    owner = liveness_owner(store=store, escalation=escalation)
    owner.record(KEY, ActionOutcome.permanent("stuck"))
    changed = LivenessKey(KEY.identity, "b" * 32, 229)

    owner.record(changed, ActionOutcome.done())

    assert owner.admit(KEY).admitted
    assert [[row.key for row in rows] for rows in escalation.released] == [[KEY]]
    assert escalation.unblocks == [(229, True)]
    assert store.releases == {}


def test_a_release_github_refused_is_retried_until_it_lands() -> None:
    clock = ManualClock()
    escalation = RecordingEscalation(unblock_commits=False)
    store = InMemoryActionLivenessStore()
    owner = liveness_owner(store=store, escalation=escalation, clock=clock, policy=POLICY)
    owner.record(KEY, ActionOutcome.permanent("stuck"))

    owner.record(KEY, ActionOutcome.done())
    assert store.rows == {} and 229 in store.releases

    escalation.unblock_commits = True
    clock.advance(POLICY.max_backoff)
    owner.reconcile_effects()

    assert escalation.unblocks == [(229, False), (229, True)]
    assert store.releases == {}


def test_success_keeps_the_block_while_another_park_stands_on_the_issue() -> None:
    escalation = RecordingEscalation()
    owner = liveness_owner(escalation=escalation)
    other = LivenessKey(ActionIdentity("issue:229", "add_comment"), "c" * 32, 229)
    owner.record(KEY, ActionOutcome.permanent("stuck"))
    owner.record(other, ActionOutcome.permanent("also stuck"))

    owner.record(KEY, ActionOutcome.done())

    assert [[row.key for row in rows] for rows in escalation.released] == [[KEY]]
    assert escalation.unblocks == []
    assert owner.admit(other).admission is Admission.PARKED


def test_operator_release_gives_every_key_on_the_issue_a_fresh_budget() -> None:
    escalation = RecordingEscalation(unblock_commits=False)
    store = InMemoryActionLivenessStore()
    owner = liveness_owner(store=store, escalation=escalation)
    owner.record(KEY, ActionOutcome.permanent("stuck"))
    unrelated = LivenessKey(ActionIdentity("issue:7", "add_label"), "d" * 32, 7)
    owner.record(unrelated, ActionOutcome.permanent("stuck"))
    store.request_release(229)

    released = owner.release_issue(229)

    assert [row.key for row in released] == [KEY]
    assert owner.admit(KEY).admitted
    assert owner.admit(unrelated).admission is Admission.PARKED
    # The operator command settled the label itself: nothing is withdrawn
    # here, and no withdrawal is left owed.
    assert escalation.unblocks == [] and store.releases == {}


def test_a_release_is_owed_durably_the_moment_the_park_is_forgotten(tmp_path) -> None:
    """A crash right after success forgets the park; the debt must survive it."""
    path = tmp_path / "action_liveness.sqlite"
    owner = liveness_owner(store=SQLiteActionLivenessStore(path), policy=POLICY)
    owner.record(KEY, ActionOutcome.permanent("stuck"))

    # The store's half of success, with no owner afterwards (the crash).
    SQLiteActionLivenessStore(path).clear_identity(KEY.identity)

    escalation = RecordingEscalation()
    restarted = liveness_owner(
        store=SQLiteActionLivenessStore(path), escalation=escalation, policy=POLICY
    )
    restarted.reconcile_effects()
    assert escalation.unblocks == [(229, True)]
    assert SQLiteActionLivenessStore(path).pending_releases() == ()


def test_the_budget_survives_an_engine_restart(tmp_path) -> None:
    """A budget that lives in memory resets on restart, and a crash-looping
    engine then retries forever. The rows are the owner's only memory."""
    path = tmp_path / "action_liveness.sqlite"
    clock = ManualClock()
    for _ in range(POLICY.max_attempts):
        restarted = liveness_owner(
            store=SQLiteActionLivenessStore(path), clock=clock, policy=POLICY
        )
        assert restarted.admit(KEY).admitted
        restarted.record(KEY, ActionOutcome.transient("boom"))
        clock.advance(timedelta(hours=1))

    fresh = liveness_owner(store=SQLiteActionLivenessStore(path), clock=clock, policy=POLICY)
    assert fresh.admit(KEY).admission is Admission.PARKED


def test_an_operator_release_forgets_an_owed_withdrawal_in_the_same_step(tmp_path) -> None:
    """Replayed after a person re-added needs-human, a stale withdrawal would
    take their block off (review round 3)."""
    path = tmp_path / "action_liveness.sqlite"
    escalation = RecordingEscalation(unblock_commits=False)
    owner = liveness_owner(store=SQLiteActionLivenessStore(path), escalation=escalation)
    owner.record(KEY, ActionOutcome.permanent("stuck"))
    owner.record(KEY, ActionOutcome.done())  # the withdrawal is now owed
    owner.record(KEY, ActionOutcome.permanent("stuck again"))

    SQLiteActionLivenessStore(path).clear_escalation_issue(229)  # then the crash

    after = RecordingEscalation()
    clock = ManualClock()
    clock.advance(timedelta(days=1))
    restarted = liveness_owner(
        store=SQLiteActionLivenessStore(path), escalation=after, clock=clock
    )
    restarted.reconcile_effects()
    assert after.unblocks == []


def test_success_keeps_the_rows_of_operations_still_planned() -> None:
    owner = liveness_owner()
    sibling = LivenessKey(KEY.identity, "b" * 32, 229)
    owner.record(sibling, ActionOutcome.permanent("still broken"))

    owner.record(KEY, ActionOutcome.done(), still_planned=frozenset({"b" * 32}))

    assert owner.admit(sibling).admission is Admission.PARKED
