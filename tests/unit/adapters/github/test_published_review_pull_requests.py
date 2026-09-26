"""The custody owner's PR read, through the real GitHub adapter (#7293).

Custody decides whether a reset may close a PR and whether a coder may launch
over its branch, so its read must be complete: an issue search is capped at one
page, and the adapter's PR caches hold one entry. The read is by the published
record's own branch, paginated to completion and never cached.
"""

import httpx
import pytest

from issue_orchestrator.adapters.github.github_adapter import GitHubAdapter
from issue_orchestrator.adapters.github.http_client import GitHubHttpClient, GitHubHttpConfig
from issue_orchestrator.control.published_review_custody import PublishedReviewCustody
from issue_orchestrator.domain.validated_work import ValidatedWorkState
from issue_orchestrator.ports.repository_host import RepositoryHostError

from tests.unit.control.published_review_support import (
    PUBLISHED,
    DispositionStore,
    branch,
    disposition,
)

ISSUE = 392


def _raw_pr(number: int, head_ref: str, sha: str = "c" * 40) -> dict:
    repo = {"full_name": "owner/repo"}
    return {
        "number": number, "title": f"#{ISSUE}: work", "html_url": f"https://github.com/owner/repo/pull/{number}",
        "body": "", "state": "open", "labels": [], "draft": False,
        "head": {"ref": head_ref, "sha": sha, "repo": repo},
        "base": {"ref": "main", "repo": repo},
    }


def _adapter(monkeypatch, handler) -> GitHubAdapter:
    original = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs))
    client = GitHubHttpClient(GitHubHttpConfig(repo="owner/repo", token="token"))
    return GitHubAdapter(repo="owner/repo", http_client=client, verify_writes=False)


def test_the_carrying_pr_on_a_later_page_is_found(monkeypatch):
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/repos/owner/repo/pulls"
        assert request.url.params["head"] == f"owner:{branch(ISSUE)}"
        assert request.url.params["state"] == "open"
        if request.url.params["page"] == "1":
            return httpx.Response(200, json=[_raw_pr(1000 + n, branch(ISSUE)) for n in range(100)])
        return httpx.Response(200, json=[_raw_pr(500, branch(ISSUE), sha=PUBLISHED)])

    store = DispositionStore({ISSUE: (disposition(ISSUE, ValidatedWorkState.RECOVERED),)})
    custody = PublishedReviewCustody(store, _adapter(monkeypatch, handler))

    assert [hold.pr_number for hold in custody.holds(ISSUE)] == [500]
    assert [r.url.params["page"] for r in requests] == ["1", "2"]
    assert all("if-none-match" not in r.headers for r in requests), "never a cached answer"


def test_a_scan_that_cannot_prove_completeness_fails_closed(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params["page"])
        return httpx.Response(200, json=[_raw_pr(page * 1000 + n, branch(ISSUE)) for n in range(100)])

    store = DispositionStore({ISSUE: (disposition(ISSUE, ValidatedWorkState.RECOVERED),)})
    custody = PublishedReviewCustody(store, _adapter(monkeypatch, handler))

    with pytest.raises(RepositoryHostError):
        custody.holds(ISSUE)


@pytest.mark.parametrize(("total", "expected"), [(0, False), (1, True), (250, True)])
def test_retry_asks_github_for_an_uncached_open_only_count(monkeypatch, total, expected):
    """Retry's pr-pending decision (#7293): server-side ``is:open``, so a page
    of closed PRs can never hide the open one behind it."""
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.url.path == "/search/issues"
        return httpx.Response(200, json={"total_count": total, "incomplete_results": False,
                                         "items": []})

    assert _adapter(monkeypatch, handler).has_open_pr_for_issue_complete(ISSUE) is expected
    (request,) = requests
    assert "is:open" in request.url.params["q"] and f"#{ISSUE}" in request.url.params["q"]
    assert "if-none-match" not in request.headers


def test_an_incomplete_open_count_fails_closed(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"total_count": 0, "incomplete_results": True, "items": []})

    with pytest.raises(RepositoryHostError):
        _adapter(monkeypatch, handler).has_open_pr_for_issue_complete(ISSUE)
