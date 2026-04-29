"""Inflight refresh tracking for e2e tests.

Tracks issues that are being created during a test so we can trigger
targeted refreshes via the control API, rather than waiting for
the normal queue refresh cycle.
"""

import json
import logging
import os
import socket
import time as _time
import urllib.error
import urllib.request

from issue_orchestrator.domain.issue_key import IssueKey
from issue_orchestrator.infra.api_token import TOKEN_ENV_VAR, read_existing_token

logger = logging.getLogger(__name__)


def control_api_headers() -> dict[str, str]:
    """Return Control API auth headers for e2e helper requests."""
    token = os.environ.get(TOKEN_ENV_VAR) or read_existing_token()
    if token:
        return {"Authorization": f"Bearer {token}"}
    return {}


def find_free_port() -> int:
    """Find an available localhost port for the control API."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def trigger_refresh(
    port: int | None = None,
    timeout: int = 5,
    inflight_stable_ids: set[str] | None = None,
) -> bool:
    """Trigger orchestrator to refresh issues immediately via control API.

    Args:
        port: Control API port (defaults to Config.control_api_port)
        timeout: Request timeout in seconds
        inflight_stable_ids: Optional set of issue stable IDs that must be discovered

    Returns True if refresh was requested successfully.
    """
    from issue_orchestrator.infra.config import Config

    # Retry a few times with backoff - the control API might still be starting
    max_retries = 5
    if port is None:
        env_port = os.environ.get("E2E_CONTROL_API_PORT")
        port = int(env_port) if env_port is not None else Config().control_api_port
    if port <= 0:
        logger.info("[E2E] Control API disabled; relying on queue refresh")
        return False

    # Prepare request body with inflight IDs if provided
    body: bytes | None = None
    headers: dict[str, str] = control_api_headers()
    if inflight_stable_ids:
        payload = {"inflight_stable_ids": sorted(inflight_stable_ids)}
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
        logger.info("[E2E] Refresh with %d inflight IDs: %s",
                   len(inflight_stable_ids), sorted(inflight_stable_ids))

    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(
                f"http://localhost:{port}/api/refresh",
                method="POST",
                data=body,
                headers=headers,
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                if resp.status == 200:
                    logger.info("[E2E] Refresh triggered successfully")
                    return True
                return False
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < max_retries - 1:
                wait = 1 * (attempt + 1)  # 1s, 2s, 3s, 4s backoff
                logger.info("[E2E] Refresh attempt %d failed (%s), retrying in %ds...",
                           attempt + 1, e, wait)
                _time.sleep(wait)
            else:
                logger.warning("[E2E] Failed to trigger refresh after %d attempts: %s",
                              max_retries, e)
                return False
    return False


class _InflightRefreshTracker:
    """Tracks issues that need refresh before assertions."""

    def __init__(self) -> None:
        self._pending: set[str] = set()

    def reset(self) -> None:
        self._pending.clear()

    def register(self, issue_key: str) -> None:
        self._pending.add(issue_key)

    def ensure_refreshed(self, port: int | None) -> None:
        if not self._pending:
            return
        pending = set(self._pending)
        self._pending.clear()
        logger.info("[E2E] Triggering refresh for %d inflight issue(s): %s",
                   len(pending), sorted(pending))
        if not trigger_refresh(port, inflight_stable_ids=pending):
            self._pending.update(pending)


_inflight_refresh_tracker = _InflightRefreshTracker()


def register_inflight_issue(issue: IssueKey) -> None:
    """Record an inflight issue that requires a refresh when waiting."""
    _inflight_refresh_tracker.register(issue.stable_id())


def ensure_inflight_refresh(port: int | None) -> None:
    """Trigger a single refresh if inflight issues are pending."""
    _inflight_refresh_tracker.ensure_refreshed(port)


def reset_inflight_tracker() -> None:
    """Reset the tracker (called per-test)."""
    _inflight_refresh_tracker.reset()


def get_control_api_port() -> int | None:
    """Get control API port from environment or None."""
    env_port = os.environ.get("E2E_CONTROL_API_PORT")
    if env_port is not None:
        return int(env_port)
    return None
