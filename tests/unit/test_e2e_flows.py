"""Unit tests for E2E flow helpers."""

import pytest
from unittest.mock import AsyncMock, Mock

from issue_orchestrator.events import EventName
from issue_orchestrator.ports.pull_request_tracker import PRInfo
from tests.e2e import flows


def test_flow_create_issue_includes_filter_label(monkeypatch):
    """Ensure flow adds filter label automatically."""
    called = {}

    def fake_create(repo, title, labels, body=None):
        called["repo"] = repo
        called["title"] = title
        called["labels"] = labels
        called["body"] = body
        # Returns tuple of (IssueKey, issue_number)
        return Mock(stable_id=lambda: "123", scope=lambda: repo), 123

    monkeypatch.setattr(flows, "inflight_create", fake_create)

    flow = flows.E2EFlow(repo="owner/repo", watcher=None, filter_label="test-data")
    issue, issue_num = flow.create_issue("Test issue", ["agent:e2e-test"])

    assert issue is not None
    assert issue_num == 123
    assert called["labels"] == ["agent:e2e-test", "test-data"]


def test_flow_create_issue_no_duplicate_filter_label(monkeypatch):
    """Avoid adding filter label twice."""
    called = {}

    def fake_create(repo, title, labels, body=None):
        called["labels"] = labels
        # Returns tuple of (IssueKey, issue_number)
        return Mock(stable_id=lambda: "123", scope=lambda: repo), 456

    monkeypatch.setattr(flows, "inflight_create", fake_create)

    flow = flows.E2EFlow(repo="owner/repo", watcher=None, filter_label="test-data")
    flow.create_issue("Test issue", ["test-data", "agent:e2e-test"])

    assert called["labels"] == ["test-data", "agent:e2e-test"]


def test_cleanup_test_prs_for_issues_closes_only_matching_e2e_prs(monkeypatch):
    matching = PRInfo(
        number=10,
        title="#123: Test artifact",
        url="https://example.test/pull/10",
        branch="123-test-artifact",
        body="Closes #123",
        state="open",
        labels=["io-e2e-test-data"],
    )
    unrelated = PRInfo(
        number=11,
        title="#999: Other artifact",
        url="https://example.test/pull/11",
        branch="999-other-artifact",
        body="Closes #999",
        state="open",
        labels=["io-e2e-test-data"],
    )
    adapter = Mock()
    adapter.get_prs_with_label.return_value = [matching, unrelated]
    monkeypatch.setattr(flows, "_github_adapter", lambda repo: adapter)

    closed = flows.cleanup_test_prs_for_issues(
        "owner/repo",
        [123],
        ["io-e2e-test-data", "agent:e2e-test"],
    )

    assert closed == 1
    adapter.get_prs_with_label.assert_called_once_with("io-e2e-test-data", state="open")
    adapter.close_pr.assert_called_once_with(10)
    adapter.delete_branch.assert_called_once_with("123-test-artifact")


def test_flow_cleanup_closes_prs_before_issues(monkeypatch):
    calls: list[tuple[str, object]] = []

    def fake_create(repo, title, labels, body=None):
        return Mock(stable_id=lambda: "123", scope=lambda: repo), 123

    def fake_cleanup_prs(repo, issue_numbers, labels):
        calls.append(("prs", (repo, tuple(issue_numbers), tuple(sorted(labels)))))
        return 1

    def fake_close_issue(repo, issue_number):
        calls.append(("issue", issue_number))

    monkeypatch.setattr(flows, "inflight_create", fake_create)
    monkeypatch.setattr(flows, "cleanup_test_prs_for_issues", fake_cleanup_prs)

    import issue_orchestrator.testing.support.test_data as test_data
    monkeypatch.setattr(test_data, "close_issue", fake_close_issue)

    flow = flows.E2EFlow(repo="owner/repo", watcher=None, filter_label="io-e2e-test-data")
    flow.create_issue("Test issue", ["agent:e2e-test"])
    flow.cleanup_created_issues()

    assert calls == [
        ("prs", ("owner/repo", (123,), ("agent:e2e-test", "io-e2e-test-data"))),
        ("issue", 123),
    ]


def test_flow_update_issue_calls_inflight_update(monkeypatch):
    """Ensure update_issue delegates with control API port derived from watcher."""
    called = {}

    def fake_update(issue, add_labels=None, remove_labels=None, port=None):
        called["issue"] = issue
        called["add_labels"] = add_labels
        called["remove_labels"] = remove_labels
        called["port"] = port

    monkeypatch.setattr(flows, "inflight_update", fake_update)

    # Mock watcher with snapshot provider URL containing port
    mock_watcher = Mock()
    # noqa: SLF001 - Mock must match internal watcher structure for port extraction test
    mock_watcher._snapshot_provider = Mock(url="http://localhost:19080/api/snapshot")  # noqa: SLF001

    issue = Mock(stable_id=lambda: "123")
    flow = flows.E2EFlow(repo="owner/repo", watcher=mock_watcher)
    flow.update_issue(issue, add_labels=["blocked"], remove_labels=["in-progress"])

    assert called["issue"] is issue
    assert called["add_labels"] == ["blocked"]
    assert called["remove_labels"] == ["in-progress"]
    assert called["port"] == 19080


def test_review_timeout_from_config_uses_agent_timeouts():
    """Compute review timeout from agent timeouts."""
    config = Mock()
    config.agents = {
        "agent:e2e-test": Mock(timeout_minutes=2),
        "agent:script-review": Mock(timeout_minutes=3),
    }
    config.code_review_agent = None

    timeout_s = flows.review_timeout_from_config(config)
    assert timeout_s == 300.0


def test_review_timeout_from_config_falls_back_on_error():
    """Use default when config is incomplete."""
    config = Mock()
    config.agents = {}
    config.code_review_agent = None

    timeout_s = flows.review_timeout_from_config(config, default_s=180.0)
    assert timeout_s == 180.0


@pytest.mark.asyncio
async def test_flow_issue_seen_requires_watcher():
    """Ensure watcher is required for watch operations."""
    flow = flows.E2EFlow(repo="owner/repo", watcher=None)
    issue = Mock(stable_id=lambda: "123")
    with pytest.raises(RuntimeError):
        await flow.issue_seen(issue, timeout_s=1)


@pytest.mark.asyncio
async def test_flow_issue_event_delegates_to_issue_watch():
    """Ensure issue_event delegates to IssueWatch.event with stable ID."""
    issue_watch = Mock()
    issue_watch.event = AsyncMock()
    mock_watcher = Mock()
    mock_watcher.issue.return_value = issue_watch
    mock_watcher._snapshot_provider = Mock(url="http://localhost:19080/api/snapshot")  # noqa: SLF001

    flow = flows.E2EFlow(repo="owner/repo", watcher=mock_watcher)
    issue = Mock(stable_id=lambda: "123")

    await flow.issue_event(
        issue,
        EventName.ISSUE_LABELS_CHANGED,
        predicate=lambda e: "in-progress" in e.get("payload", {}).get("labels", []),
        timeout_s=12.0,
    )

    mock_watcher.issue.assert_called_once_with("123")
    issue_watch.event.assert_awaited_once()
    args, kwargs = issue_watch.event.call_args
    assert args[0] == EventName.ISSUE_LABELS_CHANGED
    assert kwargs["timeout_s"] == 12.0
    assert callable(kwargs["predicate"])
