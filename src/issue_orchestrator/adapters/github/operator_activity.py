"""The audited repository's recent GitHub activity, raw, for the improver (#8001).

Implements :class:`~...ports.operator_activity.OperatorActivitySource` with
bounded reads, each reporting whether it reached back to the window's start:

* issue events (``/repos/<r>/issues/events``, newest first): label changes,
  renames, closes and reopens, with each event's actor;
* issue and PR comments updated since the window began;
* the issues and PRs updated in the window (one GraphQL search): who opened
  each, who edited its body, reviewed it or merged it.

People and automation are told apart by the domain policy, not here.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from ...ports.operator_activity import (
    Actor,
    ContentEdit,
    ItemActivity,
    RepoActivityRead,
    RepoComment,
    RepoEvent,
    Review,
    SourceRead,
)
from .http_client import GitHubHttpClient, GitHubHttpConfig, build_github_auth

API_URL = "https://api.github.com"
EVENT_PAGES = 20
COMMENT_PAGES = 10
SEARCH_PAGES = 5
PAGE_BYTES = 8_000_000
_NESTED = 50

_SEARCH = """query($q: String!, $after: String) {
  search(query: $q, type: ISSUE, first: 50, after: $after) {
    pageInfo { hasNextPage endCursor }
    nodes {
      __typename
      ... on Issue { number url createdAt author { login __typename }
        userContentEdits(first: %(n)d) { totalCount nodes { editedAt editor { login __typename } } } }
      ... on PullRequest { number url createdAt author { login __typename } mergedAt mergedBy { login __typename }
        userContentEdits(first: %(n)d) { totalCount nodes { editedAt editor { login __typename } } }
        reviews(first: %(n)d) { totalCount nodes { state submittedAt author { login __typename } } } }
    }
  }
}""" % {"n": _NESTED}


class GitHubOperatorActivity:
    def __init__(self, repo: str, *, http_client: GitHubHttpClient | None = None) -> None:
        self._repo = repo
        self._client = http_client or GitHubHttpClient(
            GitHubHttpConfig(repo=repo, base_url=API_URL, auth=build_github_auth(repo=repo, api_url=API_URL))
        )

    def read(self, *, since: datetime, until: datetime) -> RepoActivityRead:
        events, events_read = self._events(since)
        comments, comments_read = self._comments(since)
        items, items_read = self._items(since)
        return RepoActivityRead(
            events=events, comments=comments, items=items, sources=(events_read, comments_read, items_read)
        )

    def _page(self, path: str, params: dict[str, Any]) -> list[dict[str, Any]]:
        page = self._client.get_json_bounded(path, params=params, max_bytes=PAGE_BYTES, caller="improver_interventions")
        if not isinstance(page, list):
            raise ValueError(f"GitHub GET {path} did not answer a list")
        return page

    def _events(self, since: datetime) -> tuple[tuple[RepoEvent, ...], SourceRead]:
        found: list[RepoEvent] = []
        for number in range(1, EVENT_PAGES + 1):
            page = self._page(f"/repos/{self._repo}/issues/events", {"per_page": 100, "page": number})
            found += (_event(e, self._repo) for e in page if e.get("issue"))
            if len(page) < 100 or (page and _at(page[-1]["created_at"]) < since):
                return tuple(found), SourceRead("issue events", True, f"{number} page(s)")
        return tuple(found), SourceRead(
            "issue events", False, f"stopped after {EVENT_PAGES} pages, before the window's start"
        )

    def _comments(self, since: datetime) -> tuple[tuple[RepoComment, ...], SourceRead]:
        found: list[RepoComment] = []
        params: dict[str, Any] = {"since": since.isoformat(), "sort": "updated", "direction": "asc", "per_page": 100}
        for number in range(1, COMMENT_PAGES + 1):
            page = self._page(f"/repos/{self._repo}/issues/comments", {**params, "page": number})
            found += (_comment(c) for c in page)
            if len(page) < 100:
                return tuple(found), SourceRead("comments", True, f"{number} page(s)")
        return tuple(found), SourceRead(
            "comments", False, f"stopped after {COMMENT_PAGES} pages; later comments are missing"
        )

    def _items(self, since: datetime) -> tuple[tuple[ItemActivity, ...], SourceRead]:
        found: list[ItemActivity] = []
        truncated: list[int] = []
        after: str | None = None
        query = f"repo:{self._repo} updated:>={since.strftime('%Y-%m-%dT%H:%M:%SZ')}"
        for number in range(1, SEARCH_PAGES + 1):
            search = self._client.graphql_query(
                _SEARCH, {"q": query, "after": after}, caller="improver_interventions"
            )["search"]
            for node in search["nodes"]:
                if node.get("__typename") not in ("Issue", "PullRequest"):
                    continue
                item, cut = _item(node)
                found.append(item)
                if cut:
                    truncated.append(item.number)
            if not search["pageInfo"]["hasNextPage"]:
                detail = f"{number} page(s)" + (
                    f"; only the first {_NESTED} edits/reviews of #{', #'.join(map(str, truncated))}"
                    if truncated else ""
                )
                return tuple(found), SourceRead("issues and PRs", not truncated, detail)
            after = search["pageInfo"]["endCursor"]
        return tuple(found), SourceRead(
            "issues and PRs", False, f"stopped after {SEARCH_PAGES} search pages; more items were updated"
        )


def _at(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def _rest_actor(user: dict[str, Any] | None, app: object) -> Actor:
    if not user:
        return Actor(None, None, via_app=bool(app))
    return Actor(user.get("login"), user.get("type"), via_app=bool(app))


def _graph_actor(node: dict[str, Any] | None) -> Actor:
    if not node:
        return Actor(None, None)
    return Actor(node.get("login"), node.get("__typename"))


def _event(e: dict[str, Any], repo: str) -> RepoEvent:
    number = int(e["issue"]["number"])
    rename = e.get("rename")
    return RepoEvent(
        at=_at(e["created_at"]),
        event=str(e["event"]),
        number=number,
        actor=_rest_actor(e.get("actor"), e.get("performed_via_github_app")),
        label=(e.get("label") or {}).get("name"),
        rename=(str(rename.get("from", "")), str(rename.get("to", ""))) if rename else None,
        ref=f"https://github.com/{repo}/issues/{number}#event-{e['id']}",
    )


def _comment(c: dict[str, Any]) -> RepoComment:
    return RepoComment(
        at=_at(c["created_at"]),
        number=int(str(c["issue_url"]).rsplit("/", 1)[1]),
        actor=_rest_actor(c.get("user"), c.get("performed_via_github_app")),
        body=str(c.get("body") or ""),
        ref=str(c["html_url"]),
    )


def _item(node: dict[str, Any]) -> tuple[ItemActivity, bool]:
    edits = node.get("userContentEdits") or {"totalCount": 0, "nodes": []}
    reviews = node.get("reviews") or {"totalCount": 0, "nodes": []}
    item = ItemActivity(
        number=int(node["number"]),
        is_pr=node["__typename"] == "PullRequest",
        created_at=_at(node["createdAt"]),
        author=_graph_actor(node.get("author")),
        edits=tuple(ContentEdit(_at(e["editedAt"]), _graph_actor(e.get("editor"))) for e in edits["nodes"]),
        reviews=tuple(
            Review(_at(r["submittedAt"]), str(r["state"]), _graph_actor(r.get("author")))
            for r in reviews["nodes"]
            if r.get("submittedAt")
        ),
        merged_at=_at(node["mergedAt"]) if node.get("mergedAt") else None,
        merged_by=_graph_actor(node["mergedBy"]) if node.get("mergedBy") else None,
        ref=str(node["url"]),
    )
    cut = edits["totalCount"] > len(edits["nodes"]) or reviews["totalCount"] > len(reviews["nodes"])
    return item, cut


__all__ = ["GitHubOperatorActivity"]
