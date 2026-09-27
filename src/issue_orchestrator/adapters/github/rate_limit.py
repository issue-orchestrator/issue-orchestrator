"""Recognise a GitHub rate-limit refusal and read when it resets (#7297).

GitHub refuses a rate-limited request with HTTP 403 or 429 — the same 403 it
uses for a missing permission — so the status alone says nothing. What does:

* ``retry-after: <seconds>`` — a secondary (abuse) limit; wait that long;
* ``x-ratelimit-remaining: 0`` with ``x-ratelimit-reset: <epoch>`` — the
  primary per-token budget is spent until that instant;
* a body naming a rate limit with neither header — GitHub documents that the
  caller should then wait at least one minute.

GraphQL can also answer HTTP 200 with an error of type ``RATE_LIMITED``.

Every GitHub HTTP chokepoint builds its failure through
:func:`github_http_failure`, so a rate limit surfaces as the typed
:class:`GitHubRateLimitedError` on every path rather than as a generic
``GitHubHttpError`` whose text a caller would have to sniff.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from ...domain.host_rate_limit import HostRateLimit
from .errors import (
    GitHubHttpError,
    GitHubRateLimitedError,
    GitHubRateLimitedScanIncompleteError,
    GitHubScanIncompleteError,
)

#: GitHub's documented floor when a secondary limit names no retry time.
UNTIMED_RATE_LIMIT_WAIT = timedelta(minutes=1)

_RATE_LIMIT_STATUSES = frozenset({403, 429})


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _header_int(headers: Mapping[str, str], name: str) -> int | None:
    raw = headers.get(name)
    if raw is None:
        return None
    raw = raw.strip()
    return int(raw) if raw.isdigit() else None


def _not_already_past(resets_at: datetime, now: datetime) -> datetime:
    """A reset at or before ``now`` (clock skew, ``retry-after: 0``) still waits.

    Otherwise the window would read as closed the instant it opened and the
    next tick would ask GitHub again - the very retry loop this exists to stop.
    """
    return resets_at if resets_at > now else now + UNTIMED_RATE_LIMIT_WAIT


def github_rate_limit(
    status_code: int,
    headers: Mapping[str, str],
    body: str,
    *,
    now: datetime,
) -> HostRateLimit | None:
    """The rate limit a REST/GraphQL HTTP response reports, or ``None``.

    ``headers`` must be case-insensitive (``httpx.Headers``), as GitHub's are.
    """
    if status_code not in _RATE_LIMIT_STATUSES:
        return None
    resource = headers.get("x-ratelimit-resource")
    retry_after = _header_int(headers, "retry-after")
    if retry_after is not None:
        return HostRateLimit(
            resets_at=_not_already_past(now + timedelta(seconds=retry_after), now),
            kind="secondary",
            resource=resource,
        )
    reset = _header_int(headers, "x-ratelimit-reset")
    if _header_int(headers, "x-ratelimit-remaining") == 0 and reset is not None:
        return HostRateLimit(
            resets_at=_not_already_past(datetime.fromtimestamp(reset, UTC), now),
            kind="primary",
            resource=resource,
        )
    lowered = body.lower()
    if status_code == 403 and "rate limit" not in lowered:
        # A 403 that names no rate limit is a genuine refusal (a missing
        # scope, a blocked resource); it stays an ordinary HTTP error. A 429
        # is "too many requests" by definition, whatever its body says.
        return None
    return HostRateLimit(
        resets_at=now + UNTIMED_RATE_LIMIT_WAIT,
        kind="secondary" if "secondary" in lowered else "primary",
        resource=resource,
    )


def graphql_rate_limit(
    errors: Iterable[Any],
    headers: Mapping[str, str],
    *,
    now: datetime,
) -> HostRateLimit | None:
    """The rate limit a GraphQL HTTP-200 error list reports, or ``None``."""
    if not any(
        isinstance(item, dict) and item.get("type") == "RATE_LIMITED"
        for item in errors
    ):
        return None
    reset = _header_int(headers, "x-ratelimit-reset")
    return HostRateLimit(
        resets_at=(
            _not_already_past(datetime.fromtimestamp(reset, UTC), now)
            if reset is not None
            else now + UNTIMED_RATE_LIMIT_WAIT
        ),
        kind="primary",
        resource=headers.get("x-ratelimit-resource") or "graphql",
    )


def github_http_failure(
    message: str,
    *,
    status_code: int,
    headers: Mapping[str, str],
    response_text: str,
    method: str,
    url: str,
    scan_incomplete: bool = False,
    now: Callable[[], datetime] = _utc_now,
) -> GitHubHttpError:
    """The one constructor for a failed GitHub HTTP response.

    A rate-limited response becomes :class:`GitHubRateLimitedError` carrying
    its reset; anything else stays the ordinary error type. ``scan_incomplete``
    keeps an exhaustive pager's fail-loud contract either way.
    """
    fields: dict[str, Any] = {
        "method": method,
        "url": url,
        "status_code": status_code,
        "response_text": response_text,
    }
    rate_limit = github_rate_limit(status_code, headers, response_text, now=now())
    if rate_limit is None:
        if scan_incomplete:
            return GitHubScanIncompleteError(message, **fields)
        return GitHubHttpError(message, **fields)
    if scan_incomplete:
        return GitHubRateLimitedScanIncompleteError(
            message, rate_limit=rate_limit, **fields
        )
    return GitHubRateLimitedError(message, rate_limit=rate_limit, **fields)


__all__ = [
    "UNTIMED_RATE_LIMIT_WAIT",
    "github_http_failure",
    "github_rate_limit",
    "graphql_rate_limit",
]
