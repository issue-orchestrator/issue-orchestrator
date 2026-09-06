"""Connection/process races use barriers, never elapsed-time ownership rules."""

from dataclasses import replace
import multiprocessing
import os
from pathlib import Path
import sqlite3
from contextlib import closing
import threading

import pytest

from issue_orchestrator.domain.validated_work import (
    LineageRole,
    ValidatedWorkState as State,
)
from issue_orchestrator.domain.validated_work_store import AdmissionStatus
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.sqlite_registry import list_sqlite_databases
from tests.unit.threading_helpers import join_or_fail, run_in_thread
from tests.unit.validated_work_support import (
    OTHER,
    OWNER,
    L,
    V,
    Rig,
    Liveness,
    begin,
    capture,
    claim,
)


def _race(*functions):
    barrier = threading.Barrier(len(functions))

    def invoke(fn):
        barrier.wait(timeout=10)
        return fn()

    workers = [run_in_thread(invoke, fn) for fn in functions]
    for thread, _ in workers:
        join_or_fail(thread, 15)
    return [result.unwrap() for _, result in workers]


def test_concurrent_identical_capture_converges_on_one_record_and_evidence(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    first, second = rig.open(), rig.open()
    results = _race(lambda: first.admit(capture()), lambda: second.admit(capture()))
    assert {r.status for r in results} == {
        AdmissionStatus.ADMITTED,
        AdmissionStatus.CONVERGED,
    }
    assert len(first.for_issue(6914).dispositions) == 1
    assert (
        first.evidence_for_id(
            capture().evidence.evidence_id
        ).evidence.observation_revision
        == 1
    )


def test_concurrent_distinct_heads_leave_one_drainable_descendant(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    first, second = rig.open(), rig.open()
    _race(lambda: first.admit(capture(V)), lambda: second.admit(capture(L)))
    rows = first.for_issue(6914).dispositions
    assert len(rows) == 2
    assert sum(d.state is State.QUEUED for d in rows) == 1
    assert first.get(capture(V).evidence.record_id).lineage_role is LineageRole.ANCESTOR
    assert first.get(capture(L).evidence.record_id).state is State.QUEUED


def test_concurrent_claims_and_same_owner_attempts_have_one_winner(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    first, second = rig.open(), rig.open(Liveness(OTHER))
    a = capture()
    first.admit(a)

    def acquire(store):
        return store.acquire_claim(
            a.evidence.record_id,
            expected_states=frozenset({State.QUEUED}),
            evidence_id=a.evidence.evidence_id,
        )

    results = _race(lambda: acquire(first), lambda: acquire(second))
    assert sum(c is not None for c in results) == 1
    winner, token = next(
        (s, c) for s, c in zip((first, second), results) if c is not None
    )
    attempts = _race(lambda: begin(winner, token), lambda: begin(winner, token))
    assert sum(a is not None for a in attempts) == 1
    assert len(winner.publish_attempts(token.record_id)) == 1


def test_capture_racing_begin_never_rewrites_inflight_evidence(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    first, second = rig.open(), rig.open()
    old, new = capture(), capture(run="replacement")
    first.admit(old)
    token = claim(first, old)
    result, _ = _race(lambda: second.admit(new), lambda: begin(first, token))
    row = first.get(token.record_id)
    if result.status is AdmissionStatus.ATTACHED:
        assert row.evidence_id == old.evidence.evidence_id
        assert (
            first.attached_evidence(row.record_id)[0].evidence_id
            == new.evidence.evidence_id
        )
    else:
        assert result.status is AdmissionStatus.SUPERSEDES
        assert row.evidence_id == new.evidence.evidence_id
    for attempt in first.publish_attempts(row.record_id):
        assert attempt.evidence_id == row.evidence_id


def _process_capture_and_claim(args):
    path, head = args
    identity = replace(OWNER, pid=os.getpid(), instance_id=f"test-{os.getpid()}")
    store = Rig(Path(path), liveness=Liveness(identity)).open()
    a = capture(head)
    store.admit(a)
    token = store.acquire_claim(
        a.evidence.record_id,
        expected_states=frozenset({State.QUEUED}),
        evidence_id=a.evidence.evidence_id,
    )
    return token is not None  # never export the claim secret across IPC


def test_spawned_processes_share_unique_admission_and_cannot_copy_owner(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    store = rig.open()
    context = multiprocessing.get_context("spawn")
    pool = context.Pool(2)
    try:
        results = pool.map_async(
            _process_capture_and_claim, [(str(rig.path), V)] * 2
        ).get(timeout=30)
    finally:
        pool.terminate()
        pool.join()
    assert sum(results) == 1
    assert len(store.for_issue(6914).dispositions) == 1
    assert store.owner_of(capture().evidence.record_id).pid != os.getpid()


def test_four_table_indexes_and_registry_are_durable(tmp_path):
    rig = Rig(tmp_path / "work.sqlite")
    rig.open().admit(capture())
    with closing(sqlite3.connect(rig.path)) as conn, conn:
        tables = {
            r[0]
            for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert tables == {
            "validated_work_records",
            "validated_work_evidence",
            "validated_work_publish_attempts",
            "validated_work_lineage",
        }
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO validated_work_records SELECT * FROM validated_work_records"
            )
    config = Config(repo_root=tmp_path)
    entry = next(
        db for db in list_sqlite_databases(config) if db.key == "validated_work"
    )
    assert entry.backup and entry.enforce_pragmas and entry.enabled_fn(config)
    assert entry.path_fn(config).name == "validated_work.sqlite"
