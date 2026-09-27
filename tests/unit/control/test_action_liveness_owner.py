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

    decision = owner.admit(KEY)
    assert decision.admission is Admission.PARKED
    assert "registry is terminal" in decision.describe()
    assert len(escalation.escalated) == 1
    assert store.row(KEY).escalated is True
    clock.advance(timedelta(days=30))
    assert owner.admit(KEY).admission is Admission.PARKED


def test_an_uncommitted_escalation_is_recorded_as_such() -> None:
    store = InMemoryActionLivenessStore()
    owner = liveness_owner(store=store, escalation=RecordingEscalation(commits=False))

    owner.record(KEY, ActionOutcome.permanent("422"))

    assert store.row(KEY).parked and store.row(KEY).escalated is False


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
    [(rows, release)] = escalation.resolved
    assert [row.key for row in rows] == [KEY] and release is True


def test_success_keeps_the_block_while_another_park_stands_on_the_issue() -> None:
    escalation = RecordingEscalation()
    owner = liveness_owner(escalation=escalation)
    other = LivenessKey(ActionIdentity("issue:229", "add_comment"), "c" * 32, 229)
    owner.record(KEY, ActionOutcome.permanent("stuck"))
    owner.record(other, ActionOutcome.permanent("also stuck"))

    owner.record(KEY, ActionOutcome.done())

    [(rows, release)] = escalation.resolved
    assert [row.key for row in rows] == [KEY] and release is False
    assert owner.admit(other).admission is Admission.PARKED


def test_operator_release_gives_every_key_on_the_issue_a_fresh_budget() -> None:
    escalation = RecordingEscalation()
    owner = liveness_owner(escalation=escalation)
    owner.record(KEY, ActionOutcome.permanent("stuck"))
    unrelated = LivenessKey(ActionIdentity("issue:7", "add_label"), "d" * 32, 7)
    owner.record(unrelated, ActionOutcome.permanent("stuck"))

    released = owner.release_issue(229)

    assert [row.key for row in released] == [KEY]
    assert owner.admit(KEY).admitted
    assert owner.admit(unrelated).admission is Admission.PARKED
    # The operator command settled the label itself; this only announces.
    assert escalation.resolved[-1][1] is False


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
