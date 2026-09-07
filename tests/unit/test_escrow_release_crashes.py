"""Real Git/filesystem deletion prefixes resume only with durable store authority."""

import sqlite3
from contextlib import closing
from dataclasses import replace
from pathlib import Path

import pytest

from issue_orchestrator.domain.validated_work import ValidatedWorkState as State
from issue_orchestrator.execution.git_working_copy import GitWorkingCopy
from .test_validated_work_retention import retained as retained, CUTOFF, NOW
from .git_escrow_support import real_capture
from issue_orchestrator.infra.validated_work_release import ReleaseProgress


class InterruptedRelease(RuntimeError):
    pass


def release(retained):
    return retained.open().release_evidence_for_retention(
        retained.admission.evidence.evidence_id,
        released_before=CUTOFF,
        released_at=NOW.isoformat(),
    )


def interrupt_deletion(monkeypatch, retained, step, when):
    directory = retained.escrow.root / retained.admission.escrow_dir
    original_pin, original_unlink, original_rmdir = (
        GitWorkingCopy.delete_pinned_ref,
        Path.unlink,
        Path.rmdir,
    )
    calls = 0

    def action(callback):
        nonlocal calls
        calls += 1
        if calls == step and when == "before":
            raise InterruptedRelease()
        callback()
        if calls == step and when == "after":
            raise InterruptedRelease()

    def pin(self, repository, *, ref, sha):
        action(lambda: original_pin(self, repository, ref=ref, sha=sha))

    def unlink(path, *args, **kwargs):
        if path.parent == directory:
            action(lambda: original_unlink(path, *args, **kwargs))
        else:
            original_unlink(path, *args, **kwargs)

    def rmdir(path):
        if path == directory:
            action(lambda: original_rmdir(path))
        else:
            original_rmdir(path)

    monkeypatch.setattr(GitWorkingCopy, "delete_pinned_ref", pin)
    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(Path, "rmdir", rmdir)


@pytest.mark.parametrize("step", range(1, 7))
@pytest.mark.parametrize("when", ["before", "after"])
def test_each_destructive_prefix_resumes_after_store_reopen(
    retained, monkeypatch, step, when
):
    retained.resolve(State.RECOVERED)
    durable_before = retained.snapshot()[0]
    with monkeypatch.context() as fault:
        interrupt_deletion(fault, retained, step, when)
        with pytest.raises(InterruptedRelease):
            release(retained)
    assert retained.snapshot()[0] == durable_before
    assert release(retained)
    assert not (retained.escrow.root / retained.admission.escrow_dir).exists()
    assert retained.git.working.retained_refs(retained.git.root) == ()
    assert not release(retained)


def test_filesystem_complete_before_database_commit_resumes(retained):
    retained.resolve(State.RECOVERED)
    with closing(sqlite3.connect(retained.database)) as conn, conn:
        conn.execute(
            "CREATE TRIGGER crash_release BEFORE UPDATE OF released_at ON validated_work_evidence BEGIN SELECT RAISE(ABORT, 'simulated commit boundary'); END"
        )
    with pytest.raises(sqlite3.IntegrityError, match="simulated commit boundary"):
        release(retained)
    assert retained.git.working.retained_refs(retained.git.root) == ()
    assert not (retained.escrow.root / retained.admission.escrow_dir).exists()
    assert (
        retained.open()
        .evidence_for_id(retained.admission.evidence.evidence_id)
        .evidence.released_at
        == ""
    )
    with closing(sqlite3.connect(retained.database)) as conn, conn:
        conn.execute("DROP TRIGGER crash_release")
    assert release(retained)


def test_previously_deleted_pin_reappearing_is_verified_and_removed(
    retained, monkeypatch
):
    retained.resolve(State.RECOVERED)
    with monkeypatch.context() as fault:
        interrupt_deletion(fault, retained, 2, "before")
        with pytest.raises(InterruptedRelease):
            release(retained)
    retained.git.working.pin_ref(
        retained.git.root, ref=retained.admission.pinned_ref, sha=retained.git.target
    )
    assert release(retained)
    assert retained.git.working.retained_refs(retained.git.root) == ()


@pytest.mark.parametrize("missing", ["pin", "artifact", "capture"])
def test_unexplained_missing_content_never_starts_release(retained, missing):
    retained.resolve(State.RECOVERED)
    if missing == "pin":
        retained.git.working.delete_pinned_ref(
            retained.git.root,
            ref=retained.admission.pinned_ref,
            sha=retained.git.target,
        )
    else:
        filename = "capture.json" if missing == "capture" else "validation.json"
        (retained.escrow.root / retained.admission.escrow_dir / filename).unlink()
    before = retained.snapshot()
    with pytest.raises((ValueError, FileNotFoundError)):
        release(retained)
    assert retained.snapshot() == before


def test_missing_future_pin_is_not_explained_by_earlier_intent(retained, monkeypatch):
    retained.resolve(State.RECOVERED)
    with monkeypatch.context() as fault:
        interrupt_deletion(fault, retained, 1, "after")
        with pytest.raises(InterruptedRelease):
            release(retained)
    retained.git.working.delete_pinned_ref(
        retained.git.root, ref=retained.admission.observed_ref, sha=retained.git.tip
    )
    before = retained.snapshot()
    with pytest.raises(ValueError, match="without durable intent"):
        release(retained)
    assert retained.snapshot() == before


@pytest.mark.parametrize("authority", ["unresolved", "claimed", "clock", "resolution"])
def test_release_progress_cannot_override_changed_store_authority(
    retained, monkeypatch, authority
):
    retained.resolve(State.RECOVERED)
    with monkeypatch.context() as fault:
        interrupt_deletion(fault, retained, 1, "after")
        with pytest.raises(InterruptedRelease):
            release(retained)
    if authority == "claimed":
        retained.open().acquire_claim(
            retained.admission.evidence.record_id,
            expected_states=frozenset({State.RECOVERED}),
            evidence_id=retained.admission.evidence.evidence_id,
        )
    else:
        changes = {
            "unresolved": "state='queued'",
            "clock": "terminal_at='!'",
            "resolution": "published_head_sha=''",
        }
        with closing(sqlite3.connect(retained.database)) as conn, conn:
            conn.execute("UPDATE validated_work_records SET " + changes[authority])
    before = retained.snapshot()
    if authority in {"clock", "resolution"}:
        with pytest.raises(ValueError):
            release(retained)
    else:
        assert not release(retained)
    assert retained.snapshot() == before


@pytest.mark.parametrize(
    "corruption", ["checksum", "binding", "intent", "symbolic_pin"]
)
def test_damaged_progress_or_changed_pin_is_retained(
    retained, monkeypatch, tmp_path, corruption
):
    retained.resolve(State.RECOVERED)
    with monkeypatch.context() as fault:
        interrupt_deletion(fault, retained, 1, "after")
        with pytest.raises(InterruptedRelease):
            release(retained)
    receipt = (
        retained.escrow.root
        / ".releases"
        / (retained.admission.evidence.evidence_id + ".json")
    )
    original = ReleaseProgress.decode(receipt.read_bytes())
    if corruption == "checksum":
        receipt.write_bytes(receipt.read_bytes().replace(b'"intent":0', b'"intent":1'))
    elif corruption == "binding":
        other, _ = real_capture(retained.git, tmp_path / "other", run="another-run")
        receipt.write_bytes(ReleaseProgress(other, 0).encode())
    elif corruption == "intent":
        receipt.write_bytes(ReleaseProgress(original.original, 999).encode())
    else:
        retained.git.run(
            "symbolic-ref", retained.admission.pinned_ref, "refs/heads/does-not-exist"
        )
    before = retained.snapshot()
    with pytest.raises(ValueError):
        release(retained)
    assert retained.snapshot() == before


def test_reopened_record_preserves_partial_release_until_resolved_again(
    retained, monkeypatch, tmp_path
):
    retained.resolve(State.ABANDONED)
    with monkeypatch.context() as fault:
        interrupt_deletion(fault, retained, 1, "after")
        with pytest.raises(InterruptedRelease):
            release(retained)
    new, sources = real_capture(
        retained.git, tmp_path / "replacement", run="replacement"
    )
    store = retained.open()
    store.admit(retained.escrow.capture(new, sources))
    before = retained.snapshot()
    assert not release(retained)
    assert retained.snapshot() == before
    store.resolve_observed_merge(
        record_id=new.evidence.record_id,
        merged_head_sha=retained.git.target,
        observed_at=NOW.isoformat(),
    )
    # New terminal_at also restarts the retention clock for every evidence role.
    assert not release(retained)
    with closing(sqlite3.connect(retained.database)) as conn, conn:
        conn.execute(
            "UPDATE validated_work_records SET terminal_at='2026-09-06T13:00:00+00:00'"
        )
    assert release(retained)
    assert retained.escrow.verifies(
        store.evidence_for_id(new.evidence.evidence_id).evidence
    )


def test_refreshed_observations_do_not_rebind_release_pins(retained):
    original = retained.admission
    refreshed = replace(
        original,
        evidence=replace(
            original.evidence,
            observations=replace(
                original.evidence.observations, worktree_head_sha=retained.git.base
            ),
        ),
    )
    store = retained.open()
    store.admit(refreshed)
    assert (
        store.evidence_for_id(
            original.evidence.evidence_id
        ).evidence.admission.evidence.observations.worktree_head_sha
        == retained.git.base
    )
    retained.resolve(State.RECOVERED)
    assert release(retained)
    assert retained.git.working.retained_refs(retained.git.root) == ()
