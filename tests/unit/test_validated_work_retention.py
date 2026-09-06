"""Retention validates the same durable dispositions as the teardown safety probe."""

import sqlite3
from contextlib import closing
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import pytest

from issue_orchestrator.control.validated_work_escrow import (
    ValidatedWorkEscrowMaintenance,
)
from issue_orchestrator.domain.validated_work import ValidatedWorkState as State
from issue_orchestrator.domain.validated_work_store import (
    AdmissionStatus,
    EvidenceAdmission,
)
from issue_orchestrator.execution.validated_work_ancestry import (
    GitValidatedWorkAncestry,
)
from issue_orchestrator.infra.validated_work_escrow import FilesystemValidatedWorkEscrow
from issue_orchestrator.infra.validated_work_store import SqliteValidatedWorkStore
from .git_escrow_support import GitRig, git_rig, real_capture
from .validated_work_support import LATER, Liveness

CUTOFF = "2099-01-01T00:00:00+00:00"
NOW = datetime.fromisoformat("2099-02-01T00:00:00+00:00")


@dataclass
class RetentionRig:
    git: GitRig
    escrow: FilesystemValidatedWorkEscrow
    admission: EvidenceAdmission
    database: Path

    def open(self, *, retention=None):
        return SqliteValidatedWorkStore(
            self.database,
            ancestry=GitValidatedWorkAncestry(
                repository=self.git.root, repo_slug="owner/repo", git=self.git.working
            ),
            artifacts=self.escrow,
            retention=self.escrow if retention is None else retention,
            liveness=Liveness(),
        )

    def owner(self, store):
        return ValidatedWorkEscrowMaintenance(
            escrow=self.escrow, store=store, retention_days=30
        )

    def resolve(self, state):
        if state is State.RECOVERED:
            self.open().resolve_observed_merge(
                record_id=self.admission.evidence.record_id,
                merged_head_sha=self.git.target,
                observed_at=LATER,
            )
        else:
            # The abandonment command is a later slice: install its valid audit contract.
            with closing(sqlite3.connect(self.database)) as conn, conn:
                conn.execute(
                    "UPDATE validated_work_records SET state='abandoned', resolution_kind='operator_abandoned', "
                    "resolved_by='operator', resolution_reason='accepted loss', resolved_at=?, terminal_at=? "
                    "WHERE record_id=?",
                    (LATER, LATER, self.admission.evidence.record_id),
                )
        assert self.open().get(self.admission.evidence.record_id).state is state

    def snapshot(self):
        with closing(sqlite3.connect(self.database)) as conn:
            durable = tuple(conn.iterdump())
        files = {
            str(p.relative_to(self.escrow.root)): p.read_bytes()
            for p in self.escrow.root.rglob("*")
            if p.is_file()
        }
        return durable, files, self.git.working.retained_refs(self.git.root)


@pytest.fixture
def retained(tmp_path, monkeypatch):
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    git = git_rig(tmp_path)
    escrow = FilesystemValidatedWorkEscrow(
        git.root / ".issue-orchestrator/state/validated-work",
        repository=git.root,
        repo_slug="owner/repo",
        git=git.working,
    )
    admission, sources = real_capture(git, tmp_path / "sources")
    result = RetentionRig(
        git,
        escrow,
        admission,
        git.root / ".issue-orchestrator/state/validated_work.sqlite",
    )
    result.open().admit(escrow.capture(admission, sources))
    return result


@pytest.mark.parametrize(
    "state,column,value",
    [
        (State.RECOVERED, "published_head_sha", ""),
        (State.RECOVERED, "published_head_sha", "invalid-sha"),
        (State.ABANDONED, "resolved_by", ""),
        (State.ABANDONED, "resolution_reason", ""),
        (State.ABANDONED, "resolved_at", ""),
    ],
)
@pytest.mark.parametrize("operation", ["candidates", "release", "sweep"])
def test_malformed_terminal_disposition_never_releases_evidence(
    retained, state, column, value, operation
):
    retained.resolve(state)
    store = retained.open()
    # A valid earlier query must not exempt the release transaction from validation.
    assert store.evidence_for_retention(released_before=CUTOFF)
    with closing(sqlite3.connect(retained.database)) as conn, conn:
        conn.execute(f"UPDATE validated_work_records SET {column}=?", (value,))
    before = retained.snapshot()
    with pytest.raises(ValueError):
        store.has_unresolved_work(6914)
    with pytest.raises(ValueError):
        if operation == "candidates":
            store.evidence_for_retention(released_before=CUTOFF)
        elif operation == "release":
            store.release_evidence_for_retention(
                retained.admission.evidence.evidence_id,
                released_before=CUTOFF,
                released_at=NOW.isoformat(),
            )
        else:
            retained.owner(store).sweep(now=NOW)
    assert retained.snapshot() == before


@pytest.mark.parametrize("state", [State.RECOVERED, State.ABANDONED])
def test_valid_terminal_disposition_releases_and_retries_after_reopen(retained, state):
    retained.resolve(state)
    evidence_id = retained.admission.evidence.evidence_id
    assert [
        r.evidence_id
        for r in retained.open().evidence_for_retention(released_before=CUTOFF)
    ] == [evidence_id]
    report = retained.owner(retained.open()).sweep(now=NOW)
    assert report.repaired == (evidence_id,) and not report.problems
    assert not (retained.escrow.root / retained.admission.escrow_dir).exists()
    assert retained.git.working.retained_refs(retained.git.root) == ()
    store = retained.open()
    assert store.evidence_for_id(evidence_id).evidence.released_at == NOW.isoformat()
    before = retained.snapshot()
    assert retained.owner(store).sweep(now=NOW).repaired == ()
    assert not store.release_evidence_for_retention(
        evidence_id, released_before=CUTOFF, released_at=NOW.isoformat()
    )
    assert retained.snapshot() == before


def test_malformed_later_candidate_prevents_partial_sweep(retained, tmp_path):
    retained.resolve(State.RECOVERED)
    second, sources = real_capture(
        retained.git, tmp_path / "second", issue=6915, at=LATER
    )
    store = retained.open()
    store.admit(retained.escrow.capture(second, sources))
    store.resolve_observed_merge(
        record_id=second.evidence.record_id,
        merged_head_sha=retained.git.target,
        observed_at=LATER,
    )
    assert [
        row.evidence_id for row in store.evidence_for_retention(released_before=CUTOFF)
    ] == [retained.admission.evidence.evidence_id, second.evidence.evidence_id]
    with closing(sqlite3.connect(retained.database)) as conn, conn:
        conn.execute(
            "UPDATE validated_work_records SET published_head_sha='' WHERE record_id=?",
            (second.evidence.record_id,),
        )
    before = retained.snapshot()
    with pytest.raises(ValueError):
        retained.owner(store).sweep(now=NOW)
    assert retained.snapshot() == before


def test_reopened_work_invalidates_retention_candidates_for_every_evidence_role(
    retained, tmp_path
):
    retained.resolve(State.ABANDONED)
    first = retained.open()
    candidate = first.evidence_for_retention(released_before=CUTOFF)[0]
    new, sources = real_capture(retained.git, tmp_path / "second", run="replacement")
    outcome = retained.open().admit(retained.escrow.capture(new, sources))
    assert outcome.status is AdmissionStatus.REOPENED
    before = retained.snapshot()
    assert not first.release_evidence_for_retention(
        candidate.evidence_id, released_before=CUTOFF, released_at=NOW.isoformat()
    )
    assert retained.owner(first).sweep(now=NOW).repaired == ()
    assert retained.snapshot() == before
    first.resolve_observed_merge(
        record_id=new.evidence.record_id,
        merged_head_sha=retained.git.target,
        observed_at=LATER,
    )
    report = retained.owner(retained.open()).sweep(now=NOW)
    assert not report.problems
    assert set(report.repaired) == {candidate.evidence_id, new.evidence.evidence_id}


def test_claim_acquired_after_candidate_query_refuses_cleanup_until_relinquished(
    retained,
):
    retained.resolve(State.RECOVERED)
    first, second = retained.open(), retained.open()
    candidate = first.evidence_for_retention(released_before=CUTOFF)[0]
    token = second.acquire_claim(
        candidate.record_id,
        expected_states=frozenset({State.RECOVERED}),
        evidence_id=candidate.evidence_id,
    )
    assert token is not None
    before = retained.snapshot()
    assert not first.release_evidence_for_retention(
        candidate.evidence_id, released_before=CUTOFF, released_at=NOW.isoformat()
    )
    assert retained.snapshot() == before
    assert second.holds_claim(token)
    assert second.relinquish_claim(token)
    assert first.release_evidence_for_retention(
        candidate.evidence_id, released_before=CUTOFF, released_at=NOW.isoformat()
    )


def test_release_holds_write_transaction_through_filesystem_deletion(retained):
    retained.resolve(State.ABANDONED)

    class ContendingRelease:
        def release(self, evidence):
            # A second connection cannot change eligibility after it was validated.
            # timeout=0 makes lock refusal deterministic, without timing or polling.
            with closing(sqlite3.connect(retained.database, timeout=0)) as conn, conn:
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    conn.execute("UPDATE validated_work_records SET state='parked'")
            retained.escrow.release(evidence)

    store = retained.open(retention=ContendingRelease())
    assert store.release_evidence_for_retention(
        retained.admission.evidence.evidence_id,
        released_before=CUTOFF,
        released_at=NOW.isoformat(),
    )
    assert (
        retained.open().get(retained.admission.evidence.record_id).state
        is State.ABANDONED
    )
