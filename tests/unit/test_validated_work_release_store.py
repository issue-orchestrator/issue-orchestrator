"""All-or-nothing abandonment of several records of one issue (#9092).

A release names every record a rewritten PR stranded; porchpin #262's are
divergent heads of one lineage. The store must resolve them in ONE
transaction, refuse the whole batch when any record moved, and never let the
lineage classifier promote a sibling between two of them.
"""

from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock
import sqlite3

import pytest

from issue_orchestrator.domain.validated_work import (
    LineageRole,
    ValidatedWorkFailure as Failure,
    ValidatedWorkState as State,
)
from issue_orchestrator.domain.validated_work_commands import (
    AbandonAllOutcome,
    AbandonStatus,
    AbandonValidatedWorkCommand,
    AbandonValidatedWorkOutcome,
)
from issue_orchestrator.domain.validated_work_execution import RecordExecutionBusy
from issue_orchestrator.domain.issue_disposition_gate import IssueDispositionGateStatus
from issue_orchestrator.events import EventName
from tests.unit.aggregate_recovery_support import AggregateRig
from tests.unit.validated_work_support import (
    DIVERGENT,
    L,
    LATER,
    Rig,
    capture,
    changed_observations,
)


def _fixed_clock() -> datetime:
    return datetime.fromisoformat(LATER)


def _divergent_pair(store):
    """Two validated heads of one branch, neither containing the other."""
    first, second = capture(L), capture(DIVERGENT, run="run-2")
    store.admit(first)
    store.admit(second)
    for admission in (first, second):
        row = store.get(admission.evidence.record_id)
        assert (row.state, row.lineage_role, row.failure) == (
            State.PARKED, LineageRole.DIVERGENT, Failure.DIVERGENT_VALIDATED_HEADS)
    return first, second


def _command(store, admission) -> AbandonValidatedWorkCommand:
    lookup = store.evidence_for_id(admission.evidence.evidence_id)
    assert lookup is not None
    return AbandonValidatedWorkCommand(
        lookup.evidence.authority,
        "approved by maintainer @operator on tech-lead proposal #485",
        "Released on operator approval: rebuilt in merged PR #479",
    )


def _contents(path: Path) -> tuple[str, ...]:
    with closing(sqlite3.connect(path)) as conn:
        return tuple(conn.iterdump())


def test_every_divergent_sibling_is_abandoned_in_one_transaction(tmp_path):
    """Classifying between the two would make the second the lineage's sole
    head and refuse it; the batch classifies once, after both resolved."""
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open(clock=_fixed_clock)
    first, second = _divergent_pair(store)
    commands = (_command(store, first), _command(store, second))

    outcome = store.abandon_all_if_current(commands)

    assert outcome.committed
    assert [item.disposition.record_id for item in outcome.abandoned] == [
        first.evidence.record_id, second.evidence.record_id]
    for command in commands:
        row = store.get(command.authority.record_id)
        assert row.state is State.ABANDONED
        assert row.resolution is not None
        assert (row.resolution.actor, row.resolution.reason, row.resolution.resolved_at) == (
            command.actor, command.reason, LATER)
    assert not store.has_unresolved_work(6914)


def test_replaying_a_committed_batch_is_recognized_with_no_write(tmp_path):
    """Review r1 F1: a retry of the very same release finds its own commit."""
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open(clock=_fixed_clock)
    first, second = _divergent_pair(store)
    commands = (_command(store, first), _command(store, second))
    assert store.abandon_all_if_current(commands).committed
    before = _contents(rig.path)

    replay = store.abandon_all_if_current(commands)

    assert replay.committed and replay.replayed
    assert [item.disposition.state for item in replay.abandoned] == [State.ABANDONED] * 2
    assert _contents(rig.path) == before
    # A different release of the same records is not that operation.
    other = tuple(replace(command, reason="another release") for command in commands)
    refused = store.abandon_all_if_current(other)
    assert refused.refusal is not None
    assert refused.refusal.status is AbandonStatus.ALREADY_RESOLVED
    assert _contents(rig.path) == before


def test_one_moved_record_refuses_the_whole_batch_with_no_write(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open(clock=_fixed_clock)
    first, second = _divergent_pair(store)
    commands = (_command(store, first), _command(store, second))
    store.admit(changed_observations(second, pr_number=92))
    before = _contents(rig.path)

    outcome = store.abandon_all_if_current(commands)

    assert not outcome.committed and outcome.abandoned == ()
    assert outcome.refused_record_id == second.evidence.record_id
    assert outcome.refusal is not None
    assert outcome.refusal.status is AbandonStatus.AUTHORITY_STALE
    assert outcome.refusal.current_authority is not None
    assert outcome.refusal.current_authority.pr_number == 92
    # The first command passed its own checks; the rollback undid it too.
    assert _contents(rig.path) == before
    assert store.get(first.evidence.record_id).state is State.PARKED


def test_releasing_siblings_one_at_a_time_strands_the_second(tmp_path):
    """Why a release is one batch: abandoning one divergent sibling alone
    makes the other its lineage's sole head, which is no longer parked, so a
    release of the pair after that refuses as a whole, with no write."""
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open(clock=_fixed_clock)
    first, second = _divergent_pair(store)
    commands = (_command(store, first), _command(store, second))
    assert store.abandon_if_current(commands[1]).status is AbandonStatus.ABANDONED
    assert store.get(first.evidence.record_id).state is not State.PARKED
    before = _contents(rig.path)

    outcome = store.abandon_all_if_current(commands)

    assert outcome.refusal is not None
    assert outcome.refusal.status is AbandonStatus.REFUSED_STATE
    assert outcome.refused_record_id == first.evidence.record_id
    assert _contents(rig.path) == before


@pytest.mark.parametrize("commands", [(), "duplicate"])
def test_a_batch_names_each_record_once(tmp_path, commands):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open(clock=_fixed_clock)
    first, _ = _divergent_pair(store)
    if commands == "duplicate":
        commands = (_command(store, first), _command(store, first))
    with pytest.raises(ValueError):
        store.abandon_all_if_current(commands)


def test_batch_outcome_is_all_or_nothing_by_construction():
    refusal = AbandonValidatedWorkOutcome(AbandonStatus.BUSY, None, (), None, "busy")
    with pytest.raises(ValueError, match="abandons at least one"):
        AbandonAllOutcome(())
    with pytest.raises(ValueError, match="abandons at least one"):
        AbandonAllOutcome((), refusal=None, refused_record_id="r1")
    with pytest.raises(ValueError, match="refused_record_id"):
        AbandonAllOutcome((), refusal, "")
    assert not AbandonAllOutcome((), refusal, "r1").committed


def _plant_two_failed_records(rig: AggregateRig) -> tuple[AbandonValidatedWorkCommand, ...]:
    """The rig's own failed record plus a second, parked one on the same issue."""
    from tests.unit.test_aggregate_recovery_block import _make_abandonable

    first = _make_abandonable(rig)
    second = capture(DIVERGENT, run="run-2", branch="feature-r1", state=State.PARKED)
    rig.base.store.admit(second)
    lookup = rig.base.store.evidence_for_id(second.evidence.evidence_id)
    assert lookup is not None
    actor, reason = "approved by maintainer @operator", "rebuilt in merged PR #479"
    return (
        AbandonValidatedWorkCommand(first.authority, actor, reason),
        AbandonValidatedWorkCommand(lookup.evidence.authority, actor, reason),
    )


def test_owner_releases_every_record_audits_each_and_drops_recovery_pending(tmp_path):
    rig = AggregateRig(tmp_path)
    commands = _plant_two_failed_records(rig)
    rig.aggregate.reconcile_issue_block(6914)
    assert "recovery-pending" in rig.remote.labels

    outcome = rig.abandonment.abandon_all(commands)

    assert outcome.committed
    assert "recovery-pending" not in rig.remote.labels
    audited = [
        event.data["record_id"]
        for event in rig.events.events
        if event.name == EventName.VALIDATED_WORK_ABANDONED.value
    ]
    assert audited == [command.authority.record_id for command in commands]
    assert not rig.base.store.has_unresolved_work(6914)


def test_owner_refuses_busy_when_any_record_is_executing(tmp_path):
    rig = AggregateRig(tmp_path)
    commands = _plant_two_failed_records(rig)
    lease = rig.base.execution.try_enter(commands[1].authority.record_id)
    assert not isinstance(lease, RecordExecutionBusy)

    with lease:
        outcome = rig.abandonment.abandon_all(commands)

    assert outcome.refusal is not None
    assert outcome.refusal.status is AbandonStatus.BUSY
    assert outcome.refused_record_id == commands[1].authority.record_id
    for command in commands:
        assert rig.base.store.get(command.authority.record_id).state is not State.ABANDONED
    assert rig.events.events == []


def test_owner_refuses_busy_while_the_issue_is_changing(tmp_path):
    rig = AggregateRig(tmp_path)
    commands = _plant_two_failed_records(rig)

    with rig.gate.try_acquire("owner/repo", 6914) as status:
        assert status is IssueDispositionGateStatus.ACQUIRED
        outcome = rig.abandonment.abandon_all(commands)

    assert outcome.refusal is not None and outcome.refusal.status is AbandonStatus.BUSY
    assert rig.events.events == []


def test_owner_refuses_a_batch_spanning_issues(tmp_path):
    rig = AggregateRig(tmp_path)
    first, _ = _plant_two_failed_records(rig)
    other = capture(DIVERGENT, run="run-3", issue=6915, state=State.PARKED)
    rig.base.store.admit(other)
    lookup = rig.base.store.evidence_for_id(other.evidence.evidence_id)
    assert lookup is not None
    with pytest.raises(ValueError, match="one issue"):
        rig.abandonment.abandon_all(
            (first, AbandonValidatedWorkCommand(lookup.evidence.authority, "op", "why")))


def test_a_replay_reprojects_the_block_again_without_auditing_twice(tmp_path):
    """Review r1 F1: the commit landed but reprojection failed; the retry
    reprojects and drops recovery-pending, and audits no record twice."""
    rig = AggregateRig(tmp_path)
    commands = _plant_two_failed_records(rig)
    rig.aggregate.reconcile_issue_block(6914)
    real = rig.aggregate.reconcile_issue_block
    rig.aggregate.reconcile_issue_block = Mock(side_effect=OSError("label write lost"))

    with pytest.raises(OSError):
        rig.abandonment.abandon_all(commands)
    assert "recovery-pending" in rig.remote.labels
    audited = len(rig.events.events)

    rig.aggregate.reconcile_issue_block = real
    outcome = rig.abandonment.abandon_all(commands)

    assert outcome.committed and outcome.replayed
    assert "recovery-pending" not in rig.remote.labels
    assert len(rig.events.events) == audited
