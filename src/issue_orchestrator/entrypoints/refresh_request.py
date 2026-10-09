"""The one way an engine route requests a refresh (#8222).

Both ``/api/refresh`` routers — the dashboard's and ``control_app``'s — read
the same optional body and hand it to ``Orchestrator.request_refresh``, which
takes the state lock a running tick holds. Keeping that here gives the two one
body parser and one off-loop, uncancellable path (see ``engine_custody``).
"""

from __future__ import annotations

import json
from functools import partial
from typing import Protocol

from fastapi import Request

from .engine_custody import off_loop


class RefreshableEngine(Protocol):
    def request_refresh(self, inflight_stable_ids: set[str] | None = None) -> None: ...


async def inflight_stable_ids(request: Request) -> set[str]:
    """Issue IDs a caller expects the refresh to discover.

    Optional JSON body ``{"inflight_stable_ids": [...]}``: if those issues are
    missing after a cached refresh, the engine retries uncached to ride out
    GitHub's eventual consistency. A malformed body means an empty set.
    """
    try:
        body = await request.body()
        if not body:
            return set()
        data = json.loads(body)
    except (json.JSONDecodeError, ValueError):
        return set()
    ids = data.get("inflight_stable_ids") if isinstance(data, dict) else None
    return {str(i) for i in ids} if isinstance(ids, list) else set()


async def request_refresh(request: Request, engine: RefreshableEngine) -> None:
    """Parse the body and request the refresh off the event loop."""
    ids = await inflight_stable_ids(request)
    await off_loop(partial(engine.request_refresh, inflight_stable_ids=ids))


__all__ = ["inflight_stable_ids", "request_refresh"]
