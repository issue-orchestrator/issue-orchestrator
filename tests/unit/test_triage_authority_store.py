"""Tests for the orchestrator-owned triage launch-authority store (#6761 rr F1)."""

from pathlib import Path

import pytest

from issue_orchestrator.domain.triage_session import (
    TriageLaunchAuthority,
    TriageSessionFlavor,
)
from issue_orchestrator.infra.repo_identity import state_dir
from issue_orchestrator.infra.triage_authority_store import TriageAuthorityStore


def _batch(prs: tuple[int, ...] = (101, 102)) -> TriageLaunchAuthority:
    return TriageLaunchAuthority(
        flavor=TriageSessionFlavor.BATCH_REVIEW,
        anchor_issue_number=7,
        manifest_pr_numbers=prs,
    )


def test_round_trip_keyed_by_run_identity(tmp_path: Path) -> None:
    store = TriageAuthorityStore.for_repo(tmp_path)
    store.record(run_id="r1", session_name="issue-7", authority=_batch())

    loaded = store.load(run_id="r1", session_name="issue-7")

    assert loaded == _batch()
    # Other runs of the same session (and other sessions) see nothing.
    assert store.load(run_id="r2", session_name="issue-7") is None
    assert store.load(run_id="r1", session_name="issue-8") is None


def test_record_is_idempotent_and_last_write_wins(tmp_path: Path) -> None:
    store = TriageAuthorityStore.for_repo(tmp_path)
    store.record(run_id="r1", session_name="issue-7", authority=_batch((1,)))
    store.record(run_id="r1", session_name="issue-7", authority=_batch((2, 3)))

    loaded = store.load(run_id="r1", session_name="issue-7")

    assert loaded is not None
    assert loaded.manifest_pr_numbers == (2, 3)


def test_store_lives_in_orchestrator_state_dir(tmp_path: Path) -> None:
    """The record must live OUTSIDE any agent-writable worktree."""
    TriageAuthorityStore.for_repo(tmp_path).record(
        run_id="r1", session_name="issue-7", authority=_batch()
    )
    assert (state_dir(tmp_path) / "triage_authority.sqlite").exists()


def test_survives_reopen(tmp_path: Path) -> None:
    """A restart constructs a fresh handle over the same durable file."""
    TriageAuthorityStore.for_repo(tmp_path).record(
        run_id="r1", session_name="issue-7", authority=_batch()
    )

    reopened = TriageAuthorityStore.for_repo(tmp_path)

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
