"""Cold retained-work discovery through a real SQLite store."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from issue_orchestrator.domain.read_only_sqlite import (
    ReadOnlySqliteAccessError,
    ReadOnlySqliteFailure,
)
from issue_orchestrator.domain.validated_work import (
    FinalizationPhase,
    ValidatedWorkFailure,
    ValidatedWorkState,
)
from issue_orchestrator.domain.validated_work_commands import AbandonStatus
from issue_orchestrator.domain.repository_engine_lifecycle import (
    EngineIdentity,
    EngineStopAvailability,
)
from issue_orchestrator.domain.validated_work_discovery import (
    WorkDiscoveryStatus,
)
from issue_orchestrator.infra.validated_work_record_reader import (
    SqliteValidatedWorkRecordReader,
)
from issue_orchestrator.infra import sqlite_readonly

from .validated_work_support import LATER, ROOT, V, Rig, begin, capture, claim


def _path(repo: Path) -> Path:
    return repo / ".issue-orchestrator" / "state" / "validated_work.sqlite"


def _reader(
    availability: EngineStopAvailability = EngineStopAvailability.AVAILABLE,
) -> SqliteValidatedWorkRecordReader:
    return SqliteValidatedWorkRecordReader(
        timeout=1, stop_availability=lambda _engine: availability
    )


def _rig(repo: Path) -> Rig:
    return Rig(_path(repo))


def test_missing_and_unsupported_databases_are_distinct_without_creation(
    tmp_path: Path,
) -> None:
    missing_repo = tmp_path / "missing-repo"
    missing = _reader().discover_repository(str(missing_repo))
    assert missing.status is WorkDiscoveryStatus.DATABASE_ABSENT
    assert not missing_repo.exists()

    unsupported_repo = tmp_path / "unsupported"
    path = _path(unsupported_repo)
    path.parent.mkdir(parents=True)
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")

    unsupported = _reader().discover_repository(str(unsupported_repo))
    assert unsupported.status is WorkDiscoveryStatus.UNSUPPORTED_SCHEMA
    with pytest.raises(ReadOnlySqliteAccessError) as caught:
        _reader().snapshot_record(str(unsupported_repo), "record")
    assert caught.value.reason is ReadOnlySqliteFailure.UNSUPPORTED_SCHEMA


def test_supported_empty_database_is_available(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _rig(repo).open()

    result = _reader().discover_repository(str(repo))

    assert result.status is WorkDiscoveryStatus.AVAILABLE
    assert result.records == ()


def test_corrupt_database_is_unreadable(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    path = _path(repo)
    path.parent.mkdir(parents=True)
    path.write_bytes(b"this is not a SQLite database")

    result = _reader().discover_repository(str(repo))

    assert result.status is WorkDiscoveryStatus.UNREADABLE
    assert result.records == ()


@pytest.mark.parametrize(
    ("sql", "values"),
    [
        (
            "UPDATE validated_work_evidence SET observations=? WHERE record_id=?",
            ("{",),
        ),
        (
            "UPDATE validated_work_records SET validated_head_sha=? WHERE record_id=?",
            (ROOT,),
        ),
        (
            "UPDATE validated_work_records SET state='invalid',"
            "owner_claim_hash='corrupt' WHERE record_id=?",
            (),
        ),
    ],
)
def test_malformed_selected_rows_are_typed_unreadable_without_partial_results(
    tmp_path: Path, sql: str, values: tuple[str, ...]
) -> None:
    repo = tmp_path / "repo"
    store = _rig(repo).open()
    admission = capture(state=ValidatedWorkState.PARKED)
    store.admit(admission)
    record_id = admission.evidence.record_id
    with sqlite3.connect(_path(repo)) as connection:
        connection.execute(sql, (*values, record_id))

    _assert_unreadable_without_writes(repo, record_id)


def test_malformed_nested_observation_is_typed_unreadable(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    store = _rig(repo).open()
    admission = capture(state=ValidatedWorkState.PARKED)
    store.admit(admission)
    record_id = admission.evidence.record_id
    with sqlite3.connect(_path(repo)) as connection:
        row = connection.execute(
            "SELECT observations FROM validated_work_evidence WHERE record_id=?",
            (record_id,),
        ).fetchone()
        observations = json.loads(row[0])
        observations["admitted_from_paths"] = []
        connection.execute(
            "UPDATE validated_work_evidence SET observations=? WHERE record_id=?",
            (json.dumps(observations), record_id),
        )

    _assert_unreadable_without_writes(repo, record_id)


def test_invalid_durable_owner_fence_is_typed_unreadable(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    store = _rig(repo).open()
    admission = capture(state=ValidatedWorkState.PARKED)
    store.admit(admission)
    claim(store, admission)
    record_id = admission.evidence.record_id
    with sqlite3.connect(_path(repo)) as connection:
        connection.execute(
            "UPDATE validated_work_records SET owner_fence=0 WHERE record_id=?",
            (record_id,),
        )

    _assert_unreadable_without_writes(repo, record_id)


def _assert_unreadable_without_writes(repo: Path, record_id: str) -> None:
    def dump() -> tuple[str, ...]:
        with sqlite3.connect(_path(repo)) as connection:
            return tuple(connection.iterdump())

    before = dump()
    result = _reader().discover_repository(str(repo))
    assert result.status is WorkDiscoveryStatus.UNREADABLE
    assert result.records == ()
    with pytest.raises(ReadOnlySqliteAccessError) as caught:
        _reader().snapshot_record(str(repo), record_id)
    assert caught.value.reason is ReadOnlySqliteFailure.UNREADABLE
    assert dump() == before


def test_discovery_orders_unresolved_records_and_maps_current_facts(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    rig = _rig(repo)
    store = rig.open()
    later = capture(
        head=V,
        issue=20,
        branch="z-branch",
        state=ValidatedWorkState.FAILED,
        failure=ValidatedWorkFailure.PUSH_FAILED,
    )
    earlier = capture(
        head=ROOT,
        issue=10,
        branch="a-branch",
        state=ValidatedWorkState.PARKED,
    )
    store.admit(later)
    store.admit(earlier)

    result = _reader().discover_repository(str(repo))

    assert result.status is WorkDiscoveryStatus.AVAILABLE
    assert [item.disposition.key.issue_number for item in result.records] == [10, 20]
    parked, failed = result.records
    assert parked.record_id == earlier.evidence.record_id
    assert parked.worktree_head_sha == ROOT
    assert parked.observation_revision == 0
    assert (
        parked.remote_baseline_status
        is earlier.evidence.observations.remote_baseline_status
    )
    assert parked.authority.remote_baseline_status is parked.remote_baseline_status
    assert parked.escrow_retained
    assert parked.can_recover
    assert parked.can_abandon
    assert not failed.can_recover
    assert failed.can_abandon


def test_discovery_includes_resolved_records_only_while_they_retain_an_owner(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    rig = _rig(repo)
    store = rig.open()
    owned = capture(issue=1)
    unowned = capture(head=ROOT, issue=2, branch="other")
    store.admit(owned)
    store.admit(unowned)
    token = claim(store, owned)
    assert begin(store, token) is not None
    with sqlite3.connect(_path(repo)) as connection:
        connection.execute(
            "UPDATE validated_work_records SET state='recovered',published_head_sha=?,"
            "resolution_kind='published',resolved_at=?,terminal_at=?,finalization_phase='complete' "
            "WHERE record_id=?",
            (V, LATER, LATER, owned.evidence.record_id),
        )
        connection.execute(
            "UPDATE validated_work_records SET state='recovered',published_head_sha=?,"
            "resolution_kind='published',resolved_at=?,terminal_at=? WHERE record_id=?",
            (ROOT, LATER, LATER, unowned.evidence.record_id),
        )

    observed: list[EngineIdentity] = []
    reader = SqliteValidatedWorkRecordReader(
        timeout=1,
        stop_availability=lambda engine: (
            observed.append(engine) or EngineStopAvailability.REMOTE_HOST
        ),
    )
    result = reader.discover_repository(str(repo))

    assert [item.record_id for item in result.records] == [owned.evidence.record_id]
    assert result.records[0].owner is not None
    assert result.records[0].owner.owner_fence == token.fence
    assert (
        result.records[0].owner.stop_availability is EngineStopAvailability.REMOTE_HOST
    )
    assert observed[0].process == token.owner
    assert observed[0].repo_root == str(repo.resolve())
    assert result.records[0].publish_attempts == 1
    assert result.records[0].finalization_phase is FinalizationPhase.COMPLETE

    exact = reader.snapshot_record(str(repo), unowned.evidence.record_id)
    assert exact is not None
    assert exact.disposition.state is ValidatedWorkState.RECOVERED
    assert exact.owner is None
    assert reader.snapshot_record(str(repo), "absent-record") is None


def test_discovery_reads_one_wal_snapshot_while_a_writer_commits(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    rig = _rig(repo)
    store = rig.open()
    first = capture(issue=1)
    second = capture(head=ROOT, issue=2, branch="other")
    store.admit(first)
    store.admit(second)
    claim(store, first)
    original_second_update = store.record_for_id(second.evidence.record_id).updated_at
    writer = sqlite3.connect(_path(repo))
    changed = False

    def commit_during_mapping(_engine: EngineIdentity) -> EngineStopAvailability:
        nonlocal changed
        if not changed:
            writer.execute(
                "UPDATE validated_work_records SET updated_at=? WHERE record_id=?",
                (LATER, second.evidence.record_id),
            )
            writer.commit()
            changed = True
        return EngineStopAvailability.AVAILABLE

    try:
        result = SqliteValidatedWorkRecordReader(
            timeout=1, stop_availability=commit_during_mapping
        ).discover_repository(str(repo))
    finally:
        writer.close()

    assert changed
    snapshots = {item.record_id: item for item in result.records}
    assert snapshots[second.evidence.record_id].updated_at == original_second_update
    assert store.record_for_id(second.evidence.record_id).updated_at == LATER


def test_deadline_expiring_in_owner_availability_cannot_return_a_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    repo = tmp_path / "repo"
    store = _rig(repo).open()
    admission = capture(state=ValidatedWorkState.PARKED)
    store.admit(admission)
    claim(store, admission)
    now = [0.0]
    monkeypatch.setattr(sqlite_readonly.time, "monotonic", lambda: now[0])

    def expire(_engine: EngineIdentity) -> EngineStopAvailability:
        now[0] = 2.0
        return EngineStopAvailability.AVAILABLE

    reader = SqliteValidatedWorkRecordReader(timeout=1, stop_availability=expire)
    result = reader.discover_repository(str(repo))
    assert result.status is WorkDiscoveryStatus.UNREADABLE
    assert result.records == ()

    now[0] = 0.0
    with pytest.raises(ReadOnlySqliteAccessError) as caught:
        reader.snapshot_record(str(repo), admission.evidence.record_id)
    assert caught.value.reason is ReadOnlySqliteFailure.TIMEOUT


def test_lifecycle_probe_programmer_error_is_not_classified_as_durable_corruption(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    store = _rig(repo).open()
    admission = capture(state=ValidatedWorkState.PARKED)
    store.admit(admission)
    claim(store, admission)

    def broken_probe(_engine: EngineIdentity) -> EngineStopAvailability:
        raise ValueError("injected lifecycle bug")

    reader = SqliteValidatedWorkRecordReader(timeout=1, stop_availability=broken_probe)
    with pytest.raises(ValueError, match="injected lifecycle bug"):
        reader.discover_repository(str(repo))


def test_discovery_does_not_change_application_rows_or_schema(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    rig = _rig(repo)
    store = rig.open()
    admission = capture(state=ValidatedWorkState.PARKED)
    store.admit(admission)

    def dump() -> tuple[str, ...]:
        with sqlite3.connect(_path(repo)) as connection:
            return tuple(connection.iterdump())

    before = dump()
    _reader().discover_repository(str(repo))
    _reader().snapshot_record(str(repo), admission.evidence.record_id)

    assert dump() == before


def test_attached_evidence_blocks_abandonment_in_snapshot(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    rig = _rig(repo)
    store = rig.open()
    current = capture(state=ValidatedWorkState.PARKED)
    store.admit(current)
    # Admission identity includes run id, so a second capture of the same key is distinct.
    attached = capture(
        state=ValidatedWorkState.PARKED,
        run="run-2",
        issue=current.evidence.identity.key.issue_number,
        branch=current.evidence.identity.key.branch_name,
        head=current.evidence.identity.key.validated_head_sha,
    )
    with sqlite3.connect(_path(repo)) as connection:
        connection.execute(
            "UPDATE validated_work_records SET state='publishing' WHERE record_id=?",
            (current.evidence.record_id,),
        )
    store.admit(attached)
    with sqlite3.connect(_path(repo)) as connection:
        connection.execute(
            "UPDATE validated_work_records SET state='parked' WHERE record_id=?",
            (current.evidence.record_id,),
        )

    snapshot = _reader().snapshot_record(str(repo), current.evidence.record_id)

    assert snapshot is not None
    assert snapshot.attached_evidence_ids == (attached.evidence.evidence_id,)
    assert not snapshot.can_abandon
    assert snapshot.abandon_unavailable is AbandonStatus.ATTACHED_EVIDENCE_PENDING


def test_stop_reservation_suppresses_recovery_and_abandonment(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    rig = _rig(repo)
    store = rig.open()
    admission = capture(state=ValidatedWorkState.PARKED)
    store.admit(admission)
    with sqlite3.connect(_path(repo)) as connection:
        connection.execute(
            "UPDATE validated_work_records SET stop_reservation_id='reservation-1' "
            "WHERE record_id=?",
            (admission.evidence.record_id,),
        )

    snapshot = _reader().snapshot_record(str(repo), admission.evidence.record_id)

    assert snapshot is not None
    assert not snapshot.can_recover
    assert not snapshot.can_abandon
    assert snapshot.abandon_unavailable is AbandonStatus.REFUSED_STATE
