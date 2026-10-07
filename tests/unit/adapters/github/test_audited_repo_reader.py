"""The improver toolbox's GitHub reads are bounded before they are decoded (#8001)."""

from __future__ import annotations

import json

import httpx
import pytest

from issue_orchestrator.adapters.github.audited_repo_reader import GitHubAuditedRepoReader
from issue_orchestrator.adapters.github.errors import GitHubHttpError
from issue_orchestrator.domain.improver_toolbox_policy import AuditedRepoReadPolicy
from issue_orchestrator.ports.improver_toolbox import AuditedReadTooLarge
from tests.unit.test_github_http import _client_with_transport

READ = AuditedRepoReadPolicy("owner/repo").check("repos/owner/repo/git/blobs/abc", {"per_page": 5})


class Body(httpx.SyncByteStream):
    """A response body served in chunks, counting what was pulled from it."""

    def __init__(self, total: int) -> None:
        self.total = total
        self.pulled = 0

    def __iter__(self):  # type: ignore[no-untyped-def]
        while self.pulled < self.total:
            chunk = b"x" * min(65536, self.total - self.pulled)
            self.pulled += len(chunk)
            yield chunk


def _reader(handler) -> GitHubAuditedRepoReader:  # type: ignore[no-untyped-def]
    return GitHubAuditedRepoReader("owner/repo", http_client=_client_with_transport(httpx.MockTransport(handler)))


def test_a_read_returns_the_decoded_answer_of_the_allowed_path() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"sha": "abc", "size": 3})

    assert _reader(handler).get(READ, max_bytes=1000) == {"sha": "abc", "size": 3}
    assert seen[0].method == "GET" and seen[0].url.path == "/repos/owner/repo/git/blobs/abc"
    assert seen[0].url.params["per_page"] == "5"


def test_a_streamed_body_past_the_limit_is_refused_after_reading_about_the_limit() -> None:
    """r7 F1: a 100 MB blob is never buffered whole (no Content-Length)."""
    body = Body(100_000_000)

    with pytest.raises(AuditedReadTooLarge):
        _reader(lambda request: httpx.Response(200, stream=body)).get(READ, max_bytes=1_000_000)

    assert body.pulled <= 1_000_000 + 65536


def test_a_declared_length_past_the_limit_is_refused_before_reading() -> None:
    body = Body(5_000_000)

    with pytest.raises(AuditedReadTooLarge):
        _reader(
            lambda request: httpx.Response(200, headers={"Content-Length": "5000000"}, stream=body)
        ).get(READ, max_bytes=1_000_000)

    assert body.pulled == 0


def test_a_failed_read_raises_and_a_redirect_is_not_followed() -> None:
    def moved(request: httpx.Request) -> httpx.Response:
        return httpx.Response(301, headers={"Location": "https://api.github.com/repos/other/repo"}, content=b"{}")

    with pytest.raises(GitHubHttpError):
        _reader(moved).get(READ, max_bytes=1000)
    with pytest.raises(GitHubHttpError):
        _reader(lambda request: httpx.Response(404, content=json.dumps({"message": "Not Found"}).encode())).get(
            READ, max_bytes=1000
        )
