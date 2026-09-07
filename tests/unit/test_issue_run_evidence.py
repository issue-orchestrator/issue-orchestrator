"""Run ownership survives restart and refuses unknown or conflicting evidence."""

import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock

import pytest

from issue_orchestrator.control.issue_run_evidence import IssueRunEvidenceService
from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.issue_run_evidence import (
    IssueRunEvidence,
    IssueRunEvidenceOrigin,
    IssueRunEvidenceStatus,
    IssueRunEvidenceUnavailable,
    IssueRunRecord,
)
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.domain.session_run import SessionRunAssets
from issue_orchestrator.execution.issue_run_ledger import SqliteIssueRunLedger
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.entrypoints.bootstrap import build_orchestrator_for_testing
from issue_orchestrator.infra.repo_identity import state_dir
from issue_orchestrator.infra.sqlite_registry import list_sqlite_databases

NOW = "2026-09-06T12:00:00Z"


def run_record(tmp_path: Path, run_id: str = "run-1") -> IssueRunRecord:
    worktree = tmp_path / "worktree"
    run_dir = worktree / ".issue-orchestrator" / "sessions" / f"{run_id}__coding-1"
    return IssueRunRecord(
        session_key=SessionKey(FakeIssueKey("42", "example/repo"), TaskKind.CODE),
        run=SessionRunAssets.from_paths(
            session_name="coding-1", run_id=run_id, started_at=NOW,
            worktree_path=worktree, run_dir=run_dir,
            terminal_recording_path=run_dir / "terminal-recording.jsonl",
            manifest_path=run_dir / "manifest.json",
        ),
        recorded_at=NOW,
        branch_name="feature",
    )


def source(ledger, live=()) -> IssueRunEvidenceService:
    return IssueRunEvidenceService(ledger, live_runs=lambda issue: live, now=lambda: NOW)


def test_restart_retains_every_exact_run_without_worktree_discovery(tmp_path):
    path = tmp_path / "state" / "issue_run_ledger.sqlite"
    ledger = SqliteIssueRunLedger(path)
    records = (run_record(tmp_path), run_record(tmp_path, "run-2"))
    for record in records:
        ledger.record_run(42, record)
    # No worktree, manifest, or artifact file was ever created. The ledger is
    # authoritative for ownership even when every worktree has disappeared.
    assert not records[0].run.worktree_path.exists()
    evidence = source(SqliteIssueRunLedger(path)).evidence_for_issue(42)
    assert evidence.status is IssueRunEvidenceStatus.RUNS_RECORDED
    assert evidence.runs == records
    assert evidence.origin is IssueRunEvidenceOrigin.RUN_LEDGER


def test_composed_engine_uses_registered_ledger_and_survives_recomposition(
    tmp_path, sample_config, mock_repository_host,
):
    engine = build_orchestrator_for_testing(sample_config, mock_repository_host)
    record = run_record(tmp_path)
    engine.deps.issue_run_ledger.record_run(42, record)
    registered = next(
        db for db in list_sqlite_databases(sample_config) if db.key == "issue_run_ledger"
    )
    assert registered.path_fn(sample_config) == state_dir(sample_config.repo_root) / "issue_run_ledger.sqlite"
    assert registered.backup and registered.enforce_pragmas
    assert registered.enabled_fn(sample_config)
    restarted = build_orchestrator_for_testing(sample_config, mock_repository_host)
    evidence = source(restarted.deps.issue_run_ledger).evidence_for_issue(42)
    assert evidence.runs == (record,)


def test_test_composition_uses_the_same_durable_run_contract(sample_orchestrator, tmp_path):
    ledger = sample_orchestrator.deps.issue_run_ledger
    record = run_record(tmp_path)
    ledger.record_run(42, record)
    assert source(ledger, (record,)).evidence_for_issue(42).origin is IssueRunEvidenceOrigin.BOTH


def test_no_recorded_runs_is_an_explicit_positive_fact(tmp_path):
    evidence = source(SqliteIssueRunLedger(tmp_path / "runs.sqlite")).evidence_for_issue(42)
    assert evidence.status is IssueRunEvidenceStatus.NO_RUNS_RECORDED
    assert evidence.runs == ()


def test_live_registry_requires_exact_matching_durable_ownership(tmp_path):
    ledger = SqliteIssueRunLedger(tmp_path / "runs.sqlite")
    record = run_record(tmp_path)
    with pytest.raises(IssueRunEvidenceUnavailable, match="no matching durable"):
        source(ledger, (record,)).evidence_for_issue(42)
    ledger.record_run(42, record)
    evidence = source(ledger, (record,)).evidence_for_issue(42)
    assert evidence.origin is IssueRunEvidenceOrigin.BOTH
    assert evidence.runs == (record,)
    replacement = run_record(tmp_path, "replacement")
    with pytest.raises(IssueRunEvidenceUnavailable, match="no matching durable"):
        source(ledger, (replacement,)).evidence_for_issue(42)


def test_repeated_registration_is_idempotent_but_cannot_rebind_a_run(tmp_path):
    ledger = SqliteIssueRunLedger(tmp_path / "runs.sqlite")
    record = run_record(tmp_path)
    ledger.record_run(42, record)
    ledger.record_run(42, replace(record, recorded_at="2026-09-07T12:00:00Z"))
    assert ledger.recorded_runs(42) == (record,)
    for issue_number, conflict in (
        (43, record),
        (42, replace(record, session_key=SessionKey(record.session_key.issue, TaskKind.REWORK))),
        (42, replace(record, run=run_record(tmp_path / "other").run)),
    ):
        with pytest.raises(IssueRunEvidenceUnavailable, match="Conflicting ownership"):
            ledger.record_run(issue_number, conflict)
    assert ledger.recorded_runs(42) == (record,)
    assert ledger.recorded_runs(43) == ()


@pytest.mark.parametrize("damage", ["missing", "table", "payload", "identity"])
def test_unreadable_ledger_never_becomes_empty_evidence(tmp_path, damage):
    path = tmp_path / "runs.sqlite"
    ledger = SqliteIssueRunLedger(path)
    ledger.record_run(42, run_record(tmp_path))
    if damage == "missing":
        path.unlink()
    else:
        with sqlite3.connect(path) as conn:
            if damage == "table":
                conn.execute("DROP TABLE issue_runs")
            elif damage == "payload":
                conn.execute("UPDATE issue_runs SET assets_json='{}'")
            else:
                conn.execute("UPDATE issue_runs SET run_id='wrong'")
    with pytest.raises(IssueRunEvidenceUnavailable):
        source(ledger).evidence_for_issue(42)
    if damage == "missing":
        assert not path.exists()


def test_unavailable_live_owner_is_not_ignored(tmp_path):
    ledger = SqliteIssueRunLedger(tmp_path / "runs.sqlite")
    service = IssueRunEvidenceService(
        ledger, live_runs=Mock(side_effect=RuntimeError("unavailable")), now=lambda: NOW,
    )
    with pytest.raises(IssueRunEvidenceUnavailable):
        service.evidence_for_issue(42)


@pytest.mark.parametrize("operation", ["read", "write", "reconstruct"])
@pytest.mark.parametrize("damage", ["replacement", "missing_identity", "extra_identity"])
def test_every_connection_refuses_changed_ledger_identity(tmp_path, operation, damage):
    path = tmp_path / "runs.sqlite"
    ledger = SqliteIssueRunLedger(path)
    record = run_record(tmp_path)
    ledger.record_run(42, record)
    if damage == "replacement":
        replacement_path = tmp_path / "replacement.sqlite"
        SqliteIssueRunLedger(replacement_path)
        # Both databases have closed connections. Replace only the database,
        # retaining the established handle and its durable identity marker.
        replacement_path.replace(path)
    else:
        with closing(sqlite3.connect(path)) as conn, conn:
            if damage == "missing_identity":
                conn.execute("DELETE FROM issue_run_ledger_identity")
            else:
                conn.execute("INSERT INTO issue_run_ledger_identity VALUES ('unexpected')")

    with pytest.raises(IssueRunEvidenceUnavailable):
        if operation == "read":
            source(ledger).evidence_for_issue(42)
        elif operation == "write":
            ledger.record_run(43, run_record(tmp_path, "run-2"))
        else:
            SqliteIssueRunLedger(path)
    # Refusal must happen before a writer alters the replacement/damaged ledger.
    with closing(sqlite3.connect(path)) as conn:
        assert conn.execute("SELECT COUNT(*) FROM issue_runs WHERE issue_number=43").fetchone()[0] == 0


@pytest.mark.parametrize("symlink_location", ["run_dir", "worktree"])
@pytest.mark.parametrize("reconstruct", [False, True])
def test_symlink_retarget_cannot_rebind_lexical_run_root(tmp_path, symlink_location, reconstruct):
    path = tmp_path / "runs.sqlite"
    original = run_record(tmp_path)
    link = original.run.run_dir if symlink_location == "run_dir" else original.run.worktree_path
    link.parent.mkdir(parents=True, exist_ok=True)
    targets = (link.parent / "target-one", link.parent / "target-two")
    for target in targets:
        target.mkdir()
    link.symlink_to(targets[0], target_is_directory=True)
    ledger = SqliteIssueRunLedger(path)
    ledger.record_run(42, original)

    link.unlink()
    link.symlink_to(targets[1], target_is_directory=True)
    if reconstruct:
        ledger = SqliteIssueRunLedger(path)
    # Rebuild the typed assets just as a new process would after retargeting.
    reconstructed = replace(original, run=SessionRunAssets.from_dict(original.run.to_dict()))
    assert source(ledger).evidence_for_issue(42).runs == (reconstructed,)
    ledger.record_run(42, reconstructed)
    impostor = replace(
        reconstructed,
        run=replace(reconstructed.run, identity=replace(original.run.identity, started_at="2026-09-07T00:00:00Z")),
    )
    with pytest.raises(IssueRunEvidenceUnavailable):
        ledger.record_run(43, impostor)
    assert ledger.recorded_runs(42) == (original,)
    assert ledger.recorded_runs(43) == ()


@pytest.mark.parametrize("damage", ["missing", "table", "identity", "marker"])
def test_restart_refuses_lost_established_ownership(tmp_path, damage):
    path = tmp_path / "runs.sqlite"
    ledger = SqliteIssueRunLedger(path)
    ledger.record_run(42, run_record(tmp_path))
    if damage == "missing":
        path.unlink()
    elif damage == "marker":
        path.with_suffix(".sqlite.initialized").unlink()
    else:
        with sqlite3.connect(path) as conn:
            conn.execute("DROP TABLE issue_runs" if damage == "table" else "DELETE FROM issue_run_ledger_identity")
    with pytest.raises(IssueRunEvidenceUnavailable):
        SqliteIssueRunLedger(path)
    if damage == "missing":
        assert not path.exists()


def test_a_different_identity_cannot_rebind_retained_run_directory(tmp_path):
    ledger = SqliteIssueRunLedger(tmp_path / "runs.sqlite")
    original = run_record(tmp_path)
    ledger.record_run(42, original)
    identity = replace(original.run.identity, started_at="2026-09-07T00:00:00Z")
    impostor = replace(original, run=replace(original.run, identity=identity))
    with pytest.raises(IssueRunEvidenceUnavailable):
        ledger.record_run(42, impostor)
    assert ledger.recorded_runs(42) == (original,)


def test_same_clock_allocations_have_exclusive_distinct_roots(tmp_path):
    from unittest.mock import patch

    class FixedClock:
        @staticmethod
        def now(tz):
            from datetime import datetime
            return datetime.fromisoformat(NOW.replace("Z", "+00:00"))

    # Each worker has its own allocator, as independent engines do. A fixed
    # clock makes the second-resolution collision deterministic without sleep.
    with patch("issue_orchestrator.execution.session_output_adapter.datetime", FixedClock):
        with ThreadPoolExecutor(max_workers=2) as pool:
            runs = tuple(pool.map(lambda _: FileSystemSessionOutput().start_run(tmp_path, "coding-1"), range(2)))
    assert runs[0].run_dir != runs[1].run_dir
    for index, run in enumerate(runs):
        run.terminal_recording.path.write_bytes(f"raw output {index}\r\n".encode())
        assert run.manifest_path.exists()
        assert FileSystemSessionOutput().read_manifest(run.run_dir)["run_id"] == run.run_id
    assert runs[0].terminal_recording.path.read_bytes() == b"raw output 0\r\n"
    assert runs[1].terminal_recording.path.read_bytes() == b"raw output 1\r\n"


@pytest.mark.parametrize("status,runs", [
    (IssueRunEvidenceStatus.RUNS_RECORDED, ()),
    (IssueRunEvidenceStatus.NO_RUNS_RECORDED, (object(),)),
])
def test_evidence_rejects_impossible_status_payloads(status, runs):
    with pytest.raises(ValueError, match="status must agree"):
        IssueRunEvidence(42, status, runs, IssueRunEvidenceOrigin.RUN_LEDGER, NOW)


@pytest.mark.parametrize("field", ["status", "origin"])
def test_evidence_rejects_untyped_enum_values(field):
    evidence = IssueRunEvidence(
        42, IssueRunEvidenceStatus.NO_RUNS_RECORDED, (), IssueRunEvidenceOrigin.RUN_LEDGER, NOW,
    )
    with pytest.raises(TypeError, match=f"{field} must be typed"):
        replace(evidence, **{field: str(getattr(evidence, field))})


def test_old_ledger_reopens_without_inventing_branch_binding(tmp_path):
    path = tmp_path / "runs.sqlite"
    record = run_record(tmp_path)
    ledger = SqliteIssueRunLedger(path)
    ledger.record_run(42, record)
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE issue_runs DROP COLUMN branch_name")
    reopened = SqliteIssueRunLedger(path)
    assert reopened.recorded_runs(42) == (replace(record, branch_name=None),)
    with pytest.raises(IssueRunEvidenceUnavailable):
        reopened.record_run(42, record)
    assert SqliteIssueRunLedger(path).recorded_runs(42)[0].branch_name is None
