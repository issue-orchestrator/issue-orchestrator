"""Authoritative HTTP publication reads reject ambiguous and incomplete replies."""

from dataclasses import replace
from pathlib import Path

import httpx
import pytest

from issue_orchestrator.adapters.github.http_client import (
    GitHubHttpClient,
    GitHubHttpConfig,
)
from issue_orchestrator.adapters.github.publication_remote import (
    GitHubPublicationRemote,
)
from issue_orchestrator.domain.publication_remote import (
    PublicationPrState,
    PublicationRemoteError,
)
from issue_orchestrator.domain.validated_head_publication import (
    PublishValidatedHeadCommand,
    RemoteHeadExpectation,
)

SHA = "a" * 40
COMMAND = PublishValidatedHeadCommand(
    1,
    "owner/repo",
    "feature",
    SHA,
    RemoteHeadExpectation.ABSENT,
    None,
    Path("/repo"),
    None,
    "main",
)


def pr_payload():
    return {
        "number": 2,
        "html_url": "https://github.com/owner/repo/pull/2",
        "state": "open",
        "body": "body",
        "head": {"repo": {"full_name": "owner/repo"}, "ref": "feature", "sha": SHA},
        "base": {"repo": {"full_name": "owner/repo"}, "ref": "main"},
    }


@pytest.fixture
def remote_factory(monkeypatch):
    clients = []
    original = httpx.Client

    def make(handler, *, api_url="https://api.github.com"):
        monkeypatch.setattr(
            httpx,
            "Client",
            lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs),
        )
        client = GitHubHttpClient(
            GitHubHttpConfig(repo="owner/repo", token="test", base_url=api_url)
        )
        clients.append(client)
        return GitHubPublicationRemote(client, repo_slug="owner/repo")

    yield make
    for client in clients:
        client.close()


def test_all_reads_bypass_etag_cache(remote_factory):
    requests = []

    def handler(request):
        requests.append(request)
        if "/git/ref/" in request.url.path:
            payload = {
                "ref": "refs/heads/feature",
                "object": {"type": "commit", "sha": SHA},
            }
        elif request.url.path.endswith("/pulls"):
            payload = [pr_payload()]
        else:
            payload = pr_payload()
        return httpx.Response(200, json=payload, headers={"ETag": '"same"'})

    remote = remote_factory(handler)
    for _ in range(2):
        assert remote.read_branch(COMMAND) == SHA
        assert remote.read_pr(COMMAND, 2).head_sha == SHA
        assert len(remote.list_prs(COMMAND)) == 1
    assert len(requests) == 6
    assert all("if-none-match" not in request.headers for request in requests)


@pytest.mark.parametrize("method", ["read_branch", "read_pr", "list_prs"])
@pytest.mark.parametrize("payload", [None, "wrong", 42])
def test_malformed_payload_is_not_absence(remote_factory, method, payload):
    remote = remote_factory(lambda _: httpx.Response(200, json=payload))
    with pytest.raises(PublicationRemoteError):
        getattr(remote, method)(COMMAND, *([2] if method == "read_pr" else []))


@pytest.mark.parametrize("method", ["read_branch", "read_pr", "list_prs"])
def test_auth_failure_is_not_absence(remote_factory, method):
    remote = remote_factory(
        lambda _: httpx.Response(401, json={"message": "bad token"})
    )
    with pytest.raises(PublicationRemoteError):
        getattr(remote, method)(COMMAND, *([2] if method == "read_pr" else []))


def test_explicit_not_found_means_absence(remote_factory):
    remote = remote_factory(
        lambda _: httpx.Response(404, json={"message": "not found"})
    )
    assert remote.read_branch(COMMAND) is None
    assert remote.read_pr(COMMAND, 2) is None
    with pytest.raises(PublicationRemoteError):
        remote.list_prs(COMMAND)


def test_merged_rest_closed_is_merged(remote_factory):
    raw = pr_payload()
    raw.update(state="closed", merged_at="2026-09-01T00:00:00Z")
    remote = remote_factory(lambda _: httpx.Response(200, json=raw))
    assert remote.read_pr(COMMAND, 2).state is PublicationPrState.MERGED


def test_wrong_repository_command_makes_no_request(remote_factory):
    requests = []
    remote = remote_factory(lambda request: requests.append(request))
    with pytest.raises(PublicationRemoteError):
        remote.read_branch(replace(COMMAND, repo_slug="wrong/repo"))
    assert requests == []


def test_candidate_scan_does_not_hide_later_duplicate(remote_factory):
    pages = []

    def handler(request):
        page = int(request.url.params["page"])
        pages.append(page)
        return httpx.Response(200, json=[pr_payload()] * (100 if page == 1 else 1))

    remote = remote_factory(handler)
    assert len(remote.list_prs(COMMAND)) == 101
    assert pages == [1, 2]


def test_incomplete_scan_is_unreadable(remote_factory):
    remote = remote_factory(lambda _: httpx.Response(200, json=[pr_payload()] * 100))
    with pytest.raises(PublicationRemoteError, match="incomplete"):
        remote.list_prs(COMMAND)


@pytest.mark.parametrize("method", ["read_branch", "read_pr", "list_prs", "create_pr"])
def test_invalid_json_is_a_typed_remote_error(remote_factory, method):
    remote = remote_factory(lambda _: httpx.Response(200, content=b"{not-json"))
    with pytest.raises(PublicationRemoteError):
        getattr(remote, method)(COMMAND, *([2] if method == "read_pr" else []))


@pytest.mark.parametrize(
    "endpoint,accepted",
    [
        ("https://github.com/owner/repo.git", True),
        ("git@github.com:owner/repo.git", True),
        ("ssh://git@github.com/owner/repo", True),
        ("https://wrong.example/owner/repo.git", False),
        ("https://github.com/wrong/repo.git", False),
        ("https://github.com/owner/repo.git?other", False),
        ("https://token@github.com/owner/repo.git", False),
        ("https://github.com:444/owner/repo.git", False),
        ("file:///tmp/repo.git", False),
    ],
)
def test_push_endpoint_is_bound_to_repository_and_host(
    remote_factory, endpoint, accepted
):
    from issue_orchestrator.domain.exact_git import ExactPushDestination

    remote = remote_factory(
        lambda _: pytest.fail("endpoint check must not access HTTP")
    )
    assert (
        remote.accepts_push_destination(COMMAND, ExactPushDestination(endpoint))
        is accepted
    )


@pytest.mark.parametrize(
    "port,accepted", [("", False), (":443", False), (":8443", True)]
)
def test_enterprise_endpoint_requires_effective_configured_port(
    remote_factory, port, accepted
):
    from issue_orchestrator.domain.exact_git import ExactPushDestination

    remote = remote_factory(
        lambda _: pytest.fail("no HTTP expected"),
        api_url="https://git.example:8443/api/v3",
    )
    destination = ExactPushDestination(f"https://git.example{port}/owner/repo.git")
    assert remote.accepts_push_destination(COMMAND, destination) is accepted
