"""Atomic authority and state rules for operator abandonment."""

from __future__ import annotations

from contextlib import closing
from dataclasses import replace
from datetime import datetime
from pathlib import Path
import sqlite3

import pytest

from issue_orchestrator.domain.validated_work import (
    ResolutionKind,
    ValidatedWorkFailure as Failure,
    ValidatedWorkState as State,
    canonical_json,
)
from issue_orchestrator.domain.validated_work_commands import (
    AbandonStatus,
    AbandonValidatedWorkCommand,
    ValidatedWorkAuthoritySnapshot,
)
from tests.unit.validated_work_support import (
    AT,
    L,
    LATER,
    Rig,
    begin,
    capture,
    changed_observations,
    claim,
)


def _fixed_clock() -> datetime:
    return datetime.fromisoformat(LATER)


def _command(store, admission, *, authority=None):
    lookup = store.evidence_for_id(admission.evidence.evidence_id)
    assert lookup is not None
    return AbandonValidatedWorkCommand(
        lookup.evidence.authority if authority is None else authority,
        "operator@example.test",
        "accepted loss after inspection",
    )


def _authority(admission) -> ValidatedWorkAuthoritySnapshot:
    evidence = admission.evidence
    key = evidence.identity.key
    observations = evidence.observations
    return ValidatedWorkAuthoritySnapshot(
        evidence.record_id,
        evidence.evidence_id,
        0,
        key.validated_head_sha,
        key.branch_name,
        key.repo_slug,
        key.issue_number,
        observations.pr_number,
        observations.expected_remote_head_sha,
        observations.remote_baseline_status,
    )


def _database_contents(path: Path) -> tuple[str, ...]:
    with closing(sqlite3.connect(path)) as conn:
        return tuple(conn.iterdump())


@pytest.mark.parametrize(
    "state,failure",
    [
        (State.PARKED, None),
        (State.FAILED, Failure.ARTIFACT_MISSING),
    ],
)
def test_exact_current_unowned_record_is_abandoned_with_audit(
    tmp_path, state, failure
):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open(clock=_fixed_clock)
    admission = capture(
        state=state,
        failure=failure,
        reason="requires an operator decision",
    )
    store.admit(admission)
    command = _command(store, admission)

    outcome = store.abandon_if_current(command)

    assert outcome.status is AbandonStatus.ABANDONED
    assert outcome.disposition is not None
    assert outcome.disposition.state is State.ABANDONED
    assert outcome.disposition.failure is None
    assert outcome.disposition.reason == command.reason
    assert outcome.disposition.resolution is not None
    assert outcome.disposition.resolution.actor == command.actor
    assert outcome.disposition.resolution.reason == command.reason
    assert outcome.disposition.resolution.resolved_at == LATER
    assert not store.has_unresolved_work(admission.evidence.identity.key.issue_number)
    with closing(sqlite3.connect(rig.path)) as conn:
        row = conn.execute(
            "SELECT resolution_kind,abandon_authority_json,terminal_at "
            "FROM validated_work_records WHERE record_id=?",
            (admission.evidence.record_id,),
        ).fetchone()
    assert row == (
        ResolutionKind.OPERATOR_ABANDONED.value,
        canonical_json(command.authority.to_dict()),
        LATER,
    )

    before = _database_contents(rig.path)
    repeated = store.abandon_if_current(command)
    assert repeated.status is AbandonStatus.ALREADY_RESOLVED
    assert _database_contents(rig.path) == before


@pytest.mark.parametrize(
    "change",
    [
        {"pr_number": 92},
        {"expected_remote_head_sha": "f" * 40},
    ],
)
def test_changed_observation_returns_transactional_current_authority_without_writes(
    tmp_path, change
):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open(clock=_fixed_clock)
    admission = capture(state=State.PARKED)
    store.admit(admission)
    command = _command(store, admission)
    store.admit(changed_observations(admission, **change))
    before = _database_contents(rig.path)

    outcome = store.abandon_if_current(command)

    assert outcome.status is AbandonStatus.AUTHORITY_STALE
    assert outcome.current_authority is not None
    assert outcome.current_authority.observation_revision == 1
    for field, value in change.items():
        assert getattr(outcome.current_authority, field) == value
    assert _database_contents(rig.path) == before


def test_superseded_and_unknown_evidence_are_distinct_stale_refusals(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open(clock=_fixed_clock)
    original = capture(state=State.PARKED)
    store.admit(original)
    original_command = _command(store, original)
    replacement = capture(
        run="replacement", state=State.PARKED, reason="new decision"
    )
    store.admit(replacement)
    before = _database_contents(rig.path)

    superseded = store.abandon_if_current(original_command)
    unknown_authority = replace(
        original_command.authority, evidence_id="unknown-evidence"
    )
    unknown = store.abandon_if_current(
        replace(original_command, authority=unknown_authority)
    )

    assert superseded.status is AbandonStatus.EVIDENCE_NOT_CURRENT
    assert unknown.status is AbandonStatus.AUTHORITY_STALE
    for outcome in (superseded, unknown):
        assert outcome.current_authority is not None
        assert outcome.current_authority.evidence_id == replacement.evidence.evidence_id
    assert _database_contents(rig.path) == before


def test_missing_record_is_a_typed_zero_write_refusal(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open(clock=_fixed_clock)
    missing = capture(issue=7000, state=State.PARKED)
    before = _database_contents(rig.path)

    outcome = store.abandon_if_current(
        AbandonValidatedWorkCommand(_authority(missing), "operator", "inspected")
    )

    assert outcome.status is AbandonStatus.NO_SUCH_RECORD
    assert _database_contents(rig.path) == before


def test_queued_publishing_and_owned_resting_records_refuse_without_writes(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open(clock=_fixed_clock)
    queued = capture(branch="queued")
    publishing = capture(branch="publishing")
    owned = capture(branch="owned", state=State.PARKED)
    for admission in (queued, publishing, owned):
        store.admit(admission)
    publishing_claim = claim(store, publishing)
    assert begin(store, publishing_claim) is not None
    owned_claim = claim(store, owned)
    before = _database_contents(rig.path)

    outcomes = tuple(
        store.abandon_if_current(_command(store, admission))
        for admission in (queued, publishing, owned)
    )

    assert {outcome.status for outcome in outcomes} == {AbandonStatus.REFUSED_STATE}
    assert _database_contents(rig.path) == before
    assert store.relinquish_claim(publishing_claim)
    assert store.relinquish_claim(owned_claim)


def test_attached_evidence_must_be_promoted_and_newly_confirmed(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open(clock=_fixed_clock)
    original = capture()
    store.admit(original)
    original_command = _command(store, original)
    active = claim(store, original)
    assert begin(store, active) is not None
    attached = capture(
        run="newer",
        state=State.PARKED,
        reason="new evidence needs a decision",
        at=LATER,
    )
    store.admit(attached)
    assert store.fail(
        active,
        failure=Failure.PUSH_FAILED,
        reason="publication failed",
        failed_at=LATER,
    )
    assert store.relinquish_claim(active)
    before = _database_contents(rig.path)

    pending = store.abandon_if_current(original_command)

    assert pending.status is AbandonStatus.ATTACHED_EVIDENCE_PENDING
    assert pending.pending_evidence_ids == (attached.evidence.evidence_id,)
    assert _database_contents(rig.path) == before

    promotion_claim = claim(store, original)
    promoted = store.resolve_attached_evidence(
        promotion_claim, record_id=original.evidence.record_id, resolved_at=LATER
    )
    assert promoted is not None
    assert store.relinquish_claim(promotion_claim)
    stale = store.abandon_if_current(original_command)
    assert stale.status is AbandonStatus.EVIDENCE_NOT_CURRENT
    current_command = _command(store, attached)
    assert store.abandon_if_current(current_command).status is AbandonStatus.ABANDONED


def test_abandoning_failed_predecessor_reclassifies_its_waiter(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open(clock=_fixed_clock)
    predecessor = capture()
    store.admit(predecessor)
    active = claim(store, predecessor)
    assert begin(store, active) is not None
    successor = capture(head=L, run="successor", at=LATER)
    store.admit(successor)
    assert store.get(successor.evidence.record_id).state is State.PARKED
    assert store.fail(
        active,
        failure=Failure.PUSH_FAILED,
        reason="publication failed",
        failed_at=LATER,
    )
    assert store.relinquish_claim(active)

    outcome = store.abandon_if_current(_command(store, predecessor))

    assert outcome.status is AbandonStatus.ABANDONED
    successor_disposition = store.get(successor.evidence.record_id)
    assert successor_disposition.state is State.QUEUED
    assert successor_disposition.failure is None


def test_invalid_clock_rolls_back_the_resolution(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open(clock=lambda: datetime(2026, 9, 6, 13))
    admission = capture(state=State.PARKED)
    store.admit(admission)
    before = _database_contents(rig.path)

    with pytest.raises(ValueError, match="aware datetime"):
        store.abandon_if_current(_command(store, admission))

    assert _database_contents(rig.path) == before


def test_reopened_record_retains_then_replaces_accepted_authority_audit(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open(clock=_fixed_clock)
    original = capture(state=State.PARKED)
    store.admit(original)
    first = _command(store, original)
    assert store.abandon_if_current(first).status is AbandonStatus.ABANDONED
    replacement = capture(
        run="replacement", state=State.PARKED, reason="fresh operator decision"
    )
    store.admit(replacement)
    with closing(sqlite3.connect(rig.path)) as conn:
        retained = conn.execute(
            "SELECT abandon_authority_json FROM validated_work_records"
        ).fetchone()[0]
    assert retained == canonical_json(first.authority.to_dict())

    second = _command(store, replacement)
    assert store.abandon_if_current(second).status is AbandonStatus.ABANDONED
    with closing(sqlite3.connect(rig.path)) as conn:
        replaced = conn.execute(
            "SELECT abandon_authority_json FROM validated_work_records"
        ).fetchone()[0]
    assert replaced == canonical_json(second.authority.to_dict())
    assert replaced != retained
