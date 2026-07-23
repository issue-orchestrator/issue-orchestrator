"""Behavior tests for GitHub-to-SQL open-issue corpus synchronization."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

from issue_orchestrator.control.open_issue_corpus import OpenIssueCorpusManager
from issue_orchestrator.control.proposal_dedup_gate import CorpusState
from issue_orchestrator.domain.open_issue_corpus import build_open_issue_fingerprint
from issue_orchestrator.domain.models import Issue
from issue_orchestrator.ports import RepositoryHost
from issue_orchestrator.ports.open_issue_corpus_store import (
    InMemoryOpenIssueCorpusStore,
)

_NOW = datetime(2026, 7, 23, 12, 0, tzinfo=timezone.utc)


def _issue(
    number: int,
    *,
    title: str,
    body: str = "",
    state: str = "open",
) -> Issue:
    return Issue(
        number=number,
        title=title,
        body=body,
        labels=[],
        state=state,
        repo="owner/repo",
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
    assert snapshot.watermark == _NOW.isoformat()


def test_warm_sync_upserts_open_changes_and_evicts_closed_issues() -> None:
    repository_host = MagicMock(spec=RepositoryHost)
    store = InMemoryOpenIssueCorpusStore()
    initial_sync = OpenIssueCorpusManager(
        repository_host,
        store,
        is_enabled=lambda: True,
        clock=lambda: _NOW,
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
        since=_NOW.isoformat(),
        limit=10_000,
    )
    snapshot = store.load()
    assert snapshot is not None
    assert [issue.number for issue in snapshot.issues] == [1, 3]
    assert snapshot.issues[0].title == "changed title"
    assert snapshot.watermark == "2026-07-23T12:05:00Z"


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
    manager = OpenIssueCorpusManager(
        MagicMock(spec=RepositoryHost),
        store,
        is_enabled=lambda: enabled,
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
    corpus = manager.load()
    assert corpus.state is CorpusState.READY
    assert corpus.issues[0].number == 42
