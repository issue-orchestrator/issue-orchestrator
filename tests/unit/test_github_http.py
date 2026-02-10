"""Unit tests for GitHub HTTP client."""

from __future__ import annotations

import httpx

import json

import pytest

from issue_orchestrator.adapters.github.http_client import (
    GitHubHttpClient,
    GitHubHttpConfig,
    GitHubHttpError,
)
from issue_orchestrator.events import EventName
from issue_orchestrator.infra import gh_audit


def _client_with_transport(transport: httpx.BaseTransport) -> GitHubHttpClient:
    client = GitHubHttpClient(
        GitHubHttpConfig(repo="owner/repo", token="token", base_url="https://api.github.com")
    )
    # noqa: SLF001 - Injecting mock transport for HTTP testing
    client._client = httpx.Client(transport=transport, base_url="https://api.github.com")  # noqa: SLF001
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


def test_list_issues_since_returns_oldest_watermark_hint() -> None:
    payload = [
        {"number": 10, "title": "Newest", "updated_at": "2026-01-02T10:00:00Z"},
        {"number": 9, "title": "Older", "updated_at": "2026-01-02T09:30:00Z"},
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload)

    client = _client_with_transport(httpx.MockTransport(handler))
    issues, watermark = client.list_issues_since(since="2026-01-01T00:00:00Z", limit=20)

    assert len(issues) == 2
    assert watermark == "2026-01-02T09:30:00Z"


def test_list_issues_since_paginates_and_respects_limit() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        page = int(request.url.params.get("page", "1"))
        if page == 1:
            return httpx.Response(200, json=[
                {"number": 100, "title": "A", "updated_at": "2026-01-02T10:00:00Z"},
                {"number": 99, "title": "B", "updated_at": "2026-01-02T09:59:00Z"},
                {"number": 98, "title": "C", "updated_at": "2026-01-02T09:58:00Z"},
            ])
        return httpx.Response(200, json=[
            {"number": 97, "title": "D", "updated_at": "2026-01-02T09:57:00Z"},
        ])

    client = _client_with_transport(httpx.MockTransport(handler))
    issues, watermark = client.list_issues_since(
        since="2026-01-01T00:00:00Z",
        limit=3,
    )

    assert [issue["number"] for issue in issues] == [100, 99, 98]
    assert watermark == "2026-01-02T09:58:00Z"


def test_list_issues_since_default_bypasses_etag_cache() -> None:
    requests_seen: list[dict[str, str]] = []
    payload = [{"number": 1, "title": "Issue", "updated_at": "2026-01-02T10:00:00Z"}]

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append(dict(request.headers))
        return httpx.Response(200, json=payload, headers={"ETag": "W/issues-since-etag"})

    client = _client_with_transport(httpx.MockTransport(handler))

    client.list_issues_since(since="2026-01-01T00:00:00Z", limit=10)
    client.list_issues_since(since="2026-01-01T00:00:00Z", limit=10)

    assert len(requests_seen) == 2
    assert "if-none-match" not in requests_seen[0]
    assert "if-none-match" not in requests_seen[1]


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


def test_get_prs_with_label_state_all_skips_malformed_items() -> None:
    class _Sink:
        def __init__(self) -> None:
            self.events: list[object] = []

        def publish(self, event) -> None:
            self.events.append(event)

    sink = _Sink()
    previous_sink = getattr(gh_audit, "_event_sink", None)
    gh_audit.set_event_sink(sink)

    def handler(request: httpx.Request) -> httpx.Response:
        query = request.url.params.get("q", "")
        if "state:open" in query:
            return httpx.Response(200, json={
                "items": [
                    {"number": 10, "title": "Open PR", "html_url": "https://example.com/open"},
                    {"title": "Malformed PR", "html_url": "https://example.com/bad"},
                ],
            })
        if "state:closed" in query:
            return httpx.Response(200, json={
                "items": [
                    {"number": 10, "title": "Duplicate PR", "html_url": "https://example.com/open"},
                    {"number": 11, "title": "Closed PR", "html_url": "https://example.com/closed"},
                ],
            })
        return httpx.Response(200, json={"items": []})

    client = _client_with_transport(httpx.MockTransport(handler))
    try:
        items = client.get_prs_with_label("test-label", state="all")
    finally:
        gh_audit.set_event_sink(previous_sink)

    numbers = sorted([item["number"] for item in items])
    assert numbers == [10, 11]
    assert any(
        getattr(event, "name", None) == EventName.GH_SEARCH_ITEM_MALFORMED
        for event in sink.events
    )


# -------------------- GraphQL tests --------------------


def test_graphql_successful_query() -> None:
    """Verify _graphql() makes POST to /graphql with query and variables."""
    requests_seen: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests_seen.append({
            "method": request.method,
            "path": request.url.path,
            "body": json.loads(request.content),
        })
        return httpx.Response(200, json={
            "data": {"repository": {"pullRequest": {"id": "PR_123"}}}
        })

    client = _client_with_transport(httpx.MockTransport(handler))
    # noqa: SLF001 - Testing internal GraphQL method error handling
    result = client._graphql(  # noqa: SLF001
        "query($owner: String!) { repository(owner: $owner) { id } }",
        {"owner": "test"},
    )

    assert len(requests_seen) == 1
    assert requests_seen[0]["method"] == "POST"
    assert requests_seen[0]["path"] == "/graphql"
    assert "query" in requests_seen[0]["body"]
    assert requests_seen[0]["body"]["variables"] == {"owner": "test"}
    assert result["data"]["repository"]["pullRequest"]["id"] == "PR_123"


def test_graphql_raises_on_graphql_errors() -> None:
    """Verify _graphql() raises GitHubHttpError on GraphQL-level errors."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "data": None,
            "errors": [{"message": "Field 'foo' not found"}]
        })

    client = _client_with_transport(httpx.MockTransport(handler))

    with pytest.raises(GitHubHttpError) as exc_info:
        client._graphql("query { foo }")  # noqa: SLF001

    assert "Field 'foo' not found" in str(exc_info.value)


def test_graphql_raises_on_http_error() -> None:
    """Verify _graphql() raises GitHubHttpError on HTTP errors."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, json={"message": "Bad credentials"})

    client = _client_with_transport(httpx.MockTransport(handler))

    with pytest.raises(GitHubHttpError) as exc_info:
        client._graphql("query { viewer { login } }")  # noqa: SLF001

    assert exc_info.value.status_code == 401


# -------------------- set_pr_draft tests --------------------


def test_set_pr_draft_marks_ready_for_review() -> None:
    """Verify set_pr_draft(draft=False) uses markPullRequestReadyForReview mutation."""
    requests_seen: list[dict] = []
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        body = json.loads(request.content)
        requests_seen.append({"call": call_count, "body": body})

        if call_count == 1:
            # First call: query for node ID
            return httpx.Response(200, json={
                "data": {"repository": {"pullRequest": {"id": "PR_node_123"}}}
            })
        else:
            # Second call: mutation
            return httpx.Response(200, json={
                "data": {
                    "markPullRequestReadyForReview": {
                        "pullRequest": {"id": "PR_node_123", "number": 42, "isDraft": False}
                    }
                }
            })

    client = _client_with_transport(httpx.MockTransport(handler))
    result = client.set_pr_draft(42, draft=False)

    assert len(requests_seen) == 2
    # First request: get node ID
    assert "pullRequest(number: $number)" in requests_seen[0]["body"]["query"]
    assert requests_seen[0]["body"]["variables"]["number"] == 42
    # Second request: mutation
    assert "markPullRequestReadyForReview" in requests_seen[1]["body"]["query"]
    assert requests_seen[1]["body"]["variables"]["pullRequestId"] == "PR_node_123"
    # Result
    assert result["isDraft"] is False  # type: ignore
    assert result["number"] == 42  # type: ignore


def test_set_pr_draft_converts_to_draft() -> None:
    """Verify set_pr_draft(draft=True) uses convertPullRequestToDraft mutation."""
    requests_seen: list[dict] = []
    call_count = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal call_count
        call_count += 1
        body = json.loads(request.content)
        requests_seen.append({"call": call_count, "body": body})

        if call_count == 1:
            return httpx.Response(200, json={
                "data": {"repository": {"pullRequest": {"id": "PR_node_456"}}}
            })
        else:
            return httpx.Response(200, json={
                "data": {
                    "convertPullRequestToDraft": {
                        "pullRequest": {"id": "PR_node_456", "number": 99, "isDraft": True}
                    }
                }
            })

    client = _client_with_transport(httpx.MockTransport(handler))
    result = client.set_pr_draft(99, draft=True)

    assert len(requests_seen) == 2
    # Second request should use convertPullRequestToDraft
    assert "convertPullRequestToDraft" in requests_seen[1]["body"]["query"]
    assert result["isDraft"] is True  # type: ignore


def test_set_pr_draft_raises_on_pr_not_found() -> None:
    """Verify set_pr_draft() raises GitHubHttpError when PR doesn't exist."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "data": {"repository": {"pullRequest": None}}
        })

    client = _client_with_transport(httpx.MockTransport(handler))

    with pytest.raises(GitHubHttpError) as exc_info:
        client.set_pr_draft(9999, draft=False)

    assert "PR #9999 not found" in str(exc_info.value)


def test_get_pr_reviews_returns_list() -> None:
    """Verify get_pr_reviews() returns list of reviews."""
    reviews = [
        {
            "id": 1,
            "state": "CHANGES_REQUESTED",
            "body": "Please add tests",
            "user": {"login": "reviewer1"},
        },
        {
            "id": 2,
            "state": "APPROVED",
            "body": "LGTM",
            "user": {"login": "reviewer2"},
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        assert "/pulls/123/reviews" in request.url.path
        return httpx.Response(200, json=reviews)

    client = _client_with_transport(httpx.MockTransport(handler))
    result = client.get_pr_reviews(123)

    assert len(result) == 2
    assert result[0]["state"] == "CHANGES_REQUESTED"
    assert result[0]["body"] == "Please add tests"
    assert result[1]["state"] == "APPROVED"


def test_get_pr_reviews_returns_empty_list_on_non_list_response() -> None:
    """Verify get_pr_reviews() returns empty list if response is not a list."""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"error": "unexpected"})

    client = _client_with_transport(httpx.MockTransport(handler))
    result = client.get_pr_reviews(123)

    assert result == []
