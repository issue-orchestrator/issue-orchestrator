"""Recovery issue reads never reuse cached state or interpret unknown as open."""

import httpx
import pytest

from issue_orchestrator.adapters.github.http_client import GitHubHttpClient, GitHubHttpConfig
from issue_orchestrator.adapters.github.recovery_issue_reader import GitHubRecoveryIssueReader
from issue_orchestrator.domain.recovery_entry import RecoveryIssueState
from issue_orchestrator.ports.recovery_issue_reader import RecoveryIssueReadError


def payload():
    return {"number": 42, "title": "Retained feature", "state": "open", "labels": [{"name": "recovery-pending"}]}


@pytest.fixture
def reader_factory(monkeypatch):
    clients = []
    original = httpx.Client
    def create(handler):
        monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
        client = GitHubHttpClient(GitHubHttpConfig(repo="owner/repo", token="test"))
        clients.append(client)
        return GitHubRecoveryIssueReader(client, repo_slug="owner/repo")
    yield create
    for client in clients:
        client.close()


def test_issue_closure_is_observed_without_etag_cache(reader_factory):
    calls = []
    def respond(request):
        calls.append(request)
        raw = payload()
        raw["state"] = "open" if len(calls) == 1 else "closed"
        return httpx.Response(200, json=raw, headers={"ETag": '"same"'})
    reader = reader_factory(respond)
    assert reader.read("owner/repo", 42).state is RecoveryIssueState.OPEN
    assert reader.read("owner/repo", 42).state is RecoveryIssueState.CLOSED
    assert len(calls) == 2
    assert all("if-none-match" not in request.headers for request in calls)
    assert all(request.url.path == "/repos/owner/repo/issues/42" for request in calls)


@pytest.mark.parametrize("status", [401, 404])
def test_failed_or_missing_issue_is_unreadable(reader_factory, status):
    reader = reader_factory(lambda _: httpx.Response(status, json={"message": "unavailable"}))
    with pytest.raises(RecoveryIssueReadError):
        reader.read("owner/repo", 42)


@pytest.mark.parametrize("kind", ["absent", "wrong-number", "pr", "state", "labels", "label-name"])
def test_incomplete_or_different_identity_is_unreadable(reader_factory, kind):
    raw = payload()
    if kind == "absent":
        raw = None
    else:
        key, value = {"wrong-number": ("number", 99), "pr": ("pull_request", {}),
            "state": ("state", "unknown"), "labels": ("labels", "label"),
            "label-name": ("labels", [{"name": None}])}[kind]
        raw[key] = value
    reader = reader_factory(lambda _: httpx.Response(200, json=raw))
    with pytest.raises(RecoveryIssueReadError):
        reader.read("owner/repo", 42)


def test_repository_mismatch_makes_no_request(reader_factory):
    def unexpected(request):
        pytest.fail("repository mismatch must not reach transport")
    reader = reader_factory(unexpected)
    with pytest.raises(RecoveryIssueReadError):
        reader.read("other/repo", 42)
