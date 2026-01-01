"""Unit tests for E2E flow helpers."""

import pytest
from unittest.mock import Mock

from tests.e2e import flows


def test_flow_create_issue_includes_filter_label(monkeypatch):
    """Ensure flow adds filter label automatically."""
    called = {}

    def fake_create(repo, title, labels, body=None):
        called["repo"] = repo
        called["title"] = title
        called["labels"] = labels
        called["body"] = body
        return Mock(stable_id=lambda: "123", scope=lambda: repo)

    monkeypatch.setattr(flows, "inflight_create", fake_create)

    flow = flows.E2EFlow(repo="owner/repo", watcher=None, filter_label="test-data")
    issue = flow.create_issue("Test issue", ["agent:e2e-test"])

    assert issue is not None
    assert called["labels"] == ["agent:e2e-test", "test-data"]


def test_flow_create_issue_no_duplicate_filter_label(monkeypatch):
    """Avoid adding filter label twice."""
    called = {}

    def fake_create(repo, title, labels, body=None):
        called["labels"] = labels
        return Mock(stable_id=lambda: "123", scope=lambda: repo)

    monkeypatch.setattr(flows, "inflight_create", fake_create)

    flow = flows.E2EFlow(repo="owner/repo", watcher=None, filter_label="test-data")
    flow.create_issue("Test issue", ["test-data", "agent:e2e-test"])

    assert called["labels"] == ["test-data", "agent:e2e-test"]


def test_flow_update_issue_calls_inflight_update(monkeypatch):
    """Ensure update_issue delegates with control API port."""
    called = {}

    def fake_update(issue, add_labels=None, remove_labels=None, port=None):
        called["issue"] = issue
        called["add_labels"] = add_labels
        called["remove_labels"] = remove_labels
        called["port"] = port

    monkeypatch.setattr(flows, "inflight_update", fake_update)

    issue = Mock(stable_id=lambda: "123")
    flow = flows.E2EFlow(repo="owner/repo", watcher=None, control_api_port=19080)
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
