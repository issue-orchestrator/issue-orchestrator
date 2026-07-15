"""Route introspection helpers for FastAPI apps under test.

FastAPI 0.139 stopped flattening ``include_router`` calls into the parent app.
``app.routes`` now yields an opaque ``_IncludedRouter`` per included router, and
the concrete routes are resolved lazily via ``effective_candidates()``. Tests
that assert "this path is registered exactly once" therefore cannot iterate
``app.routes`` directly -- the wrappers have no ``.path``.

FastAPI exposes no public API for flattening a mounted app back into concrete
routes (``app.openapi()`` is public but deduplicates by path, which would defeat
the double-registration guardrails these helpers exist to support). So this
module is the *single* place allowed to reach into FastAPI's private route
internals. If a future FastAPI release changes that shape again, fix it here and
every route guardrail in the suite follows.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Iterator
from typing import Any

from fastapi import FastAPI
from fastapi.routing import _IncludedRouter  # noqa: SLF001  # see module docstring


def _walk(routes: Iterable[Any]) -> Iterator[str]:
    for route in routes:
        if isinstance(route, _IncludedRouter):
            # Recurses: effective_candidates() may itself yield nested
            # _IncludedRouter branches for routers included into routers.
            yield from _walk(route.effective_candidates())
            continue
        path = getattr(route, "path", None)
        if path is not None:
            yield path


def iter_route_paths(app: FastAPI) -> Iterator[str]:
    """Yield the fully-prefixed path of every concrete route reachable in ``app``.

    Paths are yielded once per registration, so duplicates surface as repeats
    rather than being collapsed.
    """
    yield from _walk(app.routes)


def route_path_counts(app: FastAPI, paths: Iterable[str]) -> Counter[str]:
    """Count how many times each path in ``paths`` is registered on ``app``.

    Restricting to the caller's expected ``paths`` keeps assertions readable:
    the result compares equal to ``Counter({path: 1 for path in paths})`` when
    every path is registered exactly once.
    """
    wanted = set(paths)
    return Counter(path for path in iter_route_paths(app) if path in wanted)
