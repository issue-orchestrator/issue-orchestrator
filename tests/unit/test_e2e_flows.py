"""Unit tests for E2E flow helpers."""

import pytest
from unittest.mock import AsyncMock, Mock

from issue_orchestrator.events import EventName
from tests.e2e import flows
from tests.e2e.fixtures import artifact_cleanup
from tests.e2e.fixtures.artifact_cleanup import CleanupRefusedError

from .e2e_github_fake import FakeE2EGitHub, issue, pull_request


def test_flow_create_issue_includes_filter_label(monkeypatch):
    """Ensure flow adds filter label automatically."""
    called = {}

    def fake_create(repo, title, labels, body=None, on_created=None, ensure_labels=True):
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

    def fake_create(repo, title, labels, body=None, on_created=None, ensure_labels=True):
        called["labels"] = labels
        # Returns tuple of (IssueKey, issue_number)
        return Mock(stable_id=lambda: "123", scope=lambda: repo), 456

    monkeypatch.setattr(flows, "inflight_create", fake_create)

    flow = flows.E2EFlow(repo="owner/repo", watcher=None, filter_label="test-data")
    flow.create_issue("Test issue", ["test-data", "agent:e2e-test"])

    assert called["labels"] == ["test-data", "agent:e2e-test"]


TEST_REPO = "owner/e2e-test-repo"


def _repo_with_real_reviewed_work() -> FakeE2EGitHub:
    """The run creates issue #123; everything else is real work under review,
    or a concurrent run's artifact."""
    return FakeE2EGitHub(
        [
            pull_request(10, "123-e2e-artifact", ["io-e2e-test-data", "needs-code-review"], body="Closes #123"),
            pull_request(9483, "9483-tech-lead-work", ["needs-code-review"], body="Closes #9483"),
            pull_request(9501, "9501-tech-lead-work", ["code-reviewed"], body="Closes #9501"),
            # Real work that even names the run's issue number: only an e2e label
            # may put a PR in scope, never a workflow label.
            pull_request(9296, "123-unrelated-work", ["code-reviewed"], body="Refs #123"),
            # A concurrent run's e2e PR.
            pull_request(11, "456-e2e-artifact", ["io-e2e-test-data"], body="Closes #456"),
        ],
        issues=[
            issue(123, ["agent:e2e-test", "io-e2e-test-data"]),
            issue(456, ["agent:e2e-test", "io-e2e-test-data"]),
            issue(9483, ["agent:coder"]),
        ],
    )


@pytest.fixture
def github(monkeypatch, live_e2e_run) -> FakeE2EGitHub:
    live_e2e_run(TEST_REPO)
    repository = _repo_with_real_reviewed_work()
    monkeypatch.setattr(artifact_cleanup, "_github_adapter", lambda _repo: repository)
    return repository


def _flow_that_created_issue_123(monkeypatch, labels: list[str]) -> flows.E2EFlow:
    def fake_create(repo, title, labels, body=None, on_created=None, ensure_labels=True):
        return Mock(stable_id=lambda: "123", scope=lambda: repo), 123

    monkeypatch.setattr(flows, "inflight_create", fake_create)
    flow = flows.E2EFlow(repo=TEST_REPO, watcher=None, filter_label="io-e2e-test-data")
    flow.create_issue("Test issue", labels)
    return flow


#: The labels the deleted ``TestCleanup`` swept every open PR by (#9523): one
#: e2e marker and two production workflow labels.
_SWEPT_LABELS = ["agent:e2e-test", "needs-code-review", "code-reviewed"]


def test_flow_cleanup_never_closes_real_prs_carrying_review_labels(monkeypatch, github):
    """#9523: cleanup by workflow label closed 17 real PRs and deleted their branches."""
    flow = _flow_that_created_issue_123(monkeypatch, _SWEPT_LABELS)

    flow.cleanup_created_issues()

    assert github.writes == ([10], ["123-e2e-artifact"])
    assert github.closed_issues == [123]


@pytest.mark.parametrize("pr_number", [9483, 9501, 9296, 11, 424242])
def test_flow_close_pr_refuses_a_pr_of_an_issue_it_did_not_create(monkeypatch, github, pr_number):
    flow = _flow_that_created_issue_123(monkeypatch, ["agent:e2e-test"])

    with pytest.raises(CleanupRefusedError):
        flow.close_pr(pr_number)

    assert github.writes == ([], [])


def test_flow_close_pr_closes_its_own_pr_and_deletes_the_branch(monkeypatch, github):
    flow = _flow_that_created_issue_123(monkeypatch, ["agent:e2e-test"])

    flow.close_pr(10)

    assert github.writes == ([10], ["123-e2e-artifact"])


@pytest.mark.parametrize("issue_number", [456, 9483, 424242])
def test_flow_close_issue_refuses_an_issue_it_did_not_create(monkeypatch, github, issue_number):
    flow = _flow_that_created_issue_123(monkeypatch, ["agent:e2e-test"])

    with pytest.raises(CleanupRefusedError):
        flow.close_issue(issue_number, "done")

    assert github.all_writes["closed_issues"] == [] and github.comments == []


def test_flow_close_issue_comments_then_closes_its_own(monkeypatch, github):
    flow = _flow_that_created_issue_123(monkeypatch, ["agent:e2e-test"])

    flow.close_issue(123, "done")

    assert github.comments == [(123, "done")] and github.closed_issues == [123]


def test_a_flow_that_created_nothing_cleans_up_nothing(github):
    flow = flows.E2EFlow(repo=TEST_REPO, watcher=None, filter_label="io-e2e-test-data")

    assert flow.cleanup_created_prs() == 0
    flow.cleanup_created_issues()
    assert github.writes == ([], []) and github.closed_issues == []


def test_flow_cleanup_closes_prs_before_issues(monkeypatch, github):
    calls: list[tuple[str, object]] = []
    close_pr = github.close_pr
    update_issue_state = github.update_issue_state

    def recording_close_pr(pr_number):
        calls.append(("pr", pr_number))
        close_pr(pr_number)

    def recording_update_issue_state(issue_number, state):
        calls.append(("issue", issue_number))
        update_issue_state(issue_number, state)

    monkeypatch.setattr(github, "close_pr", recording_close_pr)
    monkeypatch.setattr(github, "update_issue_state", recording_update_issue_state)
    flow = _flow_that_created_issue_123(monkeypatch, ["agent:e2e-test"])

    flow.cleanup_created_issues()

    assert calls == [("pr", 10), ("issue", 123)]


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
