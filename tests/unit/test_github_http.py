"""Unit tests for GitHub HTTP client."""

from __future__ import annotations

import httpx

from issue_orchestrator.adapters.github.http_client import (
    GitHubHttpClient,
    GitHubHttpConfig,
)


def _client_with_transport(transport: httpx.BaseTransport) -> GitHubHttpClient:
    client = GitHubHttpClient(
        GitHubHttpConfig(repo="owner/repo", token="token", base_url="https://api.github.com")
    )
    client._client = httpx.Client(transport=transport, base_url="https://api.github.com")
    return client


def test_get_issue_uses_etag_cache() -> None:
    seen = {"count": 0}
    payload = {"number": 1, "title": "Test"}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["count"] += 1
        if seen["count"] == 1:
            return httpx.Response(200, json=payload, headers={"ETag": "W/etag"})
        return httpx.Response(304, text="")

    client = _client_with_transport(httpx.MockTransport(handler))
    first = client.get_issue(1)
    second = client.get_issue(1)

    assert first == payload
    assert second == payload


def test_create_label_force_updates_on_422() -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        if request.method == "POST":
            return httpx.Response(422, json={"message": "exists"})
        return httpx.Response(200, json={"name": "bug"})

    client = _client_with_transport(httpx.MockTransport(handler))
    client.create_label("bug", force=True)

    assert calls[0][0] == "POST"
    assert calls[1][0] == "PATCH"


def test_create_label_ignores_422_without_force() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(422, json={"message": "exists"})

    client = _client_with_transport(httpx.MockTransport(handler))
    client.create_label("bug", force=False)


def test_list_issues_filters_pull_requests() -> None:
    payload = [
        {"number": 1, "title": "Issue"},
        {"number": 2, "title": "PR", "pull_request": {"url": "https://api.github.com/pulls/2"}},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = _client_with_transport(httpx.MockTransport(handler))
    issues = client.list_issues()

    assert len(issues) == 1
    assert issues[0]["number"] == 1


def test_get_token_scopes_from_header() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"login": "user"},
            headers={"X-OAuth-Scopes": "repo, read:org"},
        )

    client = _client_with_transport(httpx.MockTransport(handler))
    scopes = client.get_token_scopes()

    assert scopes == ["repo", "read:org"]


def test_list_issues_use_cache_false_bypasses_etag() -> None:
    """Verify that use_cache=False bypasses ETag caching.

    When use_cache=False:
    - First call should not send If-None-Match header
    - Server returning 304 should NOT happen (no ETag sent)
    - Fresh data should always be fetched
    """
    requests_seen: list[dict] = []
    payload = [{"number": 1, "title": "Fresh Issue"}]

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append({
            "method": request.method,
            "path": request.url.path,
            "headers": dict(request.headers),
        })
        # Always return 200 with fresh data
        return httpx.Response(200, json=payload, headers={"ETag": "W/fresh-etag"})

    client = _client_with_transport(httpx.MockTransport(handler))

    # First call with cache - establishes ETag
    first_cached = client.list_issues(use_cache=True)
    assert len(first_cached) == 1
    assert len(requests_seen) == 1
    assert "If-None-Match" not in requests_seen[0]["headers"]

    # Second call with use_cache=False - should NOT send If-None-Match
    requests_seen.clear()
    second_uncached = client.list_issues(use_cache=False)
    assert len(second_uncached) == 1
    assert len(requests_seen) == 1
    # The key assertion: no If-None-Match header when use_cache=False
    # (httpx stores headers as lowercase)
    assert "if-none-match" not in requests_seen[0]["headers"]


def test_list_issues_use_cache_true_sends_etag() -> None:
    """Verify that use_cache=True sends If-None-Match for cached responses."""
    requests_seen: list[dict] = []
    payload = [{"number": 1, "title": "Cached Issue"}]
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        requests_seen.append({
            "method": request.method,
            "path": request.url.path,
            "headers": dict(request.headers),
        })
        if call_count == 1:
            # First call: return data with ETag
            return httpx.Response(200, json=payload, headers={"ETag": "W/cached-etag"})
        else:
            # Second call: should have If-None-Match, return 304
            return httpx.Response(304, text="")

    client = _client_with_transport(httpx.MockTransport(handler))

    # First call - establishes cache
    first = client.list_issues(use_cache=True)
    assert len(first) == 1

    # Second call with use_cache=True - should send If-None-Match
    second = client.list_issues(use_cache=True)
    assert len(second) == 1
    assert len(requests_seen) == 2
    # The key assertion: If-None-Match header sent when use_cache=True
    # (httpx stores headers as lowercase)
    assert "if-none-match" in requests_seen[1]["headers"]
    assert requests_seen[1]["headers"]["if-none-match"] == "W/cached-etag"


def test_get_issue_labels_use_cache_false_bypasses_etag() -> None:
    """Verify that use_cache=False bypasses ETag caching for label reads."""
    requests_seen: list[dict] = []
    payload = [{"name": "bug"}]

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append({
            "method": request.method,
            "path": request.url.path,
            "headers": dict(request.headers),
        })
        return httpx.Response(200, json=payload, headers={"ETag": "W/labels-etag"})

    client = _client_with_transport(httpx.MockTransport(handler))

    first = client.get_issue_labels(1, use_cache=True)
    assert first == ["bug"]
    assert "if-none-match" not in requests_seen[0]["headers"]

    requests_seen.clear()
    second = client.get_issue_labels(1, use_cache=False)
    assert second == ["bug"]
    assert "if-none-match" not in requests_seen[0]["headers"]


def test_get_issue_labels_use_cache_true_sends_etag() -> None:
    """Verify that use_cache=True sends If-None-Match for label reads."""
    requests_seen: list[dict] = []
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        requests_seen.append({
            "method": request.method,
            "path": request.url.path,
            "headers": dict(request.headers),
        })
        if call_count == 1:
            return httpx.Response(200, json=[{"name": "bug"}], headers={"ETag": "W/labels-etag"})
        return httpx.Response(304, text="")

    client = _client_with_transport(httpx.MockTransport(handler))

    first = client.get_issue_labels(1, use_cache=True)
    assert first == ["bug"]
    second = client.get_issue_labels(1, use_cache=True)
    assert second == ["bug"]
    assert "if-none-match" in requests_seen[1]["headers"]


def test_invalidate_labels_etag_clears_cache() -> None:
    """Verify that invalidate_labels_etag clears the ETag cache for labels.

    After a write operation on labels (POST/PATCH/DELETE), the ETag cache
    should be invalidated so subsequent list_labels() fetches fresh data
    instead of getting a stale 304.
    """
    requests_seen: list[dict] = []
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        requests_seen.append({
            "method": request.method,
            "path": request.url.path,
            "headers": dict(request.headers),
        })
        if "if-none-match" in request.headers:
            # If client sent If-None-Match, return 304
            return httpx.Response(304, text="")
        # Otherwise return fresh data with new ETag
        return httpx.Response(
            200,
            json=[{"name": f"label-{call_count}"}],
            headers={"ETag": f"W/etag-{call_count}"},
        )

    client = _client_with_transport(httpx.MockTransport(handler))

    # First call - establishes cache
    first = client.list_labels()
    assert len(first) == 1
    assert first[0]["name"] == "label-1"
    assert len(requests_seen) == 1
    assert "if-none-match" not in requests_seen[0]["headers"]

    # Second call without invalidation - should send If-None-Match, get 304
    requests_seen.clear()
    second = client.list_labels()
    assert len(second) == 1
    assert second[0]["name"] == "label-1"  # Cached value from 304
    assert "if-none-match" in requests_seen[0]["headers"]

    # Invalidate the cache
    client.invalidate_labels_etag()

    # Third call after invalidation - should NOT send If-None-Match
    requests_seen.clear()
    third = client.list_labels()
    assert len(third) == 1
    assert third[0]["name"] == "label-3"  # Fresh data, not cached
    assert "if-none-match" not in requests_seen[0]["headers"]
