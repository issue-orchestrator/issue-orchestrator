"""The audited repository's GitHub activity, read raw and bounded (#8001)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from issue_orchestrator.adapters.github import operator_activity
from issue_orchestrator.adapters.github.operator_activity import GitHubOperatorActivity
from issue_orchestrator.ports.operator_activity import MalformedActivity
from tests.unit.test_github_http import _client_with_transport

UNTIL = datetime(2026, 10, 5, 12, 0, tzinfo=UTC)
SINCE = UNTIL - timedelta(days=1)


def _iso(at: datetime) -> str:
    return at.strftime("%Y-%m-%dT%H:%M:%SZ")


def _event(i: int, at: datetime, **extra: object) -> dict:
    return {
        "id": i, "event": "labeled", "created_at": _iso(at), "issue": {"number": 364},
        "actor": {"login": "BruceBGordon", "type": "User"}, "performed_via_github_app": None,
        "label": {"name": "approved"}, **extra,
    }


def _source(events_pages: list[list[dict]], comments: list[dict], search: dict, seen: list[httpx.Request]):  # type: ignore[no-untyped-def]
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/issues/events"):
            page = int(request.url.params["page"])
            return httpx.Response(200, json=events_pages[page - 1] if page <= len(events_pages) else [])
        if request.url.path.endswith("/issues/comments"):
            return httpx.Response(200, json=comments if request.url.params["page"] == "1" else [])
        if request.url.path == "/graphql":
            return httpx.Response(200, json={"data": {"search": search}})
        return httpx.Response(404, json={"message": "Not Found"})

    return GitHubOperatorActivity("porchpin/porchpin", http_client=_client_with_transport(httpx.MockTransport(handler)))


_EMPTY_SEARCH = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": []}


def test_events_are_paged_back_to_the_windows_start_with_each_actor() -> None:
    full = [_event(i, UNTIL - timedelta(minutes=i)) for i in range(100)]
    older = [_event(200, SINCE - timedelta(hours=1)),
             _event(201, SINCE - timedelta(hours=2), actor={"login": "porchpin-bot[bot]", "type": "Bot"},
                    performed_via_github_app={"slug": "porchpin-bot"})]
    seen: list[httpx.Request] = []

    read = _source([full, older], [], _EMPTY_SEARCH, seen).read(since=SINCE, until=UNTIL)

    assert len(read.events) == 102
    assert read.events[0].actor.login == "BruceBGordon" and read.events[0].label == "approved"
    assert read.events[-1].actor.via_app is True and read.events[-1].actor.account_type == "Bot"
    assert read.events[0].ref == "https://github.com/porchpin/porchpin/issues/364#event-0"
    [events_read, comments_read, items_read] = read.sources
    assert events_read.complete and events_read.detail == "2 page(s)"
    assert sum(r.url.path.endswith("/issues/events") for r in seen) == 2


def test_a_source_that_hits_its_page_bound_says_it_is_incomplete(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.setattr(operator_activity, "EVENT_PAGES", 2)
    pages = [[_event(i, UNTIL - timedelta(minutes=i)) for i in range(p * 100, p * 100 + 100)] for p in range(3)]

    read = _source(pages, [], _EMPTY_SEARCH, []).read(since=SINCE - timedelta(days=30), until=UNTIL)

    assert read.sources[0].complete is False and "stopped after 2 pages" in read.sources[0].detail


def test_comments_and_items_carry_their_actors_and_a_truncated_list_is_said() -> None:
    comments = [{
        "created_at": _iso(UNTIL), "updated_at": _iso(UNTIL),
        "issue_url": "https://api.github.com/repos/porchpin/porchpin/issues/379",
        "user": {"login": "BruceBGordon", "type": "User"}, "performed_via_github_app": None,
        "body": "Ruling: rework to this design", "html_url": "https://github.com/porchpin/porchpin/issues/379#c1",
    }]
    search = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [{
        "__typename": "PullRequest", "number": 511, "url": "https://github.com/porchpin/porchpin/pull/511",
        "createdAt": _iso(SINCE), "author": {"login": "porchpin-bot", "__typename": "Bot"},
        "mergedAt": _iso(UNTIL), "mergedBy": {"login": "BruceBGordon", "__typename": "User"},
        "userContentEdits": {"totalCount": 60, "nodes": [{"editedAt": _iso(UNTIL), "editor": {"login": "BruceBGordon", "__typename": "User"}}]},
        "reviews": {"totalCount": 0, "nodes": []},
    }, {"__typename": "Discussion"}]}
    seen: list[httpx.Request] = []

    read = _source([[]], comments, search, seen).read(since=SINCE, until=UNTIL)

    [comment] = read.comments
    assert (comment.number, comment.actor.login, comment.body) == (379, "BruceBGordon", "Ruling: rework to this design")
    [item] = read.items
    assert item.is_pr and item.merged_by is not None and item.merged_by.login == "BruceBGordon"
    assert item.author.account_type == "Bot" and item.edits[0].editor.account_type == "User"
    assert read.sources[2].complete is False and "#511" in read.sources[2].detail
    graphql = next(r for r in seen if r.url.path == "/graphql")
    assert json.loads(graphql.content)["variables"]["q"] == "repo:porchpin/porchpin updated:>=2026-10-04T12:00:00Z"
    # Every request is a read.
    assert {r.method for r in seen} == {"GET", "POST"} and all(
        r.method == "GET" or r.url.path == "/graphql" for r in seen
    )
    assert not json.loads(graphql.content)["query"].lstrip().startswith("mutation")


def _pr_node(**overrides: object) -> dict:
    node = {
        "__typename": "PullRequest", "number": 511, "url": "u", "createdAt": _iso(SINCE),
        "author": None, "mergedAt": None, "mergedBy": None,
        "userContentEdits": {"totalCount": 0, "nodes": []}, "reviews": {"totalCount": 0, "nodes": []},
    }
    node.update(overrides)
    return {k: v for k, v in node.items() if v != "DROP"}


@pytest.mark.parametrize(
    ("events", "comments", "nodes"),
    [
        # r1 F2: a relevant event without its issue, or a rename without its titles.
        ([{"id": 1, "event": "labeled", "created_at": "2026-10-05T11:00:00Z", "label": {"name": "approved"},
           "actor": {"login": "BruceBGordon", "type": "User"}}], [], []),
        ([_event(2, UNTIL, event="renamed")], [], []),
        ([_event(3, UNTIL, label=None)], [], []),
        # r2 F2: the nested fields themselves.
        ([_event(4, UNTIL, label={})], [], []),
        ([_event(5, UNTIL, event="renamed", rename={})], [], []),
        ([_event(6, UNTIL, event="renamed", rename={"from": "a"})], [], []),
        # A comment whose issue_url names no issue.
        ([], [{"created_at": _iso(UNTIL), "updated_at": _iso(UNTIL), "issue_url": "https://x/issues/",
               "html_url": "h", "user": {"login": "a", "type": "User"}, "body": "b"}], []),
        # r1 F3: a PR without its reviews, an item without its edits.
        ([], [], [_pr_node(reviews="DROP")]),
        ([], [], [_pr_node(userContentEdits="DROP")]),
        ([], [], [_pr_node(reviews={"nodes": []})]),
    ],
)
def test_a_malformed_row_fails_the_read_never_a_complete_read_with_it_missing(events, comments, nodes) -> None:  # type: ignore[no-untyped-def]
    search = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": nodes}

    with pytest.raises(MalformedActivity):
        _source([events], comments, search, []).read(since=SINCE, until=UNTIL)


def test_a_comment_from_before_the_window_edited_in_it_is_counted_not_staged() -> None:
    """GitHub's ``since`` is the update time: an older comment edited in the
    window is returned, and its editor is unknown."""
    older = {
        "created_at": _iso(SINCE - timedelta(days=3)), "updated_at": _iso(UNTIL),
        "issue_url": "https://api.github.com/repos/porchpin/porchpin/issues/12", "html_url": "h12",
        "user": {"login": "BruceBGordon", "type": "User"}, "body": "old text",
    }

    read = _source([[]], [older], _EMPTY_SEARCH, []).read(since=SINCE, until=UNTIL)

    assert read.comments == ()
    assert "1 comment(s) created before the window and edited in it are not staged" in read.sources[1].detail


def test_app_provenance_is_carried_where_github_reports_it_and_marked_unreported_where_not() -> None:
    """r2 F1: an App acting with a user's token is attributed to the user.
    Where GitHub reports provenance the action is `checked`; GraphQL actors
    and a comment row without the field are `unreported`, never presented as
    proven hand work."""
    from issue_orchestrator.domain.operator_interventions import ATTRIBUTION_LIMITS, github_interventions

    with_key = {"created_at": _iso(UNTIL), "updated_at": _iso(UNTIL), "performed_via_github_app": None,
                "issue_url": "https://api.github.com/repos/porchpin/porchpin/issues/1", "html_url": "c-with",
                "user": {"login": "BruceBGordon", "type": "User"}, "body": "a person's comment"}
    without_key = {k: v for k, v in with_key.items() if k != "performed_via_github_app"} | {"html_url": "c-without"}
    search = {"pageInfo": {"hasNextPage": False, "endCursor": None}, "nodes": [{
        "__typename": "Issue", "number": 520, "url": "i520", "createdAt": _iso(UNTIL),
        "author": {"login": "BruceBGordon", "__typename": "User"}, "userContentEdits": {"totalCount": 0, "nodes": []},
    }]}

    read = _source([[_event(1, UNTIL)]], [with_key, without_key], search, []).read(since=SINCE, until=UNTIL)
    hand, _ = github_interventions(read, since=SINCE, until=UNTIL)

    by_ref = {i.ref: i.app_provenance for i in hand}
    assert by_ref["https://github.com/porchpin/porchpin/issues/364#event-1"] == "checked"
    assert by_ref["c-with"] == "checked"
    assert by_ref["c-without"] == "unreported"
    assert by_ref["i520"] == "unreported"
    assert any("not proven hand work" in limit for limit in ATTRIBUTION_LIMITS)
