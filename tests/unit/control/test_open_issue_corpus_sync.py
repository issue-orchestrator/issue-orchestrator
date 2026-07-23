"""Behavior tests for GitHub-to-SQL open-issue corpus synchronization."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.open_issue_corpus import OpenIssueCorpusManager
from issue_orchestrator.control.proposal_dedup_gate import CorpusState
from issue_orchestrator.domain.open_issue_corpus import build_open_issue_fingerprint
from issue_orchestrator.domain.models import Issue
from issue_orchestrator.ports import RepositoryHost
from issue_orchestrator.ports.open_issue_corpus_store import (
    InMemoryOpenIssueCorpusStore,
)

_NOW = datetime(2026, 7, 23, 12, 0, 0, 900_000, tzinfo=timezone.utc)
_LATER = datetime(2026, 7, 23, 12, 5, 0, 900_000, tzinfo=timezone.utc)
_INITIAL_CURSOR = "2026-07-23T11:59:59Z"
_LATER_CURSOR = "2026-07-23T12:04:59Z"


def _issue(
    number: int,
    *,
    title: str,
    body: str = "",
    state: str = "open",
    updated_at: str | None = None,
) -> Issue:
    return Issue(
        number=number,
        title=title,
        body=body,
        labels=[],
        state=state,
        repo="owner/repo",
        updated_at=updated_at,
    )


def test_cold_sync_rebuilds_from_every_open_github_issue() -> None:
    repository_host = MagicMock(spec=RepositoryHost)
    repository_host.list_issues.return_value = [
        _issue(2, title="Second open issue"),
        _issue(1, title="First open issue"),
    ]
    store = InMemoryOpenIssueCorpusStore()
    sync = OpenIssueCorpusManager(
        repository_host,
        store,
        is_enabled=lambda: True,
        clock=lambda: _NOW,
    )

    result = sync.sync()

    assert result is not None
    assert result.mode == "rebuild"
    assert result.upserted == 2
    repository_host.list_issues.assert_called_once_with(
        state="open",
        limit=10_000,
        exhaustive=True,
    )
    repository_host.list_issues_delta.assert_not_called()
    snapshot = store.load()
    assert snapshot is not None
    assert [issue.number for issue in snapshot.issues] == [1, 2]
    # GitHub timestamps are whole seconds and ``since`` is exclusive. The
    # cursor deliberately overlaps the scan-start second so an update reported
    # at 12:00:00Z after its page was read is recovered by the next delta.
    assert snapshot.watermark == _INITIAL_CURSOR


def test_warm_sync_upserts_open_changes_and_evicts_closed_issues() -> None:
    repository_host = MagicMock(spec=RepositoryHost)
    store = InMemoryOpenIssueCorpusStore()
    clock_values = iter((_NOW, _LATER))
    initial_sync = OpenIssueCorpusManager(
        repository_host,
        store,
        is_enabled=lambda: True,
        clock=lambda: next(clock_values),
    )
    repository_host.list_issues.return_value = [
        _issue(1, title="Original title"),
        _issue(2, title="Will close"),
    ]
    initial_sync.sync()
    repository_host.reset_mock()
    repository_host.list_issues_delta.return_value = (
        [
            _issue(1, title="Changed title"),
            _issue(2, title="Will close", state="closed"),
            _issue(3, title="New issue"),
        ],
        "2026-07-23T12:05:00Z",
    )

    result = initial_sync.sync()

    assert result is not None
    assert result.mode == "delta"
    assert result.upserted == 2
    assert result.evicted == 1
    repository_host.list_issues.assert_not_called()
    repository_host.list_issues_delta.assert_called_once_with(
        since=_INITIAL_CURSOR,
        limit=10_000,
    )
    snapshot = store.load()
    assert snapshot is not None
    assert [issue.number for issue in snapshot.issues] == [1, 3]
    assert snapshot.issues[0].title == "changed title"
    assert snapshot.watermark == _LATER_CURSOR


def test_same_second_post_scan_close_is_recovered_by_overlapping_cursor() -> None:
    repository_host = MagicMock(spec=RepositoryHost)
    repository_host.list_issues.return_value = [
        _issue(1, title="Closes during the scan")
    ]
    store = InMemoryOpenIssueCorpusStore()
    manager = OpenIssueCorpusManager(
        repository_host,
        store,
        is_enabled=lambda: True,
        clock=lambda: _NOW,
    )
    manager.sync()
    repository_host.reset_mock()
    repository_host.list_issues_delta.return_value = (
        [
            _issue(
                1,
                title="Closes during the scan",
                state="closed",
                updated_at="2026-07-23T12:00:00Z",
            )
        ],
        "2026-07-23T12:00:00Z",
    )

    manager.sync()

    repository_host.list_issues_delta.assert_called_once_with(
        since=_INITIAL_CURSOR,
        limit=10_000,
    )
    snapshot = store.load()
    assert snapshot is not None
    assert snapshot.entries == ()


def test_disabled_sync_performs_no_github_reads() -> None:
    repository_host = MagicMock(spec=RepositoryHost)
    enabled = False
    sync = OpenIssueCorpusManager(
        repository_host,
        InMemoryOpenIssueCorpusStore(),
        is_enabled=lambda: enabled,
    )

    assert sync.sync() is None
    repository_host.list_issues.assert_not_called()
    repository_host.list_issues_delta.assert_not_called()

    enabled = True
    repository_host.list_issues.return_value = []
    assert sync.sync() is not None
    repository_host.list_issues.assert_called_once()


def test_load_projects_disabled_unavailable_and_ready_corpus_states() -> None:
    enabled = False
    store = InMemoryOpenIssueCorpusStore()
    repository_host = MagicMock(spec=RepositoryHost)
    repository_host.list_issues_delta.return_value = ([], None)
    manager = OpenIssueCorpusManager(
        repository_host,
        store,
        is_enabled=lambda: enabled,
        clock=lambda: _NOW,
    )

    assert manager.load().state is CorpusState.DISABLED
    enabled = True
    assert manager.load().state is CorpusState.UNAVAILABLE

    store.replace_all(
        (
            build_open_issue_fingerprint(
                42,
                "Stabilize flaky CI runner",
                "Runner disconnects mid-build.",
            ),
        ),
        watermark="cursor",
    )
    # A persisted generation is unavailable after process start until an
    # enabled refresh proves it current.
    assert manager.load().state is CorpusState.UNAVAILABLE
    manager.sync()
    corpus = manager.load()
    assert corpus.state is CorpusState.READY
    assert corpus.issues[0].number == 42


def test_failed_refresh_invalidates_ready_corpus_until_retry_succeeds() -> None:
    store = InMemoryOpenIssueCorpusStore()
    store.replace_all(
        (build_open_issue_fingerprint(1, "Original issue", ""),),
        watermark=_INITIAL_CURSOR,
    )
    repository_host = MagicMock(spec=RepositoryHost)
    repository_host.list_issues_delta.side_effect = (
        ([], None),
        RuntimeError("GitHub unavailable"),
        ([_issue(2, title="Recovered issue")], None),
    )
    manager = OpenIssueCorpusManager(
        repository_host,
        store,
        is_enabled=lambda: True,
        clock=lambda: _NOW,
    )

    assert manager.load().state is CorpusState.UNAVAILABLE
    manager.sync()
    assert manager.load().state is CorpusState.READY

    with pytest.raises(RuntimeError, match="GitHub unavailable"):
        manager.sync()
    assert manager.load().state is CorpusState.UNAVAILABLE

    manager.sync()
    corpus = manager.load()
    assert corpus.state is CorpusState.READY
    assert {issue.number for issue in corpus.issues} == {1, 2}


def test_delta_cap_rebuilds_without_committing_partial_cursor() -> None:
    store = InMemoryOpenIssueCorpusStore()
    store.replace_all(
        (build_open_issue_fingerprint(1, "Old issue", ""),),
        watermark=_INITIAL_CURSOR,
    )
    repository_host = MagicMock(spec=RepositoryHost)
    repository_host.list_issues_delta.return_value = (
        [_issue(number, title=f"Partial {number}") for number in range(1, 10_001)],
        "partial-cursor-must-not-be-committed",
    )
    repository_host.list_issues.return_value = [
        _issue(999, title="Authoritative rebuild")
    ]
    manager = OpenIssueCorpusManager(
        repository_host,
        store,
        is_enabled=lambda: True,
        clock=lambda: _NOW,
    )

    result = manager.sync()

    assert result is not None
    assert result.mode == "rebuild"
    repository_host.list_issues.assert_called_once_with(
        state="open",
        limit=10_000,
        exhaustive=True,
    )
    snapshot = store.load()
    assert snapshot is not None
    assert [issue.number for issue in snapshot.issues] == [999]
    assert snapshot.watermark == _INITIAL_CURSOR
    assert snapshot.watermark != "partial-cursor-must-not-be-committed"
