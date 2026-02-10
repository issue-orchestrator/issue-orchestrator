"""E2E GitHub audit report utilities."""

import json
import logging
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

logger = logging.getLogger(__name__)


def fetch_gh_audit_report(port: int | None) -> dict | None:
    """Fetch the GH audit report from the control API."""
    if port is None or port <= 0:
        return None

    for attempt in range(3):
        try:
            payload: dict | None = None
            error: Exception | None = None

            def _fetch() -> None:
                nonlocal payload, error
                try:
                    req = urllib.request.Request(
                        f"http://localhost:{port}/api/gh_audit_report",
                        data=b"{}",
                        method="POST",
                        headers={"Content-Type": "application/json"},
                    )
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        payload = json.loads(resp.read().decode("utf-8"))
                except Exception as exc:
                    error = exc

            thread = threading.Thread(target=_fetch, daemon=True)
            thread.start()
            thread.join(timeout=6)
            if thread.is_alive():
                logger.info("[E2E] GH audit report fetch timed out (hard)")
                if attempt == 2:
                    return None
                time.sleep(1 + attempt)
                continue
            if error:
                raise error
            if payload is None:
                raise RuntimeError("Empty GH audit payload")
            break
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            logger.info("[E2E] GH audit report fetch failed: %s", exc)
            if attempt == 2:
                return None
            time.sleep(1 + attempt)

    path = payload.get("path") if isinstance(payload, dict) else None  # type: ignore - Union type narrowing limitation
    if not path:
        return None
    try:
        return json.loads(Path(path).read_text())
    except OSError as exc:
        logger.info("[E2E] GH audit report read failed: %s", exc)
        return None


def usage_units_from_report(report: dict) -> int:
    """Get total usage units from a GH audit report."""
    return int(report.get("usage_units", 0))


def calls_from_report(report: dict) -> int:
    """Get total calls from a GH audit report."""
    return int(report.get("total_calls", 0))


def scope_usage(report: dict, scope: str) -> int:
    """Get usage units for a specific scope from a GH audit report."""
    totals = report.get("by_scope_totals") or {}
    entry = totals.get(scope) or {}
    return int(entry.get("usage_units", 0))


def scope_calls(report: dict, scope: str) -> int:
    """Get call count for a specific scope from a GH audit report."""
    totals = report.get("by_scope_totals") or {}
    entry = totals.get(scope) or {}
    return int(entry.get("calls", 0))


def delta_counts(before: dict | None, after: dict | None, key: str) -> dict[str, int]:
    """Calculate delta counts between two reports for a given key."""
    before_map = (before or {}).get(key) or {}
    after_map = (after or {}).get(key) or {}
    deltas: dict[str, int] = {}
    for name, count in after_map.items():
        try:
            after_count = int(count)
        except (TypeError, ValueError):
            after_count = 0
        try:
            before_count = int(before_map.get(name, 0))
        except (TypeError, ValueError):
            before_count = 0
        delta = after_count - before_count
        if delta:
            deltas[str(name)] = delta
    return deltas


def log_top_deltas(label: str, deltas: dict[str, int], limit: int = 5) -> None:
    """Log the top N deltas for a given category."""
    if not deltas:
        return
    top = sorted(deltas.items(), key=lambda kv: kv[1], reverse=True)[:limit]
    logger.info("[E2E] GH activity %s: %s", label, top)
