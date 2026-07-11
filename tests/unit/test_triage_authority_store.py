"""Contract tests for the SQLite triage launch-authority adapter (#6761 rr F1)."""

from pathlib import Path

import pytest

from issue_orchestrator.domain.triage_session import (
    TriageLaunchAuthority,
    TriageSessionFlavor,
)
from issue_orchestrator.infra.repo_identity import state_dir
from issue_orchestrator.ports.triage_authority import (
    InMemoryTriageAuthorityStore,
    TriageAuthorityConflictError,
)
from issue_orchestrator.infra.triage_authority_store import (
    SqliteTriageAuthorityStore,
)


def _batch(prs: tuple[int, ...] = (101, 102)) -> TriageLaunchAuthority:
    return TriageLaunchAuthority(
        flavor=TriageSessionFlavor.BATCH_REVIEW,
        anchor_issue_number=7,
        manifest_pr_numbers=prs,
    )


def test_round_trip_keyed_by_run_identity(tmp_path: Path) -> None:
    store = SqliteTriageAuthorityStore.for_repo(tmp_path)
    store.record(run_id="r1", session_name="issue-7", authority=_batch())

    loaded = store.load(run_id="r1", session_name="issue-7")

    assert loaded == _batch()
    # Other runs of the same session (and other sessions) see nothing.
    assert store.load(run_id="r2", session_name="issue-7") is None
    assert store.load(run_id="r1", session_name="issue-8") is None


@pytest.mark.parametrize("make_store", [
    lambda tmp_path: SqliteTriageAuthorityStore.for_repo(tmp_path),
    lambda _tmp_path: InMemoryTriageAuthorityStore(),
])
def test_record_identical_payload_is_noop(tmp_path: Path, make_store) -> None:
    """Create-once: re-recording the same payload is silently accepted."""
    store = make_store(tmp_path)
    store.record(run_id="r1", session_name="issue-7", authority=_batch((1,)))
    store.record(run_id="r1", session_name="issue-7", authority=_batch((1,)))

    loaded = store.load(run_id="r1", session_name="issue-7")

    assert loaded is not None
    assert loaded.manifest_pr_numbers == (1,)


@pytest.mark.parametrize("make_store", [
    lambda tmp_path: SqliteTriageAuthorityStore.for_repo(tmp_path),
    lambda _tmp_path: InMemoryTriageAuthorityStore(),
])
def test_record_conflicting_payload_fails_loudly(tmp_path: Path, make_store) -> None:
    """The authority constrains mutation scope: it must never silently
    change or expand for an existing (run_id, session_name) (#6769 r4)."""
    store = make_store(tmp_path)
    store.record(run_id="r1", session_name="issue-7", authority=_batch((1,)))

    with pytest.raises(TriageAuthorityConflictError):
        store.record(run_id="r1", session_name="issue-7", authority=_batch((2, 3)))

    loaded = store.load(run_id="r1", session_name="issue-7")
    assert loaded is not None
    assert loaded.manifest_pr_numbers == (1,)


def test_store_lives_in_orchestrator_state_dir(tmp_path: Path) -> None:
    """The record must live OUTSIDE any agent-writable worktree."""
    SqliteTriageAuthorityStore.for_repo(tmp_path).record(
        run_id="r1", session_name="issue-7", authority=_batch()
    )
    assert (state_dir(tmp_path) / "triage_authority.sqlite").exists()


def test_survives_reopen(tmp_path: Path) -> None:
    """A restart constructs a fresh handle over the same durable file."""
    SqliteTriageAuthorityStore.for_repo(tmp_path).record(
        run_id="r1", session_name="issue-7", authority=_batch()
    )

    reopened = SqliteTriageAuthorityStore.for_repo(tmp_path)

    assert reopened.load(run_id="r1", session_name="issue-7") == _batch()


def test_investigation_authority_requires_focus() -> None:
    with pytest.raises(ValueError, match="focus_issue_number"):
        TriageLaunchAuthority(
            flavor=TriageSessionFlavor.FAILURE_INVESTIGATION,
            anchor_issue_number=7,
        )


def test_allowed_targets_by_flavor() -> None:
    investigation = TriageLaunchAuthority(
        flavor=TriageSessionFlavor.FAILURE_INVESTIGATION,
        anchor_issue_number=7,
        focus_issue_number=7,
    )
    assert investigation.allowed_targets() == frozenset({7})
    assert _batch().allowed_targets() == frozenset({7, 101, 102})


def test_discard_removes_only_the_named_run(tmp_path: Path) -> None:
    """Retention (#6769 F3): discard drops one run's row and nothing else."""
    store = SqliteTriageAuthorityStore.for_repo(tmp_path)
    store.record(run_id="r1", session_name="issue-7", authority=_batch())
    store.record(run_id="r2", session_name="issue-7", authority=_batch((5,)))

    store.discard(run_id="r1", session_name="issue-7")

    assert store.load(run_id="r1", session_name="issue-7") is None
    assert store.load(run_id="r2", session_name="issue-7") == _batch((5,))


def test_discard_is_a_noop_when_absent(tmp_path: Path) -> None:
    store = SqliteTriageAuthorityStore.for_repo(tmp_path)
    store.discard(run_id="never-recorded", session_name="issue-7")
    assert store.load(run_id="never-recorded", session_name="issue-7") is None


def test_sqlite_adapter_satisfies_the_port() -> None:
    """The adapter must implement every method the port declares."""
    from issue_orchestrator.ports.triage_authority import (
        TriageAuthorityStore as TriageAuthorityStorePort,
    )

    for method in ("record", "load", "discard"):
        assert callable(getattr(SqliteTriageAuthorityStore, method))
        assert callable(getattr(TriageAuthorityStorePort, method))
