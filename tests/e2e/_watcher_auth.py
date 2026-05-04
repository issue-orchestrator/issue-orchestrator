"""Single owner for the e2e watcher's auth-aware client construction.

The orchestrator's loopback API guards ``/api/events``,
``/api/snapshot`` and ``/api/events_since`` behind
``Authorization: Bearer <token>``. ``tests/e2e/flows.py`` and
``tests/e2e/conftest.py`` both build watcher clients and both need to
attach that bearer; left as duplicated call sites the auth-wiring
policy drifts (one path picks the right helper, the other regresses
to ``read_existing_token`` and silently 401s).

Routing both call sites through this helper means the contract has
one owner: change the helper and both watcher paths follow.
Coverage in ``tests/unit/test_asyncdsl_http_auth.py`` exercises this
helper directly so a regression here fails one test, not "every e2e
that talks to the SSE endpoint."
"""

from __future__ import annotations

from issue_orchestrator.infra.api_token import read_existing_admin_token
from issue_orchestrator.testing.asyncdsl import (
    HTTPReplayProvider,
    HTTPSnapshotProvider,
    SSEEventStream,
)


def build_watcher_clients(
    port: int,
    *,
    auth_token: str | None = None,
) -> tuple[SSEEventStream, HTTPSnapshotProvider, HTTPReplayProvider]:
    """Build the SSE / snapshot / replay clients with consistent auth.

    Resolves the admin token via ``read_existing_admin_token()``
    (env-first, falling back to the on-disk token file) when the caller
    doesn't supply one, matching the server-side
    ``resolve_api_token()`` precedence. Tests can pass an explicit
    ``auth_token`` to override the resolution.
    """
    token = auth_token if auth_token is not None else read_existing_admin_token()
    base = f"http://localhost:{port}"
    return (
        SSEEventStream(f"{base}/api/events", auth_token=token),
        HTTPSnapshotProvider(f"{base}/api/snapshot", auth_token=token),
        HTTPReplayProvider(f"{base}/api/events_since", auth_token=token),
    )
