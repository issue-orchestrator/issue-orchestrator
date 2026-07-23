"""Contract tests for the rebuildable open-issue fingerprint cache."""

from __future__ import annotations

from pathlib import Path

import pytest

from issue_orchestrator.domain.open_issue_corpus import (
    OpenIssueFingerprint,
    build_open_issue_fingerprint,
)
from issue_orchestrator.infra.open_issue_corpus_store import (
    SqliteOpenIssueCorpusStore,
)
from issue_orchestrator.infra.repo_identity import state_dir
from issue_orchestrator.ports.open_issue_corpus_store import (
    InMemoryOpenIssueCorpusStore,
    OpenIssueCorpusStore,
)


@pytest.fixture(params=("sqlite", "memory"))
def store(request: pytest.FixtureRequest, tmp_path: Path) -> OpenIssueCorpusStore:
    if request.param == "sqlite":
        return SqliteOpenIssueCorpusStore.for_repo(tmp_path)
    return InMemoryOpenIssueCorpusStore()


def _entry(number: int, title: str, body: str = "") -> OpenIssueFingerprint:
    return build_open_issue_fingerprint(number, title, body)


def test_uninitialized_store_has_no_ready_generation(
    store: OpenIssueCorpusStore,
) -> None:
    assert store.load() is None


def test_full_build_normalizes_and_fingerprints_open_issues(
    store: OpenIssueCorpusStore,
) -> None:
    store.replace_all(
        (
            _entry(
                42,
                "Fix THE flaky runner",
                "## Problem\nThe runner disconnects during builds.",
            ),
        ),
        watermark="2026-07-23T12:00:00Z",
    )

    snapshot = store.load()

    assert snapshot is not None
    assert snapshot.watermark == "2026-07-23T12:00:00Z"
    [entry] = snapshot.entries
    assert entry.issue.number == 42
    assert entry.issue.title == "fix flaky runner"
    assert entry.issue.body == "runner disconnects during builds"
    assert len(entry.content_fingerprint) == 64


def test_delta_upserts_changed_content_and_evicts_closed_issue(
    store: OpenIssueCorpusStore,
) -> None:
    original = _entry(10, "Old title", "Old body")
    evicted = _entry(20, "Closed later")
    store.replace_all(
        (original, evicted),
        watermark="2026-07-23T12:00:00Z",
    )
    changed = _entry(10, "New title", "New body")

    store.apply_delta(
        (changed, _entry(30, "Newly opened")),
        evict_issue_numbers=(20,),
        watermark="2026-07-23T12:05:00Z",
    )

    snapshot = store.load()
    assert snapshot is not None
    assert snapshot.watermark == "2026-07-23T12:05:00Z"
    assert [entry.issue.number for entry in snapshot.entries] == [10, 30]
    assert snapshot.entries[0] == changed
    assert changed.content_fingerprint != original.content_fingerprint


def test_conflicting_delta_is_rejected_without_partial_mutation(
    store: OpenIssueCorpusStore,
) -> None:
    original = _entry(10, "Original")
    store.replace_all((original,), watermark="cursor-1")

    with pytest.raises(ValueError, match="both upserts and evicts"):
        store.apply_delta(
            (_entry(10, "Changed"),),
            evict_issue_numbers=(10,),
            watermark="cursor-2",
        )

    snapshot = store.load()
    assert snapshot is not None
    assert snapshot.entries == (original,)
    assert snapshot.watermark == "cursor-1"


def test_invalid_delta_watermark_is_rejected_without_partial_mutation(
    store: OpenIssueCorpusStore,
) -> None:
    store.replace_all((_entry(1, "Original"),), watermark="cursor-1")
    before = store.load()

    with pytest.raises(ValueError, match="watermark"):
        store.apply_delta(
            (_entry(2, "New"),),
            evict_issue_numbers=(1,),
            watermark="",
        )

    assert store.load() == before


def test_sqlite_store_survives_reopen(tmp_path: Path) -> None:
    SqliteOpenIssueCorpusStore.for_repo(tmp_path).replace_all(
        (_entry(7, "Persistent corpus row"),),
        watermark="cursor",
    )

    snapshot = SqliteOpenIssueCorpusStore.for_repo(tmp_path).load()

    assert snapshot is not None
    assert snapshot.issues[0].number == 7
    assert (state_dir(tmp_path) / "open_issue_corpus.sqlite").exists()
