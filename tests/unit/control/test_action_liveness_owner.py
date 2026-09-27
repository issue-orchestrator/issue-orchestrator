"""The action liveness owner at its store and escalation ports (#7350)."""

from __future__ import annotations

from datetime import timedelta
from unittest.mock import MagicMock

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
    for _ in range(30):  # still planned every day for a month
        clock.advance(timedelta(days=1))
        assert owner.admit(KEY).admission is Admission.PARKED
        owner.reconcile_effects()
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
        owner.admit(KEY)  # still planned
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


def test_success_clears_its_own_key_and_releases_the_block() -> None:
    escalation = RecordingEscalation()
    store = InMemoryActionLivenessStore()
    owner = liveness_owner(store=store, escalation=escalation)
    owner.record(KEY, ActionOutcome.permanent("stuck"))

    owner.record(KEY, ActionOutcome.done())

    assert owner.admit(KEY).admitted
    assert [[row.key for row in rows] for rows in escalation.released] == [[KEY]]
    assert escalation.unblocks == [(229, True)]
    assert store.releases == {}


def test_a_park_nobody_asks_about_any_more_is_retired_and_released() -> None:
    """Its facts changed (the action now plans under a new fingerprint) or it
    is no longer wanted: either way it is not a question, so it leaves the
    board and its block is withdrawn (review r8)."""
    clock = ManualClock()
    escalation = RecordingEscalation()
    owner = liveness_owner(escalation=escalation, clock=clock, policy=POLICY)
    owner.record(KEY, ActionOutcome.permanent("stuck"))
    changed = LivenessKey(KEY.identity, "b" * 32, 229)

    clock.advance(POLICY.stale_after / 2)
    owner.admit(changed)
    owner.record(changed, ActionOutcome.done())
    owner.reconcile_effects()
    assert owner.admit(KEY).admission is Admission.PARKED, "asked again: still parked"

    # The new facts keep succeeding; the old question is not asked again.
    for _ in range(3):
        clock.advance(POLICY.stale_after / 2)
        owner.admit(changed)
        owner.record(changed, ActionOutcome.done())
        owner.reconcile_effects()

    assert owner.parked() == ()
    assert escalation.unblocks == [(229, True)]


def test_a_park_on_a_slow_cadence_keeps_its_budget() -> None:
    """Asked only every few hours (the stuck sweep's cadence) or after a long
    engine pause, with unchanged facts: still the same question (review r10)."""
    clock = ManualClock()
    owner = liveness_owner(clock=clock, policy=POLICY)
    owner.record(KEY, ActionOutcome.permanent("stuck"))

    for _ in range(12):
        clock.advance(timedelta(hours=4))
        owner.reconcile_effects()
        assert owner.admit(KEY).admission is Admission.PARKED

    clock.advance(POLICY.abandon_after + timedelta(hours=1))
    owner.reconcile_effects()
    assert owner.parked() == (), "abandoned once nobody asks for abandon_after"


def test_a_sibling_operations_success_never_clears_a_park_still_asked_about() -> None:
    """Two comments on one issue share an identity: B's success clears only B,
    whether or not A is in the same plan (review r8)."""
    clock = ManualClock()
    owner = liveness_owner(clock=clock, policy=POLICY)
    sibling = LivenessKey(KEY.identity, "b" * 32, 229)
    owner.record(KEY, ActionOutcome.permanent("still broken"))

    for _ in range(10):
        clock.advance(POLICY.stale_after / 3)
        owner.admit(sibling)
        owner.record(sibling, ActionOutcome.done())
        if _ % 2 == 0:
            assert owner.admit(KEY).admission is Admission.PARKED
        owner.reconcile_effects()

    assert [row.key for row in owner.parked()] == [KEY]


def test_an_operator_can_release_a_park_no_issue_carries() -> None:
    engine_key = LivenessKey(ActionIdentity("engine", "create_tech_lead_issue"), "c" * 32)
    owner = liveness_owner(policy=POLICY)
    owner.record(engine_key, ActionOutcome.permanent("403"))

    released = owner.release_identity(engine_key.identity)

    assert [row.key for row in released] == [engine_key]
    assert owner.admit(engine_key).admitted


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
    store = InMemoryActionLivenessStore()
    owner = liveness_owner(store=store, escalation=escalation)
    other = LivenessKey(ActionIdentity("issue:229", "add_comment"), "c" * 32, 229)
    owner.record(KEY, ActionOutcome.permanent("stuck"))
    owner.record(other, ActionOutcome.permanent("also stuck"))

    owner.record(KEY, ActionOutcome.done())

    assert [[row.key for row in rows] for rows in escalation.released] == [[KEY]]
    assert escalation.unblocks == []
    assert store.releases == {}, "the other park's landed block owns the label now"
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
    SQLiteActionLivenessStore(path).clear_key(KEY, done_at=ManualClock().now)

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


def test_a_release_during_the_block_write_cannot_resurrect_the_park(tmp_path) -> None:
    """The engine is writing the block when an operator releases the park from
    the CLI (a second connection). The park stays released, and the block that
    landed anyway is owed its withdrawal (review r10)."""
    from issue_orchestrator.control.action_liveness import release_parked_action

    path = tmp_path / "action_liveness.sqlite"

    class _ReleasedMidWrite(RecordingEscalation):
        def block(self, row):
            release_parked_action(SQLiteActionLivenessStore(path), row.key.identity)
            return super().block(row)

    escalation = _ReleasedMidWrite()
    owner = liveness_owner(store=SQLiteActionLivenessStore(path), escalation=escalation)

    owner.record(KEY, ActionOutcome.permanent("403"))

    store = SQLiteActionLivenessStore(path)
    assert store.parked_rows() == ()
    assert escalation.unblocks == [(229, True)]
    assert store.pending_releases() == ()
    owner.reconcile_effects()
    assert [[row.key for row in rows] for rows in escalation.released] == [[KEY]]


def test_a_crash_before_publishing_a_park_still_announces_it(tmp_path, mock_event_sink) -> None:
    """The park and its announcement commit together; a restarted owner
    publishes it to the timeline (review r14)."""
    from issue_orchestrator.control.action_liveness_escalation import ActionLivenessEscalation
    from issue_orchestrator.events import EventName

    path = tmp_path / "action_liveness.sqlite"
    row = liveness_owner(store=SQLiteActionLivenessStore(path)).record(
        KEY, ActionOutcome.permanent("stuck")
    )
    # Simulate the crash: the owed announcement is still in the outbox.
    store = SQLiteActionLivenessStore(path)
    assert store.settle(row, row, announce_parked=True)

    class _Applier:
        def apply(self, action):
            from issue_orchestrator.control.actions import ActionResult

            return ActionResult.ok(action)

    restarted = liveness_owner(
        store=SQLiteActionLivenessStore(path),
        escalation=ActionLivenessEscalation(
            events=mock_event_sink, applier=_Applier(), needs_human_label="needs-human"
        ),
    )
    restarted.reconcile_effects()

    [event] = mock_event_sink.get_events_by_name(EventName.ACTION_PARKED)
    assert event.data["subject"] == "issue:229"
    restarted.reconcile_effects()
    assert len(mock_event_sink.get_events_by_name(EventName.ACTION_PARKED)) == 1


def test_the_block_stays_while_a_park_whose_own_block_has_not_landed_stands(tmp_path) -> None:
    """Park A's block landed; park B's did not. Releasing A must leave the
    label, which is B's block too; once B is gone it comes off (review r15)."""
    from issue_orchestrator.control.action_liveness_escalation import ActionLivenessEscalation
    from issue_orchestrator.control.actions import ActionResult, AddLabelAction
    from tests.unit.control.test_action_liveness_escalation import _shared_block

    labels, block = _shared_block(tmp_path)

    class _Applier:
        refuse_labels = False

        def apply(self, action):
            from issue_orchestrator.control.actions import AddCommentAction, RemoveLabelAction
            from issue_orchestrator.domain.human_block import HumanBlockRequest

            if isinstance(action, AddCommentAction):
                return ActionResult.ok(action)
            request = HumanBlockRequest(action.issue_number, action.needs_human_cause, "r")
            if isinstance(action, AddLabelAction):
                if self.refuse_labels:
                    return ActionResult.fail(action, "502")
                return ActionResult.ok(action) if block.acquire(request).committed else ActionResult.fail(action, "x")
            assert isinstance(action, RemoveLabelAction)
            block.release(request)
            return ActionResult.ok(action)

    applier = _Applier()
    clock = ManualClock()
    owner = liveness_owner(
        escalation=ActionLivenessEscalation(
            events=MagicMock(),
            applier=applier, needs_human_label="needs-human",
        ),
        clock=clock, policy=POLICY,
    )
    first = KEY
    second = LivenessKey(ActionIdentity("issue:229", "add_comment#x"), "e" * 32, 229)
    owner.record(first, ActionOutcome.permanent("stuck"))
    applier.refuse_labels = True
    owner.record(second, ActionOutcome.permanent("also stuck"))
    applier.refuse_labels = False

    owner.record(first, ActionOutcome.done())
    assert "needs-human" in labels.live[229], "still B's block"

    owner.release_identity(second.identity)
    clock.advance(POLICY.max_backoff)
    owner.reconcile_effects()
    assert "needs-human" not in labels.live[229]


def test_a_release_during_an_attempt_is_not_undone_by_its_settlement(tmp_path) -> None:
    """The operator releases a key between the attempt's admission and its
    settlement (a second connection). The stale settlement is discarded: no
    resurrected row, no park, no block (review r18)."""
    from issue_orchestrator.control.action_liveness import release_parked_action

    path = tmp_path / "action_liveness.sqlite"
    escalation = RecordingEscalation()
    policy = LivenessPolicy(max_attempts=5)
    clock = ManualClock()
    engine_store = SQLiteActionLivenessStore(path)
    owner = liveness_owner(
        store=engine_store, escalation=escalation, clock=clock, policy=policy
    )
    for _ in range(4):
        clock.advance(timedelta(hours=1))
        owner.admit(KEY)
        owner.record(KEY, ActionOutcome.transient("boom"))

    clock.advance(timedelta(hours=1))
    assert owner.admit(KEY).admitted
    read = engine_store.row

    def read_then_operator_releases(key):
        found = read(key)
        release_parked_action(SQLiteActionLivenessStore(path), KEY.identity)
        return found

    engine_store.row = read_then_operator_releases  # type: ignore[method-assign]
    assert owner.record(KEY, ActionOutcome.transient("boom")) is None

    store = SQLiteActionLivenessStore(path)
    assert store.row(KEY) is None
    assert escalation.parked == [] and escalation.blocks == []


def test_a_release_racing_the_withdrawal_decision_keeps_its_debt(tmp_path) -> None:
    """Two escalated parks on one issue. Clearing the first decides whether
    the block is still the other park's; the operator CLI releases that other
    park (a second connection) right after the engine's first store step. Its
    owed withdrawal must survive, and the next reconcile takes the block off
    (review r21)."""
    from issue_orchestrator.control.action_liveness import release_parked_action
    from issue_orchestrator.control.action_liveness_escalation import ActionLivenessEscalation
    from issue_orchestrator.control.actions import (
        ActionResult,
        AddCommentAction,
        AddLabelAction,
        RemoveLabelAction,
    )
    from issue_orchestrator.domain.human_block import HumanBlockRequest
    from tests.unit.control.test_action_liveness_escalation import _shared_block

    labels, block = _shared_block(tmp_path)

    class _Applier:
        def apply(self, action):
            if isinstance(action, AddCommentAction):
                return ActionResult.ok(action)
            request = HumanBlockRequest(action.issue_number, action.needs_human_cause, "r")
            if isinstance(action, AddLabelAction):
                committed = block.acquire(request).committed
                return ActionResult.ok(action) if committed else ActionResult.fail(action, "x")
            assert isinstance(action, RemoveLabelAction)
            block.release(request)
            return ActionResult.ok(action)

    path = tmp_path / "action_liveness.sqlite"
    engine_store = SQLiteActionLivenessStore(path)
    clock = ManualClock()
    owner = liveness_owner(
        store=engine_store,
        escalation=ActionLivenessEscalation(
            events=MagicMock(), applier=_Applier(), needs_human_label="needs-human",
        ),
        clock=clock, policy=POLICY,
    )
    first = KEY
    second = LivenessKey(ActionIdentity("issue:229", "add_comment#x"), "e" * 32, 229)
    owner.record(first, ActionOutcome.permanent("stuck"))
    owner.record(second, ActionOutcome.permanent("also stuck"))
    assert "needs-human" in labels.live[229]

    released: list[bool] = []

    def then_operator_releases(method):
        def step(issue_number):
            answer = method(issue_number)
            if not released:
                released.append(True)
                release_parked_action(SQLiteActionLivenessStore(path), second.identity)
            return answer
        return step

    for name in ("parked_rows_for_issue", "clear_release_if_escalated_park"):
        setattr(engine_store, name, then_operator_releases(getattr(engine_store, name)))

    owner.record(first, ActionOutcome.done())

    assert released, "the operator's release ran inside the withdrawal decision"
    assert [p.issue_number for p in SQLiteActionLivenessStore(path).pending_releases()] == [229]
    clock.advance(POLICY.max_backoff)
    owner.reconcile_effects()
    assert "needs-human" not in labels.live[229]


def test_a_withdrawal_never_takes_off_a_block_landed_while_it_decided(tmp_path) -> None:
    """Clearing the last park reads "no park" and is about to withdraw the
    block; another thread (the drain's worker) parks and escalates a second
    action on the same issue meanwhile. The second park's block must stand
    (review r25)."""
    import threading

    from issue_orchestrator.control.action_liveness_escalation import ActionLivenessEscalation
    from issue_orchestrator.control.actions import (
        ActionResult,
        AddCommentAction,
        AddLabelAction,
        RemoveLabelAction,
    )
    from issue_orchestrator.domain.human_block import HumanBlockRequest
    from tests.unit.control.test_action_liveness_escalation import _shared_block

    labels, block = _shared_block(tmp_path)

    class _Applier:
        def apply(self, action):
            if isinstance(action, AddCommentAction):
                return ActionResult.ok(action)
            request = HumanBlockRequest(action.issue_number, action.needs_human_cause, "r")
            if isinstance(action, AddLabelAction):
                committed = block.acquire(request).committed
                return ActionResult.ok(action) if committed else ActionResult.fail(action, "x")
            assert isinstance(action, RemoveLabelAction)
            block.release(request)
            return ActionResult.ok(action)

    engine_store = SQLiteActionLivenessStore(tmp_path / "action_liveness.sqlite")
    owner = liveness_owner(
        store=engine_store,
        escalation=ActionLivenessEscalation(
            events=MagicMock(), applier=_Applier(), needs_human_label="needs-human",
        ),
        clock=ManualClock(), policy=POLICY,
    )
    first = KEY
    second = LivenessKey(ActionIdentity("issue:229", "add_comment#x"), "e" * 32, 229)
    owner.record(first, ActionOutcome.permanent("stuck"))
    assert "needs-human" in labels.live[229]

    decided, resume = threading.Event(), threading.Event()
    read = engine_store.parked_rows_for_issue

    def read_then_pause(issue_number):
        rows = read(issue_number)
        if not decided.is_set():
            decided.set()
            resume.wait(timeout=10)
        return rows

    engine_store.parked_rows_for_issue = read_then_pause  # type: ignore[method-assign]
    clearing = threading.Thread(target=lambda: owner.record(first, ActionOutcome.done()))
    clearing.start()
    assert decided.wait(timeout=10)
    parking = threading.Thread(
        target=lambda: owner.record(second, ActionOutcome.permanent("also stuck"))
    )
    parking.start()
    parking.join(timeout=1.0)  # an unserialized escalation lands here
    resume.set()
    clearing.join(timeout=10)
    parking.join(timeout=10)

    assert "needs-human" in labels.live[229], "the second park's block stands"
    row = SQLiteActionLivenessStore(tmp_path / "action_liveness.sqlite").row(second)
    assert row is not None and row.parked and row.escalated
