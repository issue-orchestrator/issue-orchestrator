"""The GitHub adapter's integration-branch port at the HTTP boundary (#8144)."""

from __future__ import annotations

import json

import httpx
import pytest

from issue_orchestrator.adapters.github.errors import GitHubHttpError
from issue_orchestrator.adapters.github.github_adapter import GitHubAdapter
from issue_orchestrator.adapters.github.http_client import GitHubHttpClient, GitHubHttpConfig
from issue_orchestrator.domain.integration_branch import (
    BranchComparison,
    BranchMergeOutcome,
    MergedIntoBranch,
    OpenPullRequestRef,
)
from issue_orchestrator.ports.repository_host import RepositoryHostError

SHA_A = "a" * 40
SHA_B = "b" * 40
SHA_C = "c" * 40


@pytest.fixture
def make_host(monkeypatch):
    original = httpx.Client
    seen: list[httpx.Request] = []

    def make(handler) -> GitHubAdapter:
        def recording(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return handler(request)

        monkeypatch.setattr(
            httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(recording), **kwargs)
        )
        client = GitHubHttpClient(GitHubHttpConfig(repo="owner/repo", token="engine-token"))
        return GitHubAdapter(repo="owner/repo", http_client=client)

    make.seen = seen  # type: ignore[attr-defined]
    return make


def _body(request: httpx.Request) -> dict:
    return json.loads(request.content)


def test_branch_head_reads_the_ref_and_absence_is_none(make_host) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/repos/owner/repo/git/ref/heads/feature/integration":
            return httpx.Response(200, json={"object": {"sha": SHA_A}})
        return httpx.Response(404, json={"message": "Not Found"})

    host = make_host(handler)
    assert host.branch_head("feature/integration") == SHA_A
    assert host.branch_head("missing") is None


def test_branch_head_without_a_sha_fails_loud(make_host) -> None:
    host = make_host(lambda request: httpx.Response(200, json={"object": {}}))
    with pytest.raises(RepositoryHostError):
        host.branch_head("integration")


def test_create_and_fast_forward_write_the_heads_ref(make_host) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(201 if request.method == "POST" else 200, json={"ref": "refs/heads/integration"})

    host = make_host(handler)
    host.create_branch("integration", SHA_A)
    host.fast_forward_branch("integration", SHA_B)
    create, update = make_host.seen
    assert (create.method, create.url.path) == ("POST", "/repos/owner/repo/git/refs")
    assert _body(create) == {"ref": "refs/heads/integration", "sha": SHA_A}
    assert (update.method, update.url.path) == ("PATCH", "/repos/owner/repo/git/refs/heads/integration")
    assert _body(update) == {"sha": SHA_B, "force": False}


def test_a_non_fast_forward_fails(make_host) -> None:
    host = make_host(lambda request: httpx.Response(422, json={"message": "Update is not a fast forward"}))
    with pytest.raises(RepositoryHostError):
        host.fast_forward_branch("integration", SHA_B)


def test_compare_reads_counts_and_commits(make_host) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/owner/repo/compare/main...integration"
        return httpx.Response(200, json={
            "ahead_by": 2, "behind_by": 1, "total_commits": 2,
            "commits": [{"sha": SHA_A}, {"sha": SHA_B}],
        })

    assert make_host(handler).compare_commits("main", "integration") == BranchComparison(
        ahead_by=2, behind_by=1, commit_shas=(SHA_A, SHA_B), complete=True
    )


def test_compare_reports_an_incomplete_commit_list(make_host) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "ahead_by": 300, "behind_by": 0, "total_commits": 300, "commits": [{"sha": SHA_A}],
        })

    comparison = make_host(handler).compare_commits("main", "integration")
    assert comparison.complete is False
    assert comparison.contains_base


def test_compare_without_counts_fails_loud(make_host) -> None:
    host = make_host(lambda request: httpx.Response(200, json={"commits": []}))
    with pytest.raises(RepositoryHostError):
        host.compare_commits("main", "integration")


def test_compare_url_encodes_branch_names(make_host) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.raw_path.decode().startswith("/repos/owner/repo/compare/release%2F1...feat%2Fx")
        return httpx.Response(200, json={"ahead_by": 0, "behind_by": 0, "total_commits": 0, "commits": []})

    make_host(handler).compare_commits("release/1", "feat/x")


@pytest.mark.parametrize(
    ("status", "payload", "outcome"),
    [
        (201, {"sha": SHA_C}, BranchMergeOutcome.MERGED),
        (204, None, BranchMergeOutcome.UP_TO_DATE),
        (409, {"message": "Merge conflict"}, BranchMergeOutcome.CONFLICT),
    ],
)
def test_merge_branch_maps_github_outcomes(make_host, status, payload, outcome) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert (request.method, request.url.path) == ("POST", "/repos/owner/repo/merges")
        assert _body(request) == {"base": "integration", "head": "main", "commit_message": "sync"}
        return httpx.Response(status, json=payload) if payload is not None else httpx.Response(status)

    assert make_host(handler).merge_branch(base="integration", head="main", message="sync") is outcome


def test_merge_branch_other_failures_propagate(make_host) -> None:
    host = make_host(lambda request: httpx.Response(404, json={"message": "Base does not exist"}))
    with pytest.raises(GitHubHttpError):
        host.merge_branch(base="integration", head="main", message="sync")


def test_update_branch_is_guarded_by_the_expected_head(make_host) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert (request.method, request.url.path) == ("PUT", "/repos/owner/repo/pulls/7/update-branch")
        assert _body(request) == {"expected_head_sha": SHA_A}
        return httpx.Response(202, json={"message": "Updating pull request branch."})

    make_host(handler).update_pull_request_branch(7, expected_head_sha=SHA_A)
    assert len(make_host.seen) == 1


def test_update_branch_refused_raises(make_host) -> None:
    host = make_host(lambda request: httpx.Response(422, json={"message": "expected head sha didn't match"}))
    with pytest.raises(RepositoryHostError):
        host.update_pull_request_branch(7, expected_head_sha=SHA_A)


def test_merge_pull_request_passes_the_head_sha_and_method(make_host) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert (request.method, request.url.path) == ("PUT", "/repos/owner/repo/pulls/7/merge")
        assert _body(request) == {
            "sha": SHA_A, "merge_method": "merge", "commit_title": "Merge #7: x", "commit_message": "why",
        }
        return httpx.Response(200, json={"merged": True, "sha": SHA_C, "message": "merged"})

    sha = make_host(handler).merge_pull_request(7, head_sha=SHA_A, method="merge", title="Merge #7: x", message="why")
    assert sha == SHA_C


@pytest.mark.parametrize("status", [405, 409])
def test_merge_pull_request_refusals_raise(make_host, status) -> None:
    host = make_host(lambda request: httpx.Response(status, json={"message": "Head branch was modified"}))
    with pytest.raises(RepositoryHostError):
        host.merge_pull_request(7, head_sha=SHA_A, method="merge", title="t", message="m")


def test_find_open_pull_request_filters_by_owner_head_and_base(make_host) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/owner/repo/pulls"
        params = dict(request.url.params)
        assert params["state"] == "open"
        assert params["head"] == "owner:integration"
        assert params["base"] == "main"
        return httpx.Response(200, json=[{"number": 9, "html_url": "https://x/pull/9", "body": "b"}])

    assert make_host(handler).find_open_pull_request(head="integration", base="main") == OpenPullRequestRef(
        number=9, url="https://x/pull/9", body="b"
    )


def test_find_open_pull_request_none_and_ambiguity(make_host) -> None:
    assert make_host(lambda r: httpx.Response(200, json=[])).find_open_pull_request(head="i", base="main") is None
    two = [{"number": 1, "html_url": "u1"}, {"number": 2, "html_url": "u2"}]
    with pytest.raises(RepositoryHostError):
        make_host(lambda r: httpx.Response(200, json=two)).find_open_pull_request(head="i", base="main")


def test_update_pull_request_body_patches_the_body(make_host) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert (request.method, request.url.path) == ("PATCH", "/repos/owner/repo/pulls/9")
        assert _body(request) == {"body": "new body"}
        return httpx.Response(200, json={"number": 9})

    make_host(handler).update_pull_request_body(9, "new body")
    assert len(make_host.seen) == 1


def test_merged_pull_requests_into_keeps_only_merged_ones(make_host) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        params = dict(request.url.params)
        assert (params["state"], params["base"], params["sort"], params["direction"]) == (
            "closed", "integration", "updated", "desc",
        )
        return httpx.Response(200, json=[
            {"number": 3, "html_url": "u3", "title": "Three", "merged_at": "2026-10-08T00:00:00Z",
             "merge_commit_sha": SHA_A},
            {"number": 4, "html_url": "u4", "title": "Closed unmerged", "merged_at": None,
             "merge_commit_sha": SHA_B},
        ])

    assert make_host(handler).merged_pull_requests_into("integration") == (
        MergedIntoBranch(number=3, title="Three", url="u3", merge_commit_sha=SHA_A),
    )
