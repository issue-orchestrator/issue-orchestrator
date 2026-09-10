"""Real filesystem/Git escrow crash prefixes with normal SQLite admission."""

import hashlib
import json
import sqlite3
from dataclasses import replace
from datetime import datetime
from pathlib import Path

import pytest

from issue_orchestrator.control.validated_work_escrow import (
    EscrowInspection,
    ValidatedWorkEscrowMaintenance,
)
from issue_orchestrator.domain.validated_work import (
    RemoteBaselineStatus,
    ValidatedWorkState,
    ValidatedWorkFailure,
    EvidenceRole,
    canonical_json,
)
from issue_orchestrator.domain.validated_work_escrow import evidence_pins
from issue_orchestrator.execution.git_working_copy import GitWorkingCopy
from issue_orchestrator.execution.validated_work_ancestry import (
    GitValidatedWorkAncestry,
)
from issue_orchestrator.infra.validated_work_escrow import FilesystemValidatedWorkEscrow
from issue_orchestrator.infra.validated_work_inspection import (
    SqliteValidatedWorkEvidenceReader,
)
from issue_orchestrator.infra.validated_work_store import SqliteValidatedWorkStore
from .git_escrow_support import git_rig, real_capture
from .validated_work_support import Liveness, claim, begin, finalize, LATER


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    return git_rig(tmp_path)


def escrow_for(rig, *, working=None):
    return FilesystemValidatedWorkEscrow(
        rig.root / ".issue-orchestrator/state/validated-work",
        repository=rig.root,
        repo_slug="owner/repo",
        git=working or rig.working,
    )


def store_for(rig, escrow):
    return SqliteValidatedWorkStore(
        rig.root / ".issue-orchestrator/state/validated_work.sqlite",
        ancestry=GitValidatedWorkAncestry(
            repository=rig.root, repo_slug="owner/repo", git=rig.working
        ),
        artifacts=escrow,
        retention=escrow,
        liveness=Liveness(),
    )


def maintenance(escrow, store, days=30):
    return ValidatedWorkEscrowMaintenance(
        escrow=escrow, store=store, retention_days=days
    )


def rewrite_as_legacy_envelope(path: Path, *, extra_field: bool = False) -> bytes:
    document = json.loads(path.read_bytes())
    document.pop("checksum")
    document["observations"].pop("remote_baseline_status")
    if extra_field:
        document["unknown"] = "must remain rejected"
    document["checksum"] = hashlib.sha256(canonical_json(document).encode()).hexdigest()
    encoded = canonical_json(document).encode()
    path.write_bytes(encoded)
    return encoded


class CrashPinGit(GitWorkingCopy):
    def __init__(self, git, after):
        super().__init__(git=git)
        self.after = after
        self.calls = 0

    def pin_ref(self, repository, *, ref, sha):
        if self.calls == self.after:
            raise RuntimeError("crash at pin boundary")
        self.calls += 1
        return super().pin_ref(repository, ref=ref, sha=sha)


@pytest.mark.parametrize("prefix", [0, 1, 2, 3])
def test_every_capture_prefix_repairs_without_source_worktree(rig, tmp_path, prefix):
    worktree = tmp_path / "source"
    rig.run("worktree", "add", "--detach", str(worktree), rig.tip)
    admission, sources = real_capture(
        rig,
        worktree / "run",
        state=ValidatedWorkState.PARKED,
        observed=rig.tip,
        failure=ValidatedWorkFailure.WORKTREE_AHEAD_OF_VALIDATION,
        reason="unvalidated commits retained",
    )
    interrupted = escrow_for(rig, working=CrashPinGit(rig.git, prefix))
    if prefix < 2:
        with pytest.raises(RuntimeError, match="crash"):
            interrupted.capture(admission, sources)
    else:
        interrupted.capture(admission, sources)
    escrow = escrow_for(rig)
    store = store_for(rig, escrow)
    if prefix == 3:
        store.admit(admission)
    rig.run("worktree", "remove", "--force", str(worktree))
    report = maintenance(escrow, store).reconcile_escrow_orphans()
    assert not report.problems
    assert report.repaired == (() if prefix == 3 else (admission.evidence.evidence_id,))
    lookup = store.evidence_for_id(admission.evidence.evidence_id)
    assert lookup is not None
    assert lookup.evidence.admission == admission
    assert lookup.record.state is ValidatedWorkState.PARKED
    assert escrow.verifies(lookup.evidence)
    rig.run("gc", "--prune=now")
    assert escrow.verifies(lookup.evidence)
    assert maintenance(escrow, store).reconcile_escrow_orphans().repaired == ()


def test_content_addressed_replay_keeps_original_envelope_and_uses_no_sources(
    rig, tmp_path
):
    admission, sources = real_capture(rig, tmp_path / "sources")
    escrow = escrow_for(rig)
    escrow.capture(admission, sources)
    capture_path = escrow.root / admission.escrow_dir / "capture.json"
    before = capture_path.read_bytes()
    sources.completion.unlink()
    sources.validation.unlink()
    newer = replace(
        admission,
        evidence=replace(
            admission.evidence,
            observations=replace(
                admission.evidence.observations,
                expected_remote_head_sha=rig.tip,
                pr_number=555,
            ),
        ),
        initial_reason="changed",
    )
    assert escrow.capture(newer, sources) == admission
    assert capture_path.read_bytes() == before


def test_legacy_parked_envelope_survives_database_migration_and_reconciliation(
    rig, tmp_path
):
    admission, sources = real_capture(
        rig,
        tmp_path / "sources",
        state=ValidatedWorkState.PARKED,
        failure=ValidatedWorkFailure.WORKTREE_AHEAD_OF_VALIDATION,
        reason="retained before remote authority provenance",
    )
    escrow = escrow_for(rig)
    store = store_for(rig, escrow)
    store.admit(escrow.capture(admission, sources))
    capture_path = escrow.root / admission.escrow_dir / "capture.json"
    immutable_legacy = rewrite_as_legacy_envelope(capture_path)
    database = rig.root / ".issue-orchestrator/state/validated_work.sqlite"
    with sqlite3.connect(database) as conn:
        observations = json.loads(canonical_json(admission.evidence.observations))
        observations.pop("remote_baseline_status")
        conn.execute(
            "UPDATE validated_work_evidence SET observations=? WHERE evidence_id=?",
            (canonical_json(observations), admission.evidence.evidence_id),
        )

    reopened = store_for(rig, escrow)
    report = maintenance(escrow, reopened).reconcile_escrow_orphans()
    assert not report.problems
    (reopened_row,) = reopened.retained_evidence(6914)
    assert reopened_row.admission.initial_state is ValidatedWorkState.PARKED
    assert (
        reopened_row.admission.evidence.observations.remote_baseline_status
        is RemoteBaselineStatus.UNOBSERVED
    )
    assert reopened_row.admission.evidence.observations.expected_remote_head_sha is None
    assert reopened_row.admission.evidence.observations.pr_number is None
    assert escrow.inspect(admission.escrow_dir) == reopened_row.admission
    assert capture_path.read_bytes() == immutable_legacy


def test_legacy_queued_orphan_replays_as_parked_without_rewriting_capture(
    rig, tmp_path
):
    admission, sources = real_capture(rig, tmp_path / "sources")
    escrow = escrow_for(rig)
    escrow.capture(admission, sources)
    capture_path = escrow.root / admission.escrow_dir / "capture.json"
    immutable_legacy = rewrite_as_legacy_envelope(capture_path)
    store = store_for(rig, escrow)

    report = maintenance(escrow, store).reconcile_escrow_orphans()

    assert report.repaired == (admission.evidence.evidence_id,)
    assert not report.problems
    repaired = store.evidence_for_id(admission.evidence.evidence_id)
    assert repaired is not None
    assert repaired.record.state is ValidatedWorkState.PARKED
    assert repaired.record.failure is ValidatedWorkFailure.REMOTE_UNREADABLE
    assert repaired.evidence.admission.initial_state is ValidatedWorkState.PARKED
    assert (
        repaired.evidence.admission.evidence.observations.remote_baseline_status
        is RemoteBaselineStatus.UNOBSERVED
    )
    assert capture_path.read_bytes() == immutable_legacy


def test_legacy_envelope_still_rejects_authenticated_unknown_fields(rig, tmp_path):
    admission, sources = real_capture(rig, tmp_path / "sources")
    escrow = escrow_for(rig)
    escrow.capture(admission, sources)
    capture_path = escrow.root / admission.escrow_dir / "capture.json"
    rewrite_as_legacy_envelope(capture_path, extra_field=True)

    with pytest.raises(ValueError, match="noncanonical"):
        escrow.inspect(admission.escrow_dir)


@pytest.mark.parametrize(
    "damage",
    ["hash", "size", "envelope", "identity", "directory", "pin", "symlink", "missing"],
)
def test_malformed_or_missing_evidence_is_reported_retained_never_admitted(
    rig, tmp_path, damage
):
    admission, sources = real_capture(rig, tmp_path / "sources")
    escrow = escrow_for(rig)
    escrow.capture(admission, sources)
    directory = escrow.root / admission.escrow_dir
    if damage in {"hash", "size"}:
        path = directory / "completion.json"
        path.write_bytes(b"x" * (path.stat().st_size + (damage == "size")))
    elif damage in {"envelope", "identity"}:
        path = directory / "capture.json"
        data = json.loads(path.read_text())
        data["record_id"] = "r1:" + "0" * 64
        if damage == "identity":
            import hashlib
            from issue_orchestrator.domain.validated_work import canonical_json

            data.pop("checksum")
            data["checksum"] = hashlib.sha256(canonical_json(data).encode()).hexdigest()
        path.write_text(json.dumps(data))
    elif damage == "directory":
        directory = directory.rename(directory.with_name("e1:" + "c" * 64))
    elif damage == "pin":
        rig.run("update-ref", admission.pinned_ref, rig.tip)
    elif damage == "symlink":
        path = directory / "validation.json"
        path.unlink()
        path.symlink_to(sources.validation)
    else:
        rig.run("update-ref", "-d", admission.pinned_ref)
        # Remove the only exact target object to make repinning impossible.
        rig.run("checkout", "--detach", rig.base)
        rig.run("branch", "-D", "main")
        rig.run("reflog", "expire", "--expire=now", "--all")
        rig.run("gc", "--prune=now")
    store = store_for(rig, escrow)
    report = maintenance(escrow, store).reconcile_escrow_orphans()
    assert report.problems
    assert not report.repaired
    assert store.evidence_for_id(admission.evidence.evidence_id) is None
    assert directory.exists()


def test_orphan_admission_attaches_during_publish_and_retains_gate(rig, tmp_path):
    first, sources = real_capture(rig, tmp_path / "sources")
    escrow = escrow_for(rig)
    store = store_for(rig, escrow)
    store.admit(escrow.capture(first, sources))
    token = claim(store, first)
    assert begin(store, token) is not None
    orphan, sources2 = real_capture(
        rig,
        tmp_path / "second",
        run="second",
        state=ValidatedWorkState.PARKED,
        observed=rig.tip,
        failure=ValidatedWorkFailure.WORKTREE_AHEAD_OF_VALIDATION,
        reason="park forever until approved",
    )
    escrow.capture(orphan, sources2)
    report = maintenance(escrow, store).reconcile_escrow_orphans()
    assert not report.problems
    attached = store.attached_evidence(first.evidence.record_id)
    assert len(attached) == 1 and attached[0].admission == orphan
    assert attached[0].role is EvidenceRole.ATTACHED
    assert store.get(first.evidence.record_id).evidence_id == first.evidence.evidence_id


def test_retention_uses_resolved_records_all_roles_and_configured_window(rig, tmp_path):
    escrow = escrow_for(rig)
    store = store_for(rig, escrow)
    first, sources = real_capture(rig, tmp_path / "first")
    store.admit(escrow.capture(first, sources))
    second, sources2 = real_capture(rig, tmp_path / "second", run="second")
    store.admit(escrow.capture(second, sources2))
    token = claim(store, second)
    attempt = begin(store, token)
    assert attempt is not None
    attached, sources3 = real_capture(rig, tmp_path / "third", run="third")
    store.admit(escrow.capture(attached, sources3))
    assert store.attached_evidence(first.evidence.record_id)
    finalize(store, token, attempt)
    assert store.relinquish_claim(token)
    owner = maintenance(escrow, store, days=30)
    assert (
        owner.sweep(now=datetime.fromisoformat("2026-10-05T13:00:00+00:00")).repaired
        == ()
    )
    report = owner.sweep(now=datetime.fromisoformat("2026-10-07T13:00:00+00:00"))
    assert not report.problems
    assert set(report.repaired) == {
        a.evidence.evidence_id for a in (first, second, attached)
    }
    assert rig.working.retained_refs(rig.root) == ()
    assert (
        store.evidence_for_retention(released_before="2027-01-01T00:00:00+00:00") == ()
    )


@pytest.mark.parametrize(
    "state",
    [
        ValidatedWorkState.QUEUED,
        ValidatedWorkState.PARKED,
        ValidatedWorkState.FAILED,
        ValidatedWorkState.PUBLISHING,
    ],
)
def test_every_unresolved_state_retains_all_evidence_roles(rig, tmp_path, state):
    admission, sources = real_capture(
        rig,
        tmp_path / "first",
        state=ValidatedWorkState.QUEUED
        if state is ValidatedWorkState.PUBLISHING
        else state,
        failure=ValidatedWorkFailure.PUSH_FAILED
        if state is ValidatedWorkState.FAILED
        else None,
    )
    escrow = escrow_for(rig)
    store = store_for(rig, escrow)
    store.admit(escrow.capture(admission, sources))
    if state is ValidatedWorkState.PUBLISHING:
        assert begin(store, claim(store, admission)) is not None
    second, sources2 = real_capture(
        rig,
        tmp_path / "second",
        run="second",
        state=admission.initial_state,
        failure=admission.initial_failure,
    )
    store.admit(escrow.capture(second, sources2))
    assert (
        not maintenance(escrow, store, 1)
        .sweep(now=datetime.fromisoformat("2099-01-01T00:00:00+00:00"))
        .repaired
    )
    assert (escrow.root / admission.escrow_dir).is_dir()
    assert (escrow.root / second.escrow_dir).is_dir()
    assert len(rig.working.retained_refs(rig.root)) == 2


def test_readonly_doctor_reports_orphan_without_creating_database(rig, tmp_path):
    admission, sources = real_capture(rig, tmp_path / "first")
    escrow = escrow_for(rig)
    escrow.capture(admission, sources)
    database = tmp_path / "not-created.sqlite"
    reader = SqliteValidatedWorkEvidenceReader(database)
    report = EscrowInspection(escrow=escrow, reader=reader).inspect_orphans()
    assert len(report.problems) == 1
    assert "no evidence row" in report.problems[0].detail
    assert not database.exists()


def test_escrow_root_cannot_live_in_a_disposable_worktree(rig, tmp_path):
    worktree = tmp_path / "disposable"
    rig.run("worktree", "add", "--detach", str(worktree), rig.target)
    with pytest.raises(ValueError, match="outside disposable"):
        FilesystemValidatedWorkEscrow(
            worktree / "state",
            repository=rig.root,
            repo_slug="owner/repo",
            git=rig.working,
        )


def test_partial_capture_is_inert_and_reported(rig, tmp_path):
    admission, sources = real_capture(rig, tmp_path / "first")
    sources.validation.write_bytes(b"bad source")
    escrow = escrow_for(rig)
    with pytest.raises(ValueError):
        escrow.capture(admission, sources)
    store = store_for(rig, escrow)
    report = maintenance(escrow, store).reconcile_escrow_orphans()
    assert len(report.problems) == 1 and "partial" in report.problems[0].detail
    assert not report.repaired


def test_config_composition_supplies_actual_retention_window(rig, tmp_path):
    from issue_orchestrator.entrypoints.bootstrap import (
        build_validated_work_escrow_maintenance,
    )
    from issue_orchestrator.infra.config import Config
    from issue_orchestrator.infra.config_models import ValidatedWorkConfig

    escrow = escrow_for(rig)
    store = store_for(rig, escrow)
    admission, sources = real_capture(rig, tmp_path / "sources")
    store.admit(escrow.capture(admission, sources))
    token = claim(store, admission)
    attempt = begin(store, token)
    assert attempt is not None
    finalize(store, token, attempt)
    assert store.relinquish_claim(token)
    config = Config(
        repo_root=rig.root, repo="owner/repo", validated_work=ValidatedWorkConfig(1)
    )
    owner = build_validated_work_escrow_maintenance(
        config, store=store, working_copy=rig.working
    )
    assert owner.sweep(
        now=datetime.fromisoformat("2026-09-08T13:00:00+00:00")
    ).repaired == (admission.evidence.evidence_id,)


def test_retained_owner_and_damaged_evidence_cannot_be_cleaned(rig, tmp_path):
    escrow = escrow_for(rig)
    store = store_for(rig, escrow)
    admission, sources = real_capture(rig, tmp_path / "sources")
    store.admit(escrow.capture(admission, sources))
    token = claim(store, admission)
    attempt = begin(store, token)
    assert attempt is not None
    finalize(store, token, attempt)
    owner = maintenance(escrow, store, 1)
    now = datetime.fromisoformat("2026-10-08T13:00:00+00:00")
    assert owner.sweep(now=now).repaired == ()
    assert store.relinquish_claim(token)
    (escrow.root / admission.escrow_dir / "validation.json").unlink()
    assert owner.sweep(now=now).problems
    assert rig.working.verify_ref(rig.root, ref=admission.pinned_ref, sha=rig.target)
    assert (escrow.root / admission.escrow_dir / "completion.json").exists()


def test_verification_uses_immutable_capture_when_store_observations_refresh(
    rig, tmp_path
):
    escrow = escrow_for(rig)
    store = store_for(rig, escrow)
    admission, sources = real_capture(rig, tmp_path / "sources")
    store.admit(escrow.capture(admission, sources))
    refreshed = replace(
        admission,
        evidence=replace(
            admission.evidence,
            observations=replace(
                admission.evidence.observations,
                expected_remote_head_sha=rig.tip,
                pr_number=789,
            ),
        ),
    )
    store.admit(refreshed)
    lookup = store.evidence_for_id(admission.evidence.evidence_id)
    assert lookup is not None and lookup.evidence.observation_revision == 1
    assert lookup.evidence.admission.evidence.observations.pr_number == 789
    assert escrow.verifies(lookup.evidence)
    assert escrow.inspect(admission.escrow_dir) == admission


def test_doctor_entrypoint_reports_orphans_without_mutation(rig, tmp_path):
    from issue_orchestrator.infra.config import Config
    from issue_orchestrator.infra.doctor.checks.validated_work import (
        check_validated_work,
    )

    escrow = escrow_for(rig)
    admission, sources = real_capture(rig, tmp_path / "sources")
    escrow.capture(admission, sources)
    checks = check_validated_work(
        Config(repo_root=rig.root, repo="owner/repo"), rig.git.runner
    )
    assert len(checks) == 1 and checks[0].status == "warning"
    assert "no evidence row" in checks[0].detail
    assert not (rig.root / ".issue-orchestrator/state/validated_work.sqlite").exists()


def test_retention_rechecks_state_after_candidate_query(rig, tmp_path):
    # A stale candidate is not itself deletion authority, regardless of the caller.
    escrow = escrow_for(rig)
    store = store_for(rig, escrow)
    admission, sources = real_capture(rig, tmp_path / "sources")
    store.admit(escrow.capture(admission, sources))
    assert not store.release_evidence_for_retention(
        admission.evidence.evidence_id,
        released_before="2099-01-01T00:00:00+00:00",
        released_at="2099-01-02T00:00:00+00:00",
    )
    assert escrow.inspect(admission.escrow_dir) == admission
