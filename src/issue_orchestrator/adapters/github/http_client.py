"""HTTP GitHub client for orchestrator operations (sync httpx)."""

from __future__ import annotations

import json as _json
import logging
import time
from dataclasses import dataclass
from typing import Any, cast
from urllib.parse import quote

import httpx

from ...events import EventName
from ...infra import gh_audit
from ... import __version__
from .errors import GitHubAuthError, GitHubHttpError, GitHubTransportError
from .tokens import (
    KEYRING_SERVICE,
    KEYRING_USERNAME,
    TokenValidationResult,
    clear_keyring_token,
    describe_github_token_sources,
    resolve_github_token,
    store_keyring_token,
)

logger = logging.getLogger(__name__)


@dataclass
class GitHubRateLimitSnapshot:
    core_remaining: int | None
    core_limit: int | None
    core_reset: int | None
    search_remaining: int | None
    search_limit: int | None
    search_reset: int | None
    graphql_remaining: int | None
    graphql_limit: int | None
    graphql_reset: int | None

    def to_payload(self) -> dict[str, Any]:
        return {
            "core": {
                "remaining": self.core_remaining,
                "limit": self.core_limit,
                "reset": self.core_reset,
            },
            "search": {
                "remaining": self.search_remaining,
                "limit": self.search_limit,
                "reset": self.search_reset,
            },
            "graphql": {
                "remaining": self.graphql_remaining,
                "limit": self.graphql_limit,
                "reset": self.graphql_reset,
            },
        }


@dataclass
class GitHubHttpConfig:
    repo: str
    token: str
    base_url: str = "https://api.github.com"
    timeout_seconds: float = 20.0


@dataclass
class _ETagEntry:
    etag: str
    payload: Any


def _truncate_one_line(text: str, max_len: int) -> str:
    snippet = text.strip().replace("\n", " ")
    return snippet[:max_len] + ("..." if len(snippet) > max_len else "")


def _format_error_entry(err: object) -> str:
    """Format a single entry from a GitHub `errors[]` array."""
    if isinstance(err, dict):
        bits = [
            str(err[k])
            for k in ("resource", "field", "code", "message")
            if err.get(k)
        ]
        return "/".join(bits)
    return str(err) if err else ""


def _format_error_details(errors: object) -> str:
    if not isinstance(errors, list):
        return ""
    chunks = [c for c in (_format_error_entry(e) for e in errors) if c]
    return "[" + "; ".join(chunks) + "]" if chunks else ""


def _summarize_github_error(response_text: str, max_len: int = 280) -> str:
    """Extract GitHub's `message` and `errors[]` codes from a response body.

    GitHub error bodies look like:
        {"message": "Validation Failed",
         "errors": [{"resource": "PullRequest", "code": "invalid", "field": "state"}],
         "documentation_url": "..."}

    Returns a compact one-line summary suitable for embedding in an exception
    message or surfacing in a UI toast. Falls back to a truncated raw body
    when JSON parsing fails or fields are absent.
    """
    if not response_text:
        return ""
    try:
        body = _json.loads(response_text)
    except (ValueError, TypeError):
        return _truncate_one_line(response_text, max_len)
    if not isinstance(body, dict):
        return str(body)[:max_len]
    parts = [
        str(body.get("message") or "").strip(),
        _format_error_details(body.get("errors")),
    ]
    summary = " ".join(p for p in parts if p)
    if not summary:
        return _truncate_one_line(response_text, max_len)
    return _truncate_one_line(summary, max_len)


def _extract_rate_limit_headers(response: httpx.Response) -> dict[str, Any] | None:
    """Extract X-RateLimit-* headers from GitHub response.

    Returns a dict with remaining, limit, used, reset if available.
    """
    remaining = response.headers.get("X-RateLimit-Remaining")
    limit = response.headers.get("X-RateLimit-Limit")
    used = response.headers.get("X-RateLimit-Used")
    reset = response.headers.get("X-RateLimit-Reset")
    resource = response.headers.get("X-RateLimit-Resource")

    if remaining is None and limit is None:
        return None

    result: dict[str, Any] = {}
    if remaining is not None:
        result["remaining"] = int(remaining)
    if limit is not None:
        result["limit"] = int(limit)
    if used is not None:
        result["used"] = int(used)
    if reset is not None:
        result["reset"] = int(reset)
    if resource is not None:
        result["resource"] = resource
    return result if result else None


class GitHubHttpClient:
    """Minimal GitHub REST client for issue-orchestrator."""

    def __init__(self, config: GitHubHttpConfig) -> None:
        self._config = config
        self._etag_cache: dict[str, _ETagEntry] = {}
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {config.token}",
            "User-Agent": f"issue-orchestrator/{__version__}",
        }
        self._client = httpx.Client(
            base_url=config.base_url,
            headers=headers,
            timeout=config.timeout_seconds,
        )

    @property
    def config(self) -> GitHubHttpConfig:
        """Expose config for use by adapters that need to create temporary clients."""
        return self._config

    def close(self) -> None:
        self._client.close()

    def _cache_key(self, method: str, url: str, params: dict[str, Any] | None) -> str:
        if not params:
            return f"{method} {url}"
        ordered = "&".join(f"{k}={params[k]}" for k in sorted(params))
        return f"{method} {url}?{ordered}"

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        use_cache: bool = True,
        caller: str = "github_http",
    ) -> Any:
        url = path
        headers: dict[str, str] = {}
        cache_key = self._cache_key(method, url, params)
        if use_cache and method.upper() == "GET":
            cached = self._etag_cache.get(cache_key)
            if cached:
                headers["If-None-Match"] = cached.etag

        start = time.monotonic()
        error: str | None = None
        response_text = ""
        status_code = None
        payload: Any = None
        was_304 = False
        rate_limit_info: dict[str, int] | None = None
        try:
            try:
                response = self._client.request(
                    method,
                    url,
                    params=params,
                    json=json_body,
                    headers=headers or None,
                )
            except (httpx.TimeoutException, httpx.HTTPError, OSError) as exc:
                error = f"transport_error: {exc}"
                raise GitHubTransportError(
                    f"GitHub transport error for {method} {url}",
                    method=method,
                    url=url,
                    original=exc,
                ) from exc
            status_code = response.status_code
            response_text = response.text
            # Extract rate limit headers from every response
            rate_limit_info = _extract_rate_limit_headers(response)
            if status_code == 304 and use_cache:
                cached = self._etag_cache.get(cache_key)
                if cached is not None:
                    payload = cached.payload
                    was_304 = True
                    return payload
            if status_code >= 400:
                error = f"{status_code} {response_text.strip()}"
                summary = _summarize_github_error(response_text)
                detail = f" — {summary}" if summary else ""
                raise GitHubHttpError(
                    (
                        f"GitHub {method.upper()} {path} failed: "
                        f"{status_code}{detail}"
                    ),
                    method=method,
                    url=str(response.url),
                    status_code=status_code,
                    response_text=response_text,
                )
            if response_text:
                payload = response.json()
            else:
                payload = {}
            if use_cache and method.upper() == "GET":
                etag = response.headers.get("ETag")
                if etag:
                    self._etag_cache[cache_key] = _ETagEntry(etag=etag, payload=payload)
            return payload
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            # 304 Not Modified: no data was transferred, so items_returned=0
            items_count = 0 if was_304 else _count_items(payload)
            gh_audit.record_live_call(
                command=f"{method.upper()} {url}",
                caller=caller,
                error=error,
                rate_limit=rate_limit_info,
            )
            gh_audit.record(
                args=[method.upper(), url],
                repo=self._config.repo,
                duration_ms=duration_ms,
                error=error,
                caller=caller,
                bytes_returned=len(response_text.encode("utf-8")) if response_text else 0,
                items_returned=items_count,
                full_scan=_is_full_scan(method, path),
                rate_limit=rate_limit_info,
            )

    def _graphql(
        self,
        query: str,
        variables: dict[str, Any] | None = None,
        *,
        caller: str = "graphql",
    ) -> dict[str, Any]:
        """Execute a GraphQL query or mutation.

        Args:
            query: The GraphQL query or mutation string.
            variables: Variables to pass to the query.
            caller: Identifier for audit logging.

        Returns:
            The parsed JSON response containing 'data' and optionally 'errors'.

        Raises:
            GitHubHttpError: If the request fails or returns GraphQL errors.
        """
        json_body: dict[str, Any] = {"query": query}
        if variables:
            json_body["variables"] = variables

        start = time.monotonic()
        error: str | None = None
        response_text = ""
        status_code: int | None = None
        payload: dict[str, Any] = {}
        response: httpx.Response | None = None
        try:
            try:
                response = self._client.request(
                    "POST",
                    "/graphql",
                    json=json_body,
                )
            except (httpx.TimeoutException, httpx.HTTPError, OSError) as exc:
                error = f"transport_error: {exc}"
                raise GitHubTransportError(
                    "GitHub GraphQL transport error",
                    method="POST",
                    url="/graphql",
                    original=exc,
                ) from exc

            status_code = response.status_code
            response_text = response.text

            if status_code >= 400:
                error = f"{status_code} {response_text.strip()}"
                raise GitHubHttpError(
                    f"GitHub GraphQL request failed: {status_code}",
                    method="POST",
                    url="/graphql",
                    status_code=status_code,
                    response_text=response_text,
                )

            payload = response.json()

            # Check for GraphQL-level errors
            if "errors" in payload and payload["errors"]:
                error_messages = [e.get("message", str(e)) for e in payload["errors"]]
                error = f"GraphQL errors: {error_messages}"
                raise GitHubHttpError(
                    f"GitHub GraphQL error: {error_messages[0]}",
                    method="POST",
                    url="/graphql",
                    status_code=status_code,
                    response_text=response_text,
                )

            return payload
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            rate_limit_info = _extract_rate_limit_headers(response) if response is not None else None
            gh_audit.record_live_call(
                command="POST /graphql",
                caller=caller,
                error=error,
                rate_limit=rate_limit_info,
            )
            gh_audit.record(
                args=["POST", "/graphql", caller],
                repo=self._config.repo,
                duration_ms=duration_ms,
                error=error,
                caller=caller,
                bytes_returned=len(response_text.encode("utf-8")) if response_text else 0,
                items_returned=1 if payload.get("data") else 0,
                full_scan=False,
                rate_limit=rate_limit_info,
            )

    # -------------------- Issues --------------------

    def list_issues(
        self,
        *,
        labels: list[str] | None = None,
        state: str = "open",
        milestone: str | None = None,
        limit: int = 100,
        use_cache: bool = True,
    ) -> list[dict[str, Any]]:
        """List issues matching the given criteria.

        Args:
            labels: Filter by issues that have all of these labels.
            state: Filter by issue state. Can be "open", "closed", or "all".
            milestone: Filter by milestone title.
            limit: Maximum number of issues to return.
            use_cache: If True (default), use ETag cache. If False, bypass cache
                and force a fresh request (used when required IDs are missing).
        """
        params: dict[str, Any] = {
            "state": state,
            "per_page": min(100, max(1, limit)),
        }
        if labels:
            params["labels"] = ",".join(labels)
        if milestone:
            milestone_number = self._get_milestone_number(milestone)
            if milestone_number is not None:
                params["milestone"] = milestone_number
        payload = self._request_json(
            "GET",
            f"/repos/{self._config.repo}/issues",
            params=params,
            caller="list_issues",
            use_cache=use_cache,
        )
        if not isinstance(payload, list):
            return []
        issues = [item for item in payload if "pull_request" not in item]
        return issues[:limit]

    def list_issues_since(
        self,
        *,
        since: str,
        state: str = "all",
        limit: int = 100,
        use_cache: bool = False,
    ) -> tuple[list[dict[str, Any]], str | None]:
        """List issues updated since a watermark using repo-wide delta feed.

        Returns:
            (issues, next_watermark_hint) where next watermark is the oldest
            updated_at value from the fetched batch to avoid skipping updates
            when results are truncated by limit.
        """
        per_page = min(100, max(1, limit))
        page = 1
        collected: list[dict[str, Any]] = []
        oldest_updated_at: str | None = None

        while len(collected) < limit:
            params: dict[str, Any] = {
                "state": state,
                "sort": "updated",
                "direction": "desc",
                "since": since,
                "per_page": per_page,
                "page": page,
            }
            payload = self._request_json(
                "GET",
                f"/repos/{self._config.repo}/issues",
                params=params,
                caller="list_issues_delta",
                use_cache=use_cache,
            )
            if not isinstance(payload, list) or not payload:
                break

            for item in payload:
                if not isinstance(item, dict):
                    continue
                if "pull_request" in item:
                    continue
                collected.append(item)
                updated_at = item.get("updated_at")
                if isinstance(updated_at, str):
                    if oldest_updated_at is None or updated_at < oldest_updated_at:
                        oldest_updated_at = updated_at
                if len(collected) >= limit:
                    break

            if len(payload) < per_page:
                break
            page += 1

        return collected[:limit], oldest_updated_at

    def get_issue(self, issue_number: int) -> dict[str, Any] | None:
        payload = self._request_json(
            "GET",
            f"/repos/{self._config.repo}/issues/{issue_number}",
            caller="get_issue",
        )
        return payload if isinstance(payload, dict) else None

    def create_issue(
        self,
        title: str,
        body: str,
        labels: list[str] | None = None,
        milestone: int | None = None,
    ) -> dict[str, Any] | None:
        """Create a new issue.

        Returns the full issue data including number and html_url.
        """
        json_body: dict[str, Any] = {"title": title, "body": body, "labels": labels or []}
        if milestone is not None:
            json_body["milestone"] = milestone

        payload = self._request_json(
            "POST",
            f"/repos/{self._config.repo}/issues",
            json_body=json_body,
            use_cache=False,
            caller="create_issue",
        )
        if isinstance(payload, dict):
            return payload
        return None

    def add_label(self, issue_number: int, label: str) -> None:
        self._request_json(
            "POST",
            f"/repos/{self._config.repo}/issues/{issue_number}/labels",
            json_body={"labels": [label]},
            use_cache=False,
            caller="add_label",
        )

    def remove_label(self, issue_number: int, label: str) -> None:
        encoded = quote(label, safe="")
        self._request_json(
            "DELETE",
            f"/repos/{self._config.repo}/issues/{issue_number}/labels/{encoded}",
            use_cache=False,
            caller="remove_label",
        )

    def get_issue_labels(self, issue_number: int, *, use_cache: bool = True) -> list[str]:
        payload = self._request_json(
            "GET",
            f"/repos/{self._config.repo}/issues/{issue_number}/labels",
            params={"per_page": 100},
            use_cache=use_cache,
            caller="get_issue_labels",
        )
        if isinstance(payload, list):
            names: list[str] = []
            for label in payload:
                if not isinstance(label, dict):
                    continue
                name = label.get("name")
                if isinstance(name, str):
                    names.append(name)
            return names
        return []

    def list_labels(self) -> list[dict[str, Any]]:
        payload = self._request_json(
            "GET",
            f"/repos/{self._config.repo}/labels",
            params={"per_page": 100},
            caller="list_labels",
        )
        return payload if isinstance(payload, list) else []

    def list_all_labels(self) -> list[dict[str, Any]]:
        """Fetch all labels with pagination (for cleanup operations).

        Unlike list_labels() which returns only the first page,
        this fetches all pages. Does not use ETag caching.
        """
        all_labels: list[dict[str, Any]] = []
        page = 1
        while True:
            try:
                response = self._client.get(
                    f"/repos/{self._config.repo}/labels",
                    params={"per_page": 100, "page": page},
                )
            except (httpx.TimeoutException, httpx.HTTPError, OSError) as exc:
                raise GitHubTransportError(
                    "GitHub transport error for list_all_labels",
                    method="GET",
                    url=f"/repos/{self._config.repo}/labels",
                    original=exc,
                ) from exc
            if response.status_code != 200:
                break
            batch = response.json()
            if not batch:
                break
            all_labels.extend(batch)
            # Check for next page via Link header or empty response
            if len(batch) < 100:
                break
            page += 1
            if page > 20:  # Safety limit
                break
        return all_labels

    def invalidate_labels_etag(self) -> None:
        """Invalidate ETag cache for the labels endpoint.

        Call after POST/PATCH/DELETE on repo labels to ensure
        subsequent GETs fetch fresh data.
        """
        url = f"/repos/{self._config.repo}/labels"
        key = self._cache_key("GET", url, {"per_page": 100})  # Match list_labels params
        self._etag_cache.pop(key, None)

    def invalidate_pr_etag(self, pr_number: int) -> None:
        """Invalidate ETag cache for a PR endpoint."""
        url = f"/repos/{self._config.repo}/pulls/{pr_number}"
        key = self._cache_key("GET", url, None)
        self._etag_cache.pop(key, None)

    def list_milestones(self, state: str = "open") -> list[dict[str, Any]]:
        """List milestones in the repository.

        Args:
            state: Filter by milestone state ('open', 'closed', 'all')

        Returns:
            List of milestone dictionaries.
        """
        payload = self._request_json(
            "GET",
            f"/repos/{self._config.repo}/milestones",
            params={"state": state, "per_page": 100},
            caller="list_milestones",
        )
        return payload if isinstance(payload, list) else []

    def create_milestone(
        self,
        title: str,
        description: str | None = None,
        due_on: str | None = None,
        state: str = "open",
    ) -> dict[str, Any] | None:
        """Create a milestone."""
        json_body: dict[str, Any] = {"title": title, "state": state}
        if description:
            json_body["description"] = description
        if due_on:
            json_body["due_on"] = due_on
        payload = self._request_json(
            "POST",
            f"/repos/{self._config.repo}/milestones",
            json_body=json_body,
            use_cache=False,
            caller="create_milestone",
        )
        return payload if isinstance(payload, dict) else None

    def create_label(
        self,
        name: str,
        *,
        color: str = "ededed",
        description: str | None = None,
        force: bool = False,
    ) -> None:
        body = {"name": name, "color": color}
        if description:
            body["description"] = description
        try:
            self._request_json(
                "POST",
                f"/repos/{self._config.repo}/labels",
                json_body=body,
                use_cache=False,
                caller="create_label",
            )
        except GitHubHttpError as exc:
            if exc.status_code == 422 and force:
                encoded = quote(name, safe="")
                self._request_json(
                    "PATCH",
                    f"/repos/{self._config.repo}/labels/{encoded}",
                    json_body=body,
                    use_cache=False,
                    caller="update_label",
                )
                return
            if exc.status_code == 422:
                return
            raise

    def delete_label(self, name: str) -> None:
        encoded = quote(name, safe="")
        self._request_json(
            "DELETE",
            f"/repos/{self._config.repo}/labels/{encoded}",
            use_cache=False,
            caller="delete_label",
        )

    def add_comment(self, issue_number: int, body: str) -> str:
        payload = self._request_json(
            "POST",
            f"/repos/{self._config.repo}/issues/{issue_number}/comments",
            json_body={"body": body},
            use_cache=False,
            caller="add_comment",
        )
        if isinstance(payload, dict):
            return payload.get("html_url", f"https://github.com/{self._config.repo}/issues/{issue_number}")
        return f"https://github.com/{self._config.repo}/issues/{issue_number}"

    def get_issue_comments(
        self,
        issue_number: int,
        *,
        use_cache: bool = True,
    ) -> list[dict[str, Any]]:
        payload = self._request_json(
            "GET",
            f"/repos/{self._config.repo}/issues/{issue_number}/comments",
            params={"per_page": 100},
            caller="get_issue_comments",
            use_cache=use_cache,
        )
        return payload if isinstance(payload, list) else []

    # -------------------- Git refs / commits --------------------

    def get_default_branch(self) -> str:
        payload = self._request_json(
            "GET",
            f"/repos/{self._config.repo}",
            caller="get_default_branch",
        )
        if not isinstance(payload, dict):
            raise GitHubHttpError("GitHub repository payload was not an object")
        default_branch = payload.get("default_branch")
        if not isinstance(default_branch, str) or not default_branch:
            raise GitHubHttpError("GitHub repository payload missing default_branch")
        return default_branch

    def get_git_ref(self, ref: str) -> dict[str, Any] | None:
        encoded = quote(_api_ref_path(ref), safe="/")
        try:
            payload = self._request_json(
                "GET",
                f"/repos/{self._config.repo}/git/ref/{encoded}",
                use_cache=False,
                caller="get_git_ref",
            )
        except GitHubHttpError as exc:
            if exc.status_code == 404:
                return None
            raise
        return payload if isinstance(payload, dict) else None

    def create_git_ref(self, *, ref: str, sha: str) -> dict[str, Any]:
        payload = self._request_json(
            "POST",
            f"/repos/{self._config.repo}/git/refs",
            json_body={"ref": ref, "sha": sha},
            use_cache=False,
            caller="create_git_ref",
        )
        if not isinstance(payload, dict):
            raise GitHubHttpError("GitHub create ref payload was not an object")
        return payload

    def update_git_ref(self, *, ref: str, sha: str, force: bool = False) -> dict[str, Any]:
        encoded = quote(_api_ref_path(ref), safe="/")
        payload = self._request_json(
            "PATCH",
            f"/repos/{self._config.repo}/git/refs/{encoded}",
            json_body={"sha": sha, "force": force},
            use_cache=False,
            caller="update_git_ref",
        )
        if not isinstance(payload, dict):
            raise GitHubHttpError("GitHub update ref payload was not an object")
        return payload

    def delete_git_ref(self, ref: str) -> None:
        encoded = quote(_api_ref_path(ref), safe="/")
        self._request_json(
            "DELETE",
            f"/repos/{self._config.repo}/git/refs/{encoded}",
            use_cache=False,
            caller="delete_git_ref",
        )

    def get_git_commit(self, sha: str) -> dict[str, Any]:
        encoded = quote(sha, safe="")
        payload = self._request_json(
            "GET",
            f"/repos/{self._config.repo}/git/commits/{encoded}",
            use_cache=False,
            caller="get_git_commit",
        )
        if not isinstance(payload, dict):
            raise GitHubHttpError("GitHub commit payload was not an object")
        return payload

    def create_git_commit(
        self,
        *,
        message: str,
        tree_sha: str,
        parents: list[str],
    ) -> dict[str, Any]:
        payload = self._request_json(
            "POST",
            f"/repos/{self._config.repo}/git/commits",
            json_body={"message": message, "tree": tree_sha, "parents": parents},
            use_cache=False,
            caller="create_git_commit",
        )
        if not isinstance(payload, dict):
            raise GitHubHttpError("GitHub create commit payload was not an object")
        return payload

    def update_issue_state(self, issue_number: int, state: str) -> None:
        self._request_json(
            "PATCH",
            f"/repos/{self._config.repo}/issues/{issue_number}",
            json_body={"state": state},
            use_cache=False,
            caller="update_issue_state",
        )

    def update_issue_milestone(self, issue_number: int, milestone: int | None) -> dict[str, Any] | None:
        payload = self._request_json(
            "PATCH",
            f"/repos/{self._config.repo}/issues/{issue_number}",
            json_body={"milestone": milestone},
            use_cache=False,
            caller="update_issue_milestone",
        )
        return payload if isinstance(payload, dict) else None

    # -------------------- PRs --------------------

    def get_pr(self, pr_number: int) -> dict[str, Any] | None:
        payload = self._request_json(
            "GET",
            f"/repos/{self._config.repo}/pulls/{pr_number}",
            caller="get_pr",
        )
        return payload if isinstance(payload, dict) else None

    def get_pr_status_check_rollup(self, pr_number: int) -> str | None:
        """Fetch the aggregated status-check rollup for a PR's head commit.

        Returns one of GitHub's `StatusState` values (`SUCCESS`, `FAILURE`,
        `PENDING`, `EXPECTED`, `ERROR`) or `None` if the PR / commit has no
        rollup (no checks configured). Used by the awaiting-merge reconciler
        to disambiguate `mergeable_state=unstable|blocked` between
        "checks running" and "check actually failed".
        """
        owner, repo = self._config.repo.split("/", 1)
        query = """
        query($owner: String!, $repo: String!, $number: Int!) {
            repository(owner: $owner, name: $repo) {
                pullRequest(number: $number) {
                    commits(last: 1) {
                        nodes {
                            commit {
                                statusCheckRollup { state }
                            }
                        }
                    }
                }
            }
        }
        """
        result = self._graphql(
            query,
            {"owner": owner, "repo": repo, "number": pr_number},
            caller="get_pr_status_check_rollup",
        )
        pr_data = (
            result.get("data", {}).get("repository", {}).get("pullRequest")
        )
        if not pr_data:
            return None
        nodes = (pr_data.get("commits") or {}).get("nodes") or []
        if not nodes:
            return None
        commit = nodes[0].get("commit") or {}
        rollup = commit.get("statusCheckRollup")
        if not rollup:
            return None
        state = rollup.get("state")
        return state if isinstance(state, str) else None

    def list_prs(self, *, state: str = "open", limit: int = 100) -> list[dict[str, Any]]:
        payload = self._request_json(
            "GET",
            f"/repos/{self._config.repo}/pulls",
            params={"state": state, "per_page": min(100, max(1, limit))},
            caller="list_prs",
        )
        if not isinstance(payload, list):
            return []
        return payload[:limit]

    def close_pr(self, pr_number: int) -> None:
        self._request_json(
            "PATCH",
            f"/repos/{self._config.repo}/pulls/{pr_number}",
            json_body={"state": "closed"},
            use_cache=False,
            caller="close_pr",
        )

    def list_branches(self) -> list[str]:
        payload = self._request_json(
            "GET",
            f"/repos/{self._config.repo}/branches",
            params={"per_page": 100},
            caller="list_branches",
        )
        if not isinstance(payload, list):
            return []
        names = []
        for item in payload:
            if isinstance(item, dict):
                name = item.get("name")
                if name:
                    names.append(name)
        return names

    def delete_branch(self, branch: str) -> None:
        encoded = quote(branch, safe="")
        self._request_json(
            "DELETE",
            f"/repos/{self._config.repo}/git/refs/heads/{encoded}",
            use_cache=False,
            caller="delete_branch",
        )

    def branch_exists(self, branch: str) -> bool:
        encoded = quote(branch, safe="")
        try:
            self._request_json(
                "GET",
                f"/repos/{self._config.repo}/git/refs/heads/{encoded}",
                use_cache=False,
                caller="branch_exists",
            )
            return True
        except GitHubHttpError as exc:
            if exc.status_code == 404:
                return False
            raise

    def get_prs_for_branch(self, branch: str, state: str = "open") -> list[dict[str, Any]]:
        owner = self._config.repo.split("/")[0]
        payload = self._request_json(
            "GET",
            f"/repos/{self._config.repo}/pulls",
            params={"head": f"{owner}:{branch}", "state": state, "per_page": 100},
            caller="get_prs_for_branch",
        )
        return payload if isinstance(payload, list) else []

    def _valid_pr_search_items(
        self,
        payload: dict[str, Any],
        label: str,
        state: str,
    ) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for item in _search_items(payload):
            number = item.get("number")
            if not isinstance(number, int):
                logger.warning(
                    "Skipping malformed PR search item: label=%s state=%s url=%s",
                    label,
                    state,
                    item.get("html_url") or item.get("url"),
                )
                gh_audit.emit_event(EventName.GH_SEARCH_ITEM_MALFORMED, {
                    "label": label,
                    "state": state,
                    "item": {
                        "html_url": item.get("html_url"),
                        "url": item.get("url"),
                        "title": item.get("title"),
                    },
                })
                continue
            items.append(item)
        return items

    def get_prs_with_label(self, label: str, state: str = "open") -> list[dict[str, Any]]:
        if state == "all":
            items: list[dict[str, Any]] = []
            seen: set[int] = set()
            for st in ("open", "closed"):
                query = f"repo:{self._config.repo} is:pr label:{label} state:{st}"
                payload = self._request_json(
                    "GET",
                    "/search/issues",
                    params={"q": query, "per_page": 100},
                    caller="get_prs_with_label",
                )
                for item in self._valid_pr_search_items(payload, label=label, state=st):
                    number = cast(int, item.get("number"))
                    if number in seen:
                        continue
                    seen.add(number)
                    items.append(item)
            return items
        query = f"repo:{self._config.repo} is:pr label:{label} state:{state}"
        payload = self._request_json(
            "GET",
            "/search/issues",
            params={"q": query, "per_page": 100},
            caller="get_prs_with_label",
        )
        return self._valid_pr_search_items(payload, label=label, state=state)

    def get_prs_with_label_graphql(
        self,
        label: str,
        *,
        state: str = "open",
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Fetch PRs with a label using GraphQL (single request, includes head branch).

        Unlike get_prs_with_label (search API + N individual get_pr calls),
        this returns full PR info including head.ref in 1-2 GraphQL requests.

        Returns dicts with keys matching the REST PR API shape (number, title,
        html_url, head.ref, body, state, labels, draft) so _pr_info_from_api
        can parse them directly.
        """
        # Map state filter to GraphQL enum values
        states: list[str]
        if state == "all":
            states = ["OPEN", "CLOSED", "MERGED"]
        elif state == "closed":
            states = ["CLOSED", "MERGED"]
        else:
            states = [state.upper()]

        owner, repo_name = self._config.repo.split("/", 1)

        query = """
        query($owner: String!, $repo: String!, $label: String!, $states: [PullRequestState!], $first: Int!, $after: String) {
          repository(owner: $owner, name: $repo) {
            pullRequests(labels: [$label], states: $states, first: $first, after: $after) {
              pageInfo { hasNextPage endCursor }
              nodes {
                number
                title
                url
                headRefName
                body
                state
                isDraft
                labels(first: 20) {
                  nodes { name }
                }
              }
            }
          }
        }
        """
        all_prs: list[dict[str, Any]] = []
        cursor: str | None = None
        remaining = limit

        while remaining > 0:
            batch = min(remaining, 100)
            variables: dict[str, Any] = {
                "owner": owner,
                "repo": repo_name,
                "label": label,
                "states": states,
                "first": batch,
            }
            if cursor:
                variables["after"] = cursor

            result = self._graphql(query, variables, caller="get_prs_with_label_graphql")
            data = result.get("data", {})
            repo_data = data.get("repository", {})
            prs_data = repo_data.get("pullRequests", {})
            nodes = prs_data.get("nodes", [])

            for node in nodes:
                # Reshape to match REST API format for _pr_info_from_api
                labels = [{"name": l["name"]} for l in (node.get("labels", {}).get("nodes", []))]
                pr_dict: dict[str, Any] = {
                    "number": node["number"],
                    "title": node.get("title", ""),
                    "html_url": node.get("url", ""),
                    "head": {"ref": node.get("headRefName", "")},
                    "body": node.get("body", "") or "",
                    "state": node.get("state", "OPEN").lower(),
                    "labels": labels,
                    "draft": node.get("isDraft"),
                }
                all_prs.append(pr_dict)

            page_info = prs_data.get("pageInfo", {})
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
            remaining -= len(nodes)

        return all_prs

    def get_prs_for_issue(self, issue_number: int) -> list[dict[str, Any]]:
        # GitHub's search API rejects queries without an `is:` qualifier with
        # 422 ("Query must include 'is:issue' or 'is:pull-request'"). The
        # parens are required so `OR` binds within the disjunction rather than
        # across the `is:pr` qualifier.
        query = (
            f"repo:{self._config.repo} is:pr "
            f"(head:{issue_number} OR #{issue_number})"
        )
        payload = self._request_json(
            "GET",
            "/search/issues",
            params={"q": query, "per_page": 100},
            caller="get_prs_for_issue",
        )
        return _search_items(payload)

    def search_issues_by_title(
        self,
        query_terms: list[str],
        *,
        limit: int = 30,
        use_cache: bool = True,
    ) -> list[dict[str, Any]]:
        """Search issues whose title contains any of the given quoted terms.

        Terms are OR'd inside parens; `in:title` scopes the match to titles;
        `is:issue` is required for fine-grained PATs (search API otherwise
        returns 422). Returns raw search result items; caller filters.
        """
        if not query_terms:
            return []
        quoted = " OR ".join(f'"{t}"' for t in query_terms)
        query = f"repo:{self._config.repo} is:issue ({quoted}) in:title"
        payload = self._request_json(
            "GET",
            "/search/issues",
            params={"q": query, "per_page": min(100, max(1, limit))},
            caller="search_issues_by_title",
            use_cache=use_cache,
        )
        return _search_items(payload)

    def get_pr_reviews(self, pr_number: int) -> list[dict[str, Any]]:
        """Get all reviews on a pull request.

        Args:
            pr_number: The PR number to get reviews for.

        Returns:
            List of review dicts with 'state', 'body', 'user' etc.
        """
        payload = self._request_json(
            "GET",
            f"/repos/{self._config.repo}/pulls/{pr_number}/reviews",
            params={"per_page": 100},
            caller="get_pr_reviews",
        )
        return payload if isinstance(payload, list) else []

    def create_pr(
        self,
        title: str,
        body: str,
        head: str,
        base: str = "main",
        draft: bool | None = None,
    ) -> dict[str, Any] | None:
        body_payload: dict[str, Any] = {"title": title, "body": body, "head": head, "base": base}
        if draft is not None:
            body_payload["draft"] = draft
        payload = self._request_json(
            "POST",
            f"/repos/{self._config.repo}/pulls",
            json_body=body_payload,
            use_cache=False,
            caller="create_pr",
        )
        return payload if isinstance(payload, dict) else None

    def set_pr_draft(self, pr_number: int, draft: bool) -> dict[str, Any] | None:
        """Set draft status on a pull request using GraphQL.

        The REST API does not support changing draft status, so we must use
        GraphQL mutations: markPullRequestReadyForReview or convertPullRequestToDraft.
        """
        owner, repo = self._config.repo.split("/")

        # First, get the PR's GraphQL node ID
        node_id_query = """
        query($owner: String!, $repo: String!, $number: Int!) {
            repository(owner: $owner, name: $repo) {
                pullRequest(number: $number) {
                    id
                }
            }
        }
        """
        result = self._graphql(
            node_id_query,
            {"owner": owner, "repo": repo, "number": pr_number},
            caller="set_pr_draft_get_id",
        )
        pr_data = result.get("data", {}).get("repository", {}).get("pullRequest")
        if not pr_data:
            raise GitHubHttpError(
                f"PR #{pr_number} not found",
                method="POST",
                url="/graphql",
                status_code=200,
                response_text=str(result),
            )
        node_id = pr_data["id"]

        # Use the appropriate mutation based on desired draft status
        if draft:
            mutation = """
            mutation($pullRequestId: ID!) {
                convertPullRequestToDraft(input: {pullRequestId: $pullRequestId}) {
                    pullRequest {
                        id
                        number
                        isDraft
                    }
                }
            }
            """
            mutation_name = "convertPullRequestToDraft"
        else:
            mutation = """
            mutation($pullRequestId: ID!) {
                markPullRequestReadyForReview(input: {pullRequestId: $pullRequestId}) {
                    pullRequest {
                        id
                        number
                        isDraft
                    }
                }
            }
            """
            mutation_name = "markPullRequestReadyForReview"

        result = self._graphql(
            mutation,
            {"pullRequestId": node_id},
            caller="set_pr_draft_mutate",
        )
        mutation_result = result.get("data", {}).get(mutation_name, {})
        return mutation_result.get("pullRequest")

    # -------------------- Sub-issues (GraphQL) --------------------

    def get_issue_node_id(self, issue_number: int) -> str | None:
        """Get the GraphQL node ID for an issue.

        Args:
            issue_number: Issue number

        Returns:
            Node ID string (e.g., "I_kwDOGK5N...") or None if not found
        """
        query = """
        query($owner: String!, $repo: String!, $number: Int!) {
            repository(owner: $owner, name: $repo) {
                issue(number: $number) {
                    id
                }
            }
        }
        """
        owner, repo = self._config.repo.split("/")
        try:
            result = self._graphql(
                query,
                {"owner": owner, "repo": repo, "number": issue_number},
                caller="get_issue_node_id",
            )
            issue = result.get("data", {}).get("repository", {}).get("issue")
            if issue:
                return issue.get("id")
            return None
        except GitHubHttpError:
            return None

    def add_sub_issue(
        self,
        parent_node_id: str,
        child_issue_number: int,
    ) -> bool:
        """Link an issue as a sub-issue of a parent.

        Uses GitHub's sub-issues API (addSubIssue mutation).

        Args:
            parent_node_id: GraphQL node ID of the parent issue
            child_issue_number: Issue number of the child to link

        Returns:
            True if linked successfully, False otherwise
        """
        # First get the child's node ID
        child_node_id = self.get_issue_node_id(child_issue_number)
        if not child_node_id:
            logger.warning(
                "[github] Could not get node ID for issue #%d",
                child_issue_number,
            )
            return False

        mutation = """
        mutation($parentId: ID!, $childId: ID!) {
            addSubIssue(input: {issueId: $parentId, subIssueId: $childId}) {
                issue {
                    id
                    number
                }
                subIssue {
                    id
                    number
                }
            }
        }
        """
        try:
            result = self._graphql(
                mutation,
                {"parentId": parent_node_id, "childId": child_node_id},
                caller="add_sub_issue",
            )
            add_result = result.get("data", {}).get("addSubIssue")
            if add_result and add_result.get("subIssue"):
                logger.info(
                    "[github] Linked issue #%d as sub-issue of parent",
                    child_issue_number,
                )
                return True
            return False
        except GitHubHttpError as e:
            logger.warning(
                "[github] Failed to link sub-issue #%d: %s",
                child_issue_number,
                e,
            )
            return False

    # -------------------- Rate limits --------------------

    def get_rate_limit_snapshot(self) -> GitHubRateLimitSnapshot | None:
        payload = self._request_json("GET", "/rate_limit", caller="rate_limit", use_cache=False)
        if not isinstance(payload, dict):
            return None
        resources = payload.get("resources", {})
        core = resources.get("core", {})
        search = resources.get("search", {})
        graphql = resources.get("graphql", {})
        return GitHubRateLimitSnapshot(
            core_remaining=core.get("remaining"),
            core_limit=core.get("limit"),
            core_reset=core.get("reset"),
            search_remaining=search.get("remaining"),
            search_limit=search.get("limit"),
            search_reset=search.get("reset"),
            graphql_remaining=graphql.get("remaining"),
            graphql_limit=graphql.get("limit"),
            graphql_reset=graphql.get("reset"),
        )

    def get_token_scopes(self) -> list[str]:
        """Return OAuth scopes for the configured token (if available)."""
        start = time.monotonic()
        error: str | None = None
        response_text = ""
        status_code = None
        scopes: list[str] = []
        try:
            response = self._client.request("GET", "/user")
            status_code = response.status_code
            response_text = response.text
            if status_code >= 400:
                error = f"{status_code} {response_text.strip()}"
                summary = _summarize_github_error(response_text)
                detail = f" — {summary}" if summary else ""
                raise GitHubHttpError(
                    f"GitHub GET /user failed: {status_code}{detail}",
                    method="GET",
                    url=str(response.url),
                    status_code=status_code,
                    response_text=response_text,
                )
            header = response.headers.get("X-OAuth-Scopes", "")
            scopes = [scope.strip() for scope in header.split(",") if scope.strip()]
            return scopes
        finally:
            duration_ms = int((time.monotonic() - start) * 1000)
            gh_audit.record(
                args=["GET", "/user"],
                repo=self._config.repo,
                duration_ms=duration_ms,
                error=error,
                caller="get_token_scopes",
                bytes_returned=len(response_text.encode("utf-8")) if response_text else 0,
                items_returned=0,
                full_scan=False,
            )

    # -------------------- Helpers --------------------

    def _get_milestone_number(self, title: str) -> int | None:
        payload = self._request_json(
            "GET",
            f"/repos/{self._config.repo}/milestones",
            params={"state": "all", "per_page": 100},
            caller="list_milestones",
        )
        if not isinstance(payload, list):
            return None
        for item in payload:
            if not isinstance(item, dict):
                continue
            if item.get("title") == title:
                return item.get("number")
        return None


def validate_github_token(
    token: str | None = None,
    *,
    configured_token: str | None = None,
    configured_env: str | None = None,
    configured_keyring_service: str | None = None,
    configured_keyring_username: str | None = None,
    repo: str | None = None,
    api_url: str = "https://api.github.com",
) -> TokenValidationResult:
    """Validate a GitHub token by calling the API.

    Token source resolution lives in tokens.py; validation stays here because
    this module owns GitHub HTTP transport and import-linter allows httpx here.
    """
    try:
        if token is None:
            token = resolve_github_token(
                configured_token=configured_token,
                configured_env=configured_env,
                configured_keyring_service=configured_keyring_service,
                configured_keyring_username=configured_keyring_username,
                api_url=api_url,
            )
    except GitHubAuthError as e:
        return TokenValidationResult(valid=False, error=str(e))

    base_url = api_url.rstrip("/")
    try:
        resp = httpx.get(
            f"{base_url}/user",
            headers={"Authorization": f"token {token}"},
            timeout=10.0,
        )
        if resp.status_code == 200:
            user_info = resp.json()
            username = user_info.get("login")
            if repo:
                repo_resp = httpx.get(
                    f"{base_url}/repos/{repo}",
                    headers={"Authorization": f"token {token}"},
                    timeout=10.0,
                )
                if repo_resp.status_code != 200:
                    return TokenValidationResult(
                        valid=False,
                        username=username,
                        error=(
                            f"Token cannot access repo {repo} "
                            f"(HTTP {repo_resp.status_code})"
                        ),
                    )
            return TokenValidationResult(
                valid=True,
                username=username,
            )
        else:
            return TokenValidationResult(
                valid=False,
                error=f"Token invalid (HTTP {resp.status_code})",
            )
    except Exception as e:
        return TokenValidationResult(valid=False, error=str(e))


def _api_ref_path(ref: str) -> str:
    if ref.startswith("refs/"):
        return ref.removeprefix("refs/")
    return ref


def _search_items(payload: Any) -> list[dict[str, Any]]:
    if not isinstance(payload, dict):
        return []
    items = payload.get("items", [])
    return items if isinstance(items, list) else []


def _count_items(payload: Any) -> int | None:
    if payload is None:
        return None
    if isinstance(payload, list):
        return len(payload)
    if isinstance(payload, dict):
        if "items" in payload and isinstance(payload["items"], list):
            return len(payload["items"])
        return 1
    return None


def _is_full_scan(method: str, path: str) -> bool:
    if method.upper() != "GET":
        return False
    return path.startswith("/repos/") and (path.endswith("/issues") or path.endswith("/pulls") or path.endswith("/milestones"))


__all__ = [
    "GitHubAuthError",
    "GitHubHttpClient",
    "GitHubHttpConfig",
    "GitHubHttpError",
    "GitHubRateLimitSnapshot",
    "GitHubTransportError",
    "KEYRING_SERVICE",
    "KEYRING_USERNAME",
    "TokenValidationResult",
    "clear_keyring_token",
    "describe_github_token_sources",
    "resolve_github_token",
    "store_keyring_token",
    "validate_github_token",
]
