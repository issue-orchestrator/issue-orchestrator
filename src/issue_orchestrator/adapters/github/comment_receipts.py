"""Fresh paginated comment receipts with authenticated user/app provenance."""
from __future__ import annotations

import hashlib
from collections.abc import Callable
from typing import Any

from ...ports.comment_receipt import IssueCommentReceipt
from .errors import GitHubScanIncompleteError

_MAX_PAGES = 100


def _positive_id(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _author_key(comment: dict[str, Any], user_id: int | None,
    app_identity: tuple[str | None, str | None] | None) -> str | None:
    user = comment.get("user")
    if not isinstance(user, dict) or not _positive_id(user.get("id")):
        raise GitHubScanIncompleteError("Comment publication has malformed author identity")
    if app_identity is None:
        return f"github-user:{user_id}" if user["id"] == user_id else None
    app = comment.get("performed_via_github_app")
    if not isinstance(app, dict) or user.get("type") != "Bot":
        return None
    app_id, client_id = app_identity
    if app_id is not None:
        return f"github-app:{app_id}" if str(app.get("id")) == app_id else None
    return f"github-app-client:{client_id}" if app.get("client_id") == client_id else None


def _receipt(comment: dict[str, Any], *, body: str, author: str) -> IssueCommentReceipt:
    if not _positive_id(comment.get("id")) or not isinstance(comment.get("html_url"), str) or not comment["html_url"]:
        raise GitHubScanIncompleteError("Comment receipt identity is malformed")
    return IssueCommentReceipt(str(comment["id"]), comment["html_url"], author,
        hashlib.sha256(body.encode()).hexdigest())


def find_comment_receipt(*, request: Callable[..., Any], repo: str,
    issue_number: int, body: str,
    app_identity: tuple[str | None, str | None] | None) -> IssueCommentReceipt | None:
    user_id = None
    if app_identity is None:
        actor = request("GET", "/user", caller="comment_receipt_author", use_cache=False)
        if not isinstance(actor, dict) or not _positive_id(actor.get("id")):
            raise GitHubScanIncompleteError("Authenticated comment author response is malformed")
        user_id = actor["id"]
    for page in range(1, _MAX_PAGES + 1):
        comments = request("GET", f"/repos/{repo}/issues/{issue_number}/comments",
            params={"per_page": 100, "page": page}, caller="find_issue_comment_receipt", use_cache=False)
        if not isinstance(comments, list):
            raise GitHubScanIncompleteError("Comment receipt scan returned a malformed page")
        for comment in comments:
            if not isinstance(comment, dict) or not isinstance(comment.get("body"), str):
                raise GitHubScanIncompleteError("Comment receipt scan returned a malformed comment")
            if comment["body"] != body:
                continue
            author = _author_key(comment, user_id, app_identity)
            if author is None:
                continue
            return _receipt(comment, body=body, author=author)
        if len(comments) < 100:
            return None
    raise GitHubScanIncompleteError("Comment receipt scan exceeded its page bound")
