"""Authenticated HTTP boundary for complete pull-request diff content."""

import httpx
import pytest

from issue_orchestrator.adapters.github.http_client import (
    GitHubHttpClient,
    GitHubHttpConfig,
    GitHubHttpError,
)


def _client(monkeypatch, handler) -> GitHubHttpClient:
    original_client = httpx.Client
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original_client(
            transport=httpx.MockTransport(handler), **kwargs
        ),
    )
    return GitHubHttpClient(GitHubHttpConfig(repo="owner/repo", token="token"))


def test_get_pr_diff_requests_raw_diff_media_type_and_preserves_content(monkeypatch):
    expected = "diff --git a/a.py b/a.py\nindex 1..2 100644\n--- a/a.py\n+++ b/a.py\n"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/repos/owner/repo/pulls/42"
        assert request.headers["accept"] == "application/vnd.github.v3.diff"
        assert request.headers["authorization"] == "Bearer token"
        return httpx.Response(200, text=expected)

    assert _client(monkeypatch, handler).get_pr_diff(42) == expected


def test_get_pr_diff_preserves_upstream_http_failure(monkeypatch):
    client = _client(
        monkeypatch,
        lambda _request: httpx.Response(503, text="temporarily unavailable"),
    )

    with pytest.raises(GitHubHttpError, match="503"):
        client.get_pr_diff(42)


@pytest.mark.parametrize("status_code", [301, 302, 307, 304])
def test_get_pr_diff_rejects_non_success_evidence(monkeypatch, status_code):
    client = _client(
        monkeypatch,
        lambda _request: httpx.Response(status_code, text="not diff evidence"),
    )

    with pytest.raises(GitHubHttpError, match=str(status_code)):
        client.get_pr_diff(42)
