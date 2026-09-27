"""The recovery drain consults the action liveness owner (#7350).

Census loops #5 (a publish that fails the same way every drain pass), #6 (a
completion rejected on every pass) and #7 (records re-selected forever with
nothing to publish) all had the same shape: the drain re-selected the record
each interval and the attempt's result was thrown away. These run the real
drain over a real validated-work store; only the per-record operation is a
fake at its port.
"""

from __future__ import annotations

import dataclasses
from datetime import timedelta
from types import SimpleNamespace

from issue_orchestrator.control.recovery_drain import RecoveryDrain
from issue_orchestrator.domain.action_liveness import LivenessPolicy
from issue_orchestrator.domain.models import OrchestratorState
from issue_orchestrator.domain.recovery_attempt import RecoveryAttemptPending, RecoveryPendingKind
from issue_orchestrator.domain.recovery_completion import RecoveryCompleted
from issue_orchestrator.domain.recovery_drain import RecoveryDrainMode
from issue_orchestrator.domain.validated_work import ValidatedWorkState
from issue_orchestrator.domain.validated_work_commands import (
    DispositionInitiator,
    StoredEvidenceCommand,
)
from issue_orchestrator.ports.recovery_block import NullRecoveryBlockSweep
from issue_orchestrator.ports.validated_work_drain import NullValidatedWorkScopeSweep
from issue_orchestrator.ports.retained_claim_maintenance import (
    NullRetainedClaimMaintenance,
)
from tests.unit.control.liveness_doubles import (
    InMemoryActionLivenessStore,
    ManualClock,
    RecordingEscalation,
    drain_liveness,
    liveness_owner,
)
from tests.unit.validated_work_support import Rig, capture

POLICY = LivenessPolicy(max_attempts=3)


def ACTIVE() -> RecoveryDrainMode:
    return RecoveryDrainMode.ACTIVE


class _Operation:
    """The per-record operation, faked at its port."""

    def __init__(self, result) -> None:
        self.result = result
        self.called: list[str] = []

    def preflight(self, request):
        return None

    def run(self, request, state):
        self.called.append(request.record_id)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result(request) if callable(self.result) else self.result


class _Engine:
    def __init__(self, tmp_path, result, *, issues=(410,), batch_size=5) -> None:
        self.store = Rig(tmp_path / "work.sqlite").open()
        for issue in issues:
            self.store.admit(capture(issue=issue))
        self.operation = _Operation(result)
        self.clock = ManualClock()
        self.escalation = RecordingEscalation()
        self.rows = InMemoryActionLivenessStore()
        self.owner = liveness_owner(
            store=self.rows, escalation=self.escalation, clock=self.clock, policy=POLICY
        )
        self.now = SimpleNamespace(value=0.0)
        self.drain = RecoveryDrain(
            queue=self.store,
            operation=self.operation,
            authority_refresh=_Operation(RecoveryAttemptPending("refresh")),
            claim_maintenance=NullRetainedClaimMaintenance(),
            block_sweep=NullRecoveryBlockSweep(),
            scope_sweep=NullValidatedWorkScopeSweep(),
            batch_size=batch_size,
            interval_seconds=60,
            clock=lambda: self.now.value,
            liveness=drain_liveness(
                self.owner,
                record_disposition=self._disposition,
            ),
        )

    #: A durable state another path moved the record to, else its stored one.
    moved_to = None

    def _disposition(self, record_id: str):
        disposition = self.store.record_for_id(record_id).disposition
        if self.moved_to is None:
            return disposition
        return dataclasses.replace(disposition, state=self.moved_to)

    def passes(self, count: int) -> None:
        for _ in range(count):
            self.drain.tick(OrchestratorState(), ACTIVE)
            self.now.value += 60
            self.clock.advance(timedelta(hours=1))


def test_a_publish_failing_the_same_way_every_pass_parks(tmp_path) -> None:
    """Census #5: 'dirty publication checkout retained', 343 times in a day."""
    engine = _Engine(
        tmp_path, RecoveryAttemptPending("dirty publication checkout retained")
    )
    record_id = engine.store.drain_requests(after_record_id="", limit=1)[0].record_id
    before = engine.store.record_for_id(record_id)

    engine.passes(30)

    assert len(engine.operation.called) == POLICY.max_attempts
    [parked] = engine.escalation.parked
    assert parked.key.identity.subject == f"validated_work:{record_id}"
    assert parked.key.identity.action == "recover_validated_work"
    assert parked.key.escalation_issue == 410
    assert "dirty publication checkout retained" in parked.last_reason
    # Parking stops the drain; it never touches the work. The record, its
    # evidence and its state are exactly as they were.
    assert engine.store.record_for_id(record_id) == before


def test_an_operation_that_raises_every_pass_parks(tmp_path) -> None:
    """Census #6: a completion rejected missing_authority on every pass."""
    engine = _Engine(tmp_path, RuntimeError("Tech Lead completion rejected: missing_authority"))

    engine.passes(30)

    assert len(engine.operation.called) == POLICY.max_attempts
    assert "missing_authority" in engine.escalation.parked[0].last_reason


def test_contention_spends_nothing_but_is_shown(tmp_path) -> None:
    """A record another owner holds spends nothing, and it is a visible wait,
    so a holder that never lets go cannot hide it (review B r2)."""
    engine = _Engine(
        tmp_path,
        RecoveryAttemptPending("Record recovery is already executing", kind=RecoveryPendingKind.CONTENDED),
    )

    engine.passes(20)

    assert len(engine.operation.called) == 20
    assert engine.escalation.parked == []
    [row] = engine.rows.waiting_rows()
    assert row.attempts == 0 and "already executing" in row.last_reason


def test_a_parked_record_does_not_stall_the_round_robin(tmp_path) -> None:
    """A held record still yields a report item, so the cursor moves past it
    and every other record keeps its turn."""
    parked_issue, healthy_issue = 1, 2

    def result(request):
        issue = engine.store.record_for_id(request.record_id).disposition.key.issue_number
        if issue == parked_issue:
            return RecoveryAttemptPending("still broken")
        return RecoveryAttemptPending("waiting on remote", kind=RecoveryPendingKind.CONTENDED)

    engine = _Engine(tmp_path, result, issues=(parked_issue, healthy_issue), batch_size=1)
    engine.passes(40)

    ids = {
        engine.store.record_for_id(r).disposition.key.issue_number: r
        for r in set(engine.operation.called)
    }
    assert engine.operation.called.count(ids[parked_issue]) == POLICY.max_attempts
    assert engine.operation.called.count(ids[healthy_issue]) >= 15


def test_explicit_recovery_runs_despite_a_park_and_its_success_releases_it(tmp_path) -> None:
    engine = _Engine(tmp_path, RecoveryAttemptPending("still broken"))
    engine.passes(10)
    assert len(engine.operation.called) == POLICY.max_attempts
    request = engine.store.drain_requests(after_record_id="", limit=1)[0]
    evidence = engine.store.record_for_id(request.record_id).current_evidence
    command = StoredEvidenceCommand(
        issue_number=410,
        reason="operator retry",
        initiator=DispositionInitiator.OPERATOR,
        evidence_id=evidence.evidence_id,
        actor="operator",
        authority=evidence.authority,
    )

    completed = RecoveryCompleted.__new__(RecoveryCompleted)
    engine.operation.result = completed
    assert engine.drain.recover(command, OrchestratorState()) is completed

    engine.operation.result = RecoveryAttemptPending("broken again")
    engine.passes(1)
    assert len(engine.operation.called) == POLICY.max_attempts + 2
    [released] = engine.escalation.released
    assert released[0].key.escalation_issue == 410
    assert engine.escalation.unblocks == [(410, True)]


def _operator_retry(engine: _Engine) -> StoredEvidenceCommand:
    request = engine.store.drain_requests(after_record_id="", limit=1)[0]
    evidence = engine.store.record_for_id(request.record_id).current_evidence
    return StoredEvidenceCommand(
        issue_number=410,
        reason="operator retry",
        initiator=DispositionInitiator.OPERATOR,
        evidence_id=evidence.evidence_id,
        actor="operator",
        authority=evidence.authority,
    )


def test_a_state_change_is_a_new_question(tmp_path) -> None:
    """Parked while queued; the record then moves to publishing with the same
    evidence. That is new facts, so the drain tries it again (review B r1)."""
    engine = _Engine(tmp_path, RecoveryAttemptPending("still broken"))
    engine.passes(10)
    assert len(engine.operation.called) == POLICY.max_attempts

    engine.moved_to = ValidatedWorkState.PUBLISHING
    engine.passes(1)

    assert len(engine.operation.called) == POLICY.max_attempts + 1


def test_a_completed_recovery_releases_its_records_older_parks(tmp_path) -> None:
    """A park asked under the record's older state is answered by its recovery:
    released, and its block withdrawn (review B r1)."""
    engine = _Engine(tmp_path, RecoveryAttemptPending("still broken"))
    engine.passes(10)
    [queued_park] = engine.escalation.parked
    # The park is still being asked about (a held pass just now), so nothing
    # retires it as superseded within stale_after: only the recovery can.
    engine.drain.tick(OrchestratorState(), ACTIVE)
    engine.moved_to = ValidatedWorkState.PUBLISHING

    completed = RecoveryCompleted.__new__(RecoveryCompleted)
    engine.operation.result = completed
    assert engine.drain.recover(_operator_retry(engine), OrchestratorState()) is completed
    engine.clock.advance(POLICY.max_backoff)
    assert POLICY.max_backoff < POLICY.stale_after
    engine.owner.reconcile_effects()

    assert [row.key for rows in engine.escalation.released for row in rows] == [queued_park.key]
    assert engine.escalation.unblocks == [(410, True)]
    assert engine.rows.rows == {}


def test_a_committed_refresh_is_done_and_releases_older_refresh_parks() -> None:
    """A refresh parked on older facts; a later refresh commits. The refresh
    question is answered: every refresh row goes, its block is withdrawn
    (review B r2)."""
    from issue_orchestrator.domain.action_liveness import ActionIdentity, ActionOutcome, LivenessKey

    clock, escalation = ManualClock(), RecordingEscalation()
    rows = InMemoryActionLivenessStore()
    owner = liveness_owner(store=rows, escalation=escalation, clock=clock, policy=POLICY)
    liveness = drain_liveness(owner)
    identity = ActionIdentity("validated_work:r1", "refresh_remote_authority")
    old = LivenessKey(identity, "a" * 32, 410)
    owner.record(old, ActionOutcome.permanent("remote unreadable"))
    new = LivenessKey(identity, "b" * 32, 410)

    liveness.settle(new, RecoveryAttemptPending(
        "remote authority observed", kind=RecoveryPendingKind.ADVANCED
    ))
    clock.advance(POLICY.max_backoff)
    owner.reconcile_effects()

    assert rows.rows == {}
    assert [row.key for batch in escalation.released for row in batch] == [old]
    assert escalation.unblocks == [(410, True)]


# --- The operations mark contention, which the drain then does not count ----


class _BusyExecution:
    """An execution owner another recovery holds, or one awaiting its stop."""

    def __init__(self, *, busy: bool) -> None:
        self.busy = busy

    def try_enter(self, record_id: str):
        from contextlib import nullcontext

        from issue_orchestrator.domain.validated_work_execution import RecordExecutionBusy

        return RecordExecutionBusy(record_id) if self.busy else nullcontext(object())

    def relinquish(self, token) -> bool:
        return False


def _record_request():
    from issue_orchestrator.domain.recovery_entry import RecoveryRecordRequest

    return RecoveryRecordRequest("r1:" + "a" * 64, "e1:" + "b" * 64)


def test_record_recovery_contention_is_marked(tmp_path) -> None:
    from issue_orchestrator.control.recovery_record_operation import RecoveryRecordOperation

    for busy in (True, False):
        operation = RecoveryRecordOperation(
            execution=_BusyExecution(busy=busy), store=None, preparation=None,
            publication=None, completion=None, scope=None,
        )
        result = operation.run(_record_request(), OrchestratorState())
        assert result.kind is RecoveryPendingKind.CONTENDED, result.message


def test_authority_refresh_contention_is_marked(tmp_path) -> None:
    from unittest.mock import MagicMock

    from issue_orchestrator.control.remote_authority_refresh import (
        RemoteAuthorityRefreshOperation,
    )

    request = MagicMock(record_id="r1:" + "a" * 64)
    for busy in (True, False):
        operation = RemoteAuthorityRefreshOperation(
            execution=_BusyExecution(busy=busy), effects=None, store=None, observer=None,
            scope=None,
        )
        assert operation.run(request).kind is RecoveryPendingKind.CONTENDED


def test_a_rate_limited_remote_read_waits_for_its_reset(tmp_path) -> None:
    """#7303's typed limit behind a drain failure spends nothing until reset."""
    from issue_orchestrator.domain.host_rate_limit import HostRateLimit
    from issue_orchestrator.ports.repository_host import RepositoryHostRateLimitedError

    def refused(request):
        error = RepositoryHostRateLimitedError("API rate limit exceeded")
        error.rate_limit = HostRateLimit(
            resets_at=engine.clock.now + timedelta(minutes=30), kind="primary"
        )
        raise error

    engine = _Engine(tmp_path, refused)

    for _ in range(40):  # 40 minutes: past the 30-minute reset, once
        engine.drain.tick(OrchestratorState(), ACTIVE)
        engine.now.value += 60
        engine.clock.advance(timedelta(minutes=1))

    assert len(engine.operation.called) == 2, "once, then once more after the reset"
    [row] = engine.rows.rows.values()
    assert row.attempts == 0 and not row.parked


def test_a_rate_limit_behind_a_returned_refusal_waits_for_its_reset(tmp_path) -> None:
    """An operation that CAUGHT a typed limit (an unreadable remote or issue)
    returns it on the refusal; the drain spends nothing until reset (#7350)."""
    from issue_orchestrator.domain.host_rate_limit import HostRateLimit
    from issue_orchestrator.domain.validated_work import ValidatedWorkFailure

    def refused(request):
        return RecoveryAttemptPending(
            "remote unreadable", ValidatedWorkFailure.REMOTE_UNREADABLE,
            rate_limit=HostRateLimit(
                resets_at=engine.clock.now + timedelta(minutes=30), kind="primary"
            ),
        )

    engine = _Engine(tmp_path, refused)
    reset = engine.clock.now + timedelta(minutes=30)

    for _ in range(20):
        engine.drain.tick(OrchestratorState(), ACTIVE)
        engine.now.value += 60
        engine.clock.advance(timedelta(minutes=1))

    assert len(engine.operation.called) == 1
    [row] = engine.rows.rows.values()
    assert (row.attempts, row.next_attempt_at) == (0, reset)


def test_a_rate_limited_issue_read_carries_the_hosts_reset() -> None:
    from datetime import datetime, timezone
    from unittest.mock import MagicMock

    from issue_orchestrator.control.claimed_recovery_preparation import (
        ClaimedRecoveryPreparation,
    )
    from issue_orchestrator.domain.host_rate_limit import HostRateLimit
    from issue_orchestrator.domain.recovery_entry import RecoveryRecordRequest
    from issue_orchestrator.ports.recovery_issue_reader import RecoveryIssueReadError
    from issue_orchestrator.ports.repository_host import RepositoryHostRateLimitedError

    limited = RepositoryHostRateLimitedError("API rate limit exceeded")
    limited.rate_limit = HostRateLimit(
        resets_at=datetime(2026, 9, 27, 13, tzinfo=timezone.utc), kind="primary"
    )

    def read(repo, number):
        raise RecoveryIssueReadError("cannot read recovery issue") from limited

    record = MagicMock()
    record.disposition.key.repo_slug = "o/r"
    record.disposition.key.issue_number = 5
    preparation = ClaimedRecoveryPreparation(
        effects=MagicMock(perform=lambda token, claim, fn: fn()),
        store=MagicMock(record_for_id=lambda _rid: record),
        issues=MagicMock(read=read),
        runtime=MagicMock(), gate=MagicMock(), workspaces=MagicMock(),
        preparation=MagicMock(), repo_slug="o/r", pause_label="io:needs-reconcile",
    )
    request = MagicMock(spec=RecoveryRecordRequest)
    request.refusal.return_value = None

    result = preparation.prepare(object(), MagicMock(record_id="r1"), request)

    assert isinstance(result, RecoveryAttemptPending)
    assert result.rate_limit == limited.rate_limit


# --- Coordinator enumeration (#7346 fix): every pending kind is counted ------


def test_every_pending_kind_has_an_outcome() -> None:
    from issue_orchestrator.control.recovery_drain_liveness import drain_outcome

    for kind in RecoveryPendingKind:
        drain_outcome(RecoveryAttemptPending("x", kind=kind))  # no KeyError


def test_a_runtime_wait_is_paced_and_visible_but_never_parks(tmp_path) -> None:
    engine = _Engine(
        tmp_path,
        RecoveryAttemptPending("Other issue runtime is active", kind=RecoveryPendingKind.WAITING),
    )
    engine.passes(40)  # one hour per pass: well past any failure budget

    assert len(engine.operation.called) == 40, "paced at max_backoff, which is under an hour"
    assert engine.escalation.parked == []
    [row] = engine.rows.waiting_rows()
    assert row.attempts == 0 and "runtime is active" in row.last_reason


def test_a_paused_issue_parks_as_needs_human_at_once(tmp_path) -> None:
    engine = _Engine(
        tmp_path,
        RecoveryAttemptPending("Issue is paused", kind=RecoveryPendingKind.NEEDS_HUMAN),
    )
    engine.passes(10)

    assert len(engine.operation.called) == 1
    assert engine.escalation.parked[0].last_outcome.value == "needs_human"


def test_the_preparation_names_how_each_refusal_counts() -> None:
    """Closed: a bounded failure (census #7). Paused: needs a person. Runtime
    active: a wait; a runtime probe that cannot answer: a bounded failure.
    Disposition gate busy: contention."""
    from contextlib import contextmanager
    from unittest.mock import MagicMock

    from issue_orchestrator.control.claimed_recovery_preparation import (
        ClaimedRecoveryPreparation,
    )
    from issue_orchestrator.control.review_exchange_lifecycle import (
        IssueRuntimeActivity,
        IssueRuntimeOwnerKind,
    )
    from issue_orchestrator.domain.issue_disposition_gate import IssueDispositionGateStatus
    from issue_orchestrator.domain.recovery_entry import (
        RecoveryIssue,
        RecoveryIssueState,
        RecoveryRecordRequest,
    )

    def prepare(*, state=RecoveryIssueState.OPEN, labels=(), active=(), unverifiable=(),
                gate_busy=False):
        record = MagicMock()
        record.disposition.key.repo_slug = "o/r"
        record.disposition.key.issue_number = 5
        store = MagicMock(record_for_id=lambda _rid: record)
        issues = MagicMock(read=lambda repo, number: RecoveryIssue("o/r", 5, "t", state, labels))

        @contextmanager
        def try_acquire(repo, number):
            yield IssueDispositionGateStatus.BUSY if gate_busy else MagicMock()

        preparation = ClaimedRecoveryPreparation(
            effects=MagicMock(perform=lambda token, claim, fn: fn()),
            store=store,
            issues=issues,
            runtime=MagicMock(probe=lambda number: IssueRuntimeActivity(
                frozenset(active), frozenset(unverifiable)
            )),
            gate=MagicMock(try_acquire=try_acquire),
            workspaces=MagicMock(),
            preparation=MagicMock(),
            repo_slug="o/r",
            pause_label="io:needs-reconcile",
        )
        request = MagicMock(spec=RecoveryRecordRequest)
        request.refusal.return_value = None
        return preparation.prepare(object(), MagicMock(record_id="r1"), request)

    assert prepare(state=RecoveryIssueState.CLOSED).kind is RecoveryPendingKind.FAILED
    assert prepare(labels=("io:needs-reconcile",)).kind is RecoveryPendingKind.NEEDS_HUMAN
    assert prepare(active=(IssueRuntimeOwnerKind.SESSIONS,)).kind is RecoveryPendingKind.WAITING
    assert prepare(
        active=(IssueRuntimeOwnerKind.SESSIONS,), unverifiable=(IssueRuntimeOwnerKind.EXCHANGE_PAIR,)
    ).kind is RecoveryPendingKind.WAITING
    assert prepare(unverifiable=(IssueRuntimeOwnerKind.EXCHANGE_PAIR,)).kind is RecoveryPendingKind.FAILED
    assert prepare(gate_busy=True).kind is RecoveryPendingKind.CONTENDED


def test_a_refused_pr_create_parks_at_once(tmp_path) -> None:
    """#7357's typed refusals are deterministic: no budget is spent on them."""
    from issue_orchestrator.domain.validated_work import ValidatedWorkFailure

    engine = _Engine(
        tmp_path,
        RecoveryAttemptPending("PR create refused", ValidatedWorkFailure.PR_CREATE_NO_COMMITS),
    )
    engine.passes(10)

    assert len(engine.operation.called) == 1
    assert engine.escalation.parked[0].last_outcome.value == "permanent"
