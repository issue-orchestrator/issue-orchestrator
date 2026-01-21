"""Audit helper for GitHub API usage.

Enabled via ORCHESTRATOR_GH_AUDIT=1.
Writes JSON to ORCHESTRATOR_GH_AUDIT_FILE (or /tmp/gh-audit-<pid>.json).
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_ENABLED = os.environ.get("ORCHESTRATOR_GH_AUDIT") == "1"
_AUDIT_PATH = os.environ.get("ORCHESTRATOR_GH_AUDIT_FILE")
_INCLUDE_EVENTS = os.environ.get("ORCHESTRATOR_GH_AUDIT_EVENTS") == "1"

_lock = threading.Lock()
_stats: dict[str, Any] = {
    "total_calls": 0,
    "total_items_returned": 0,
    "total_bytes_returned": 0,
    "full_scan_calls": 0,
    "full_scan_items_returned": 0,
    "by_command": {},
    "by_caller": {},
    "by_caller_command": {},
    "by_reason": {},
    "by_reason_totals": {},
    "by_scope": {},
    "by_scope_totals": {},
    "by_reason_command": {},
    "by_scope_reason": {},
    "by_issue": {},
    "errors": 0,
    "events": [],
    "last_rate_limit": None,
    "last_rate_limit_checked_at": None,
    "last_rate_limit_from_headers": None,
}

_event_sink = None
_rate_limit_every_calls = 0
_rate_limit_warn_fraction = 0.1
_rate_limit_warn_remaining = 100
_rate_limit_lock = threading.Lock()
_rate_limit_fetcher = None
_call_id = 0
_calls: dict[int, dict[str, Any]] = {}
_context = threading.local()
_atexit_registered = False


def set_event_sink(sink) -> None:
    global _event_sink
    _event_sink = sink


def emit_event(name, payload: dict[str, Any]) -> None:
    if _event_sink is None:
        return
    try:
        from ..ports import TraceEvent
        _event_sink.publish(TraceEvent(name, payload))
    except Exception:
        logger.debug("Failed to emit GH audit event: %s", name, exc_info=True)


def set_rate_limit_fetcher(fetcher) -> None:
    """Provide a callback to fetch GitHub rate limit snapshots."""
    global _rate_limit_fetcher
    _rate_limit_fetcher = fetcher


def configure(
    *,
    enabled: bool | None = None,
    include_events: bool | None = None,
    audit_path: str | None = None,
) -> None:
    global _ENABLED, _INCLUDE_EVENTS, _AUDIT_PATH
    if enabled is not None:
        _ENABLED = bool(enabled)
        if _ENABLED:
            _ensure_atexit()
    if include_events is not None:
        _INCLUDE_EVENTS = bool(include_events)
    if audit_path is not None:
        _AUDIT_PATH = audit_path


def reset_stats() -> None:
    global _call_id
    with _lock:
        _stats.update({
            "total_calls": 0,
            "total_items_returned": 0,
            "total_bytes_returned": 0,
            "full_scan_calls": 0,
            "full_scan_items_returned": 0,
            "by_command": {},
            "by_caller": {},
            "by_caller_command": {},
            "by_reason": {},
            "by_reason_totals": {},
            "by_scope": {},
            "by_scope_totals": {},
            "by_reason_command": {},
            "by_scope_reason": {},
            "by_issue": {},
            "errors": 0,
            "events": [],
            "last_rate_limit": None,
            "last_rate_limit_checked_at": None,
        })
        _calls.clear()
        _call_id = 0


class AuditReason:
    QUEUE_REFRESH_SCHEDULED = "queue_refresh_scheduled"
    QUEUE_REFRESH_MANUAL = "queue_refresh_manual"
    SNAPSHOT_REFRESH = "snapshot_refresh"
    STARTUP_REFRESH = "startup_refresh"
    LABEL_SYNC_SCAN = "label_sync_scan"
    EXTERNAL_ID_RESOLVE = "external_id_resolve"
    PR_SCAN = "pr_scan"
    TEST_DATA_CREATE = "test_data_create"
    TEST_DATA_UPDATE = "test_data_update"
    TEST_DATA_CLOSE = "test_data_close"
    TEST_DATA_LIST = "test_data_list"
    TEST_DATA_LABEL = "test_data_label"
    GH_WRITE = "gh_write"
    GH_READ = "gh_read"


class AuditScope:
    STARTUP = "startup"
    PERIODIC = "periodic"
    MANUAL = "manual"
    TEST = "test"
    ON_DEMAND = "on_demand"
    UNKNOWN = "unknown"


class _AuditContext:
    def __init__(self, reason: str | None, issue_key: str | None, scope: str | None) -> None:
        self.reason = reason
        self.issue_key = issue_key
        self.scope = scope
        self.prev = None

    def __enter__(self):
        self.prev = getattr(_context, "current", None)
        _context.current = self
        return self

    def __exit__(self, exc_type, exc, tb):
        _context.current = self.prev


def context(*, reason: str | None, issue_key: str | None = None, scope: str | None = None) -> "_AuditContext":
    return _AuditContext(reason, issue_key, scope)


def _get_context() -> tuple[str | None, str | None, str]:
    ctx = getattr(_context, "current", None)
    if ctx is None:
        return None, None, AuditScope.UNKNOWN
    return ctx.reason, ctx.issue_key, ctx.scope or AuditScope.UNKNOWN


def configure_rate_limit(*, every_calls: int, warn_fraction: float, warn_remaining: int) -> None:
    global _rate_limit_every_calls, _rate_limit_warn_fraction, _rate_limit_warn_remaining
    _rate_limit_every_calls = max(0, int(every_calls))
    _rate_limit_warn_fraction = float(warn_fraction)
    _rate_limit_warn_remaining = int(warn_remaining)


def _extract_rate_limits(payload: dict[str, Any]) -> dict[str, Any]:
    resources = payload.get("resources", {})
    core = resources.get("core", {})
    search = resources.get("search", {})
    graphql = resources.get("graphql", {})
    return {
        "core": {
            "remaining": core.get("remaining"),
            "limit": core.get("limit"),
            "reset": core.get("reset"),
        },
        "search": {
            "remaining": search.get("remaining"),
            "limit": search.get("limit"),
            "reset": search.get("reset"),
        },
        "graphql": {
            "remaining": graphql.get("remaining"),
            "limit": graphql.get("limit"),
            "reset": graphql.get("reset"),
        },
    }


def _should_warn_rate_limit(snapshot: dict[str, Any]) -> bool:
    core = snapshot.get("core", {})
    remaining = core.get("remaining")
    limit = core.get("limit")
    if remaining is None or limit is None:
        return False
    if remaining <= _rate_limit_warn_remaining:
        return True
    if limit and remaining <= int(limit * _rate_limit_warn_fraction):
        return True
    return False


def check_rate_limit(reason: str) -> dict[str, Any] | None:
    with _rate_limit_lock:
        if _rate_limit_fetcher is None:
            logger.warning("GH rate limit fetcher not configured")
            return None
        try:
            payload = _rate_limit_fetcher()
        except Exception as exc:
            logger.warning("GH rate limit query failed: %s", exc)
            return None
        if payload is None:
            logger.warning("GH rate limit query returned no data")
            return None
        snapshot = _extract_rate_limits(payload)
        now = time.time()
        _stats["last_rate_limit"] = snapshot
        _stats["last_rate_limit_checked_at"] = now

    logger.info("[GH-RATE] %s core=%s search=%s graphql=%s", reason,
                snapshot.get("core"), snapshot.get("search"), snapshot.get("graphql"))

    if _event_sink is not None:
        try:
            from ..events import EventName
            from ..ports import TraceEvent
            _event_sink.publish(TraceEvent(EventName.GH_RATE_LIMIT, {
                "reason": reason,
                "snapshot": snapshot,
                "checked_at": now,
            }))
            if _should_warn_rate_limit(snapshot):
                _event_sink.publish(TraceEvent(EventName.GH_RATE_LIMIT_WARNING, {
                    "reason": reason,
                    "snapshot": snapshot,
                    "checked_at": now,
                }))
        except Exception:
            logger.debug("Failed to emit GH rate limit event", exc_info=True)

    return snapshot


def enabled() -> bool:
    return _ENABLED


def include_events() -> bool:
    """Query whether event recording is enabled."""
    return _INCLUDE_EVENTS


def get_audit_path() -> str | None:
    """Query the configured audit file path."""
    return _AUDIT_PATH


def get_stats() -> dict[str, Any]:
    """Get a copy of current audit statistics."""
    with _lock:
        return dict(_stats)


def get_rate_limit_config() -> dict[str, Any]:
    """Query current rate limit configuration."""
    return {
        "every_calls": _rate_limit_every_calls,
        "warn_fraction": _rate_limit_warn_fraction,
        "warn_remaining": _rate_limit_warn_remaining,
    }


def _command_key(args: list[str]) -> str:
    if not args:
        return "unknown"
    if len(args) >= 2:
        return f"{args[0]} {args[1]}"
    return args[0]


def _ensure_atexit() -> None:
    global _atexit_registered
    if _atexit_registered:
        return
    atexit.register(emit_report)
    _atexit_registered = True


def _ensure_totals_entry(container: dict[str, Any], key: str) -> dict[str, Any]:
    entry = container.get(key)
    if entry is None:
        entry = {
            "calls": 0,
            "items_returned": 0,
            "bytes_returned": 0,
            "full_scan_calls": 0,
            "full_scan_items_returned": 0,
            "total_ms": 0,
        }
        container[key] = entry
    return entry


def _record_basic_stats(command: str, caller: str, key: str, duration_ms: int) -> None:
    """Record basic call statistics."""
    _stats["total_calls"] += 1
    _stats["by_command"][command] = _stats["by_command"].get(command, 0) + 1
    _stats["by_caller"][caller] = _stats["by_caller"].get(caller, 0) + 1
    entry = _stats["by_caller_command"].setdefault(
        key, {"caller": caller, "command": command, "count": 0, "total_ms": 0}
    )
    entry["count"] += 1
    entry["total_ms"] += duration_ms


def _record_reason_stats(reason: str, command: str, duration_ms: int) -> None:
    """Record reason-based statistics."""
    _stats["by_reason"][reason] = _stats["by_reason"].get(reason, 0) + 1
    reason_totals = _ensure_totals_entry(_stats["by_reason_totals"], reason)
    reason_totals["calls"] += 1
    reason_totals["total_ms"] += duration_ms
    reason_key = f"{reason}::{command}"
    rc_entry = _stats["by_reason_command"].setdefault(
        reason_key, {"reason": reason, "command": command, "count": 0, "total_ms": 0}
    )
    rc_entry["count"] += 1
    rc_entry["total_ms"] += duration_ms


def _record_scope_stats(scope: str, reason: str | None, duration_ms: int) -> dict[str, Any]:
    """Record scope-based statistics. Returns sr_entry for further updates."""
    _stats["by_scope"][scope] = _stats["by_scope"].get(scope, 0) + 1
    scope_totals = _ensure_totals_entry(_stats["by_scope_totals"], scope)
    scope_totals["calls"] += 1
    scope_totals["total_ms"] += duration_ms
    scope_key = f"{scope}::{reason or 'unknown'}"
    sr_entry = _stats["by_scope_reason"].setdefault(
        scope_key,
        {"scope": scope, "reason": reason or "unknown", "count": 0, "total_ms": 0,
         "items_returned": 0, "bytes_returned": 0, "full_scan_calls": 0, "full_scan_items_returned": 0},
    )
    sr_entry["count"] += 1
    sr_entry["total_ms"] += duration_ms
    return sr_entry


def _add_to_stat(
    key: str, global_key: str, value: int, reason: str | None, scope: str | None, sr_entry: dict | None
) -> None:
    """Add value to stat across global, reason, scope, and sr_entry."""
    _stats[global_key] += value
    if reason and reason in _stats["by_reason_totals"]:
        _stats["by_reason_totals"][reason][key] += value
    if scope and scope in _stats["by_scope_totals"]:
        _stats["by_scope_totals"][scope][key] += value
        if sr_entry is not None:
            sr_entry[key] += value


def _record_metrics(
    reason: str | None, scope: str | None, sr_entry: dict | None,
    bytes_returned: int | None, items_returned: int | None, full_scan: bool | None,
) -> None:
    """Record bytes/items/full_scan metrics."""
    if bytes_returned is not None:
        _add_to_stat("bytes_returned", "total_bytes_returned", bytes_returned, reason, scope, sr_entry)

    if items_returned is not None:
        _add_to_stat("items_returned", "total_items_returned", items_returned, reason, scope, sr_entry)

    if full_scan:
        _stats["full_scan_calls"] += 1
        if reason and reason in _stats["by_reason_totals"]:
            _stats["by_reason_totals"][reason]["full_scan_calls"] += 1
        if scope and scope in _stats["by_scope_totals"]:
            _stats["by_scope_totals"][scope]["full_scan_calls"] += 1
            if sr_entry is not None:
                sr_entry["full_scan_calls"] += 1
        if items_returned is not None:
            _add_to_stat("full_scan_items_returned", "full_scan_items_returned", items_returned, reason, scope, sr_entry)


def record(
    *,
    args: list[str],
    repo: str | None,
    duration_ms: int,
    error: str | None,
    caller: str,
    bytes_returned: int | None = None,
    items_returned: int | None = None,
    full_scan: bool | None = None,
    rate_limit: dict[str, int] | None = None,
) -> None:
    if not _ENABLED:
        return
    command = _command_key(args)
    key = f"{caller}::{command}"
    reason, issue_key, scope = _get_context()
    with _lock:
        global _call_id
        _call_id += 1
        call_id = _call_id

        _record_basic_stats(command, caller, key, duration_ms)

        if reason:
            _record_reason_stats(reason, command, duration_ms)

        sr_entry = None
        if scope:
            sr_entry = _record_scope_stats(scope, reason, duration_ms)

        if issue_key:
            _stats["by_issue"][issue_key] = _stats["by_issue"].get(issue_key, 0) + 1
        if error:
            _stats["errors"] += 1

        _record_metrics(reason, scope, sr_entry, bytes_returned, items_returned, full_scan)

        if rate_limit is not None:
            _stats["last_rate_limit_from_headers"] = {**rate_limit, "updated_at": time.time()}

        if _INCLUDE_EVENTS:
            _stats["events"].append({
                "call_id": call_id, "ts": time.time(), "caller": caller, "command": command,
                "args": list(args), "repo": repo, "duration_ms": duration_ms, "error": error,
                "reason": reason, "scope": scope, "issue_key": issue_key,
                "items_returned": items_returned, "bytes_returned": bytes_returned, "full_scan": bool(full_scan),
            })

        _calls[call_id] = {
            "items_returned": items_returned, "bytes_returned": bytes_returned,
            "full_scan": full_scan, "reason": reason, "scope": scope,
        }
        total_calls = _stats["total_calls"]
        _context.last_call_id = call_id

    if _rate_limit_every_calls and total_calls % _rate_limit_every_calls == 0:
        check_rate_limit(f"every_{_rate_limit_every_calls}_calls")


def _update_metric_delta(
    stat_key: str, global_key: str, reason: str | None, scope: str | None, delta: int, scope_key: str | None
) -> None:
    """Update a metric across global, reason, and scope stats."""
    _stats[global_key] += delta
    if reason and reason in _stats["by_reason_totals"]:
        _stats["by_reason_totals"][reason][stat_key] += delta
    if scope and scope in _stats["by_scope_totals"]:
        _stats["by_scope_totals"][scope][stat_key] += delta
        if scope_key:
            sr_entry = _stats["by_scope_reason"].get(scope_key)
            if sr_entry is not None:
                sr_entry[stat_key] += delta


def update_last_call(*, items_returned: int | None = None, bytes_returned: int | None = None) -> None:
    if not _ENABLED:
        return
    call_id = getattr(_context, "last_call_id", None)
    if call_id is None:
        return
    with _lock:
        entry = _calls.get(call_id)
        if entry is None:
            return
        reason = entry.get("reason")
        scope = entry.get("scope")
        scope_key = f"{scope}::{reason or 'unknown'}" if scope else None

        if items_returned is not None:
            prev_items = entry.get("items_returned") or 0
            delta = items_returned - prev_items
            entry["items_returned"] = items_returned
            _update_metric_delta("items_returned", "total_items_returned", reason, scope, delta, scope_key)

        if bytes_returned is not None:
            prev_bytes = entry.get("bytes_returned") or 0
            delta = bytes_returned - prev_bytes
            entry["bytes_returned"] = bytes_returned
            _update_metric_delta("bytes_returned", "total_bytes_returned", reason, scope, delta, scope_key)

        if entry.get("full_scan") and items_returned is not None:
            prev_full = entry.get("items_returned") or 0
            delta = items_returned - prev_full
            _update_metric_delta("full_scan_items_returned", "full_scan_items_returned", reason, scope, delta, scope_key)


def _summary_lines() -> list[str]:
    with _lock:
        total = _stats["total_calls"]
        errors = _stats["errors"]
        by_caller = sorted(_stats["by_caller"].items(), key=lambda kv: kv[1], reverse=True)
        by_cmd = sorted(_stats["by_command"].items(), key=lambda kv: kv[1], reverse=True)
        total_items = _stats["total_items_returned"]
        total_bytes = _stats["total_bytes_returned"]
        full_scans = _stats["full_scan_calls"]
        full_items = _stats["full_scan_items_returned"]
        # Slowest phases (scopes) by total_ms - per architecture objective
        scope_totals = _stats.get("by_scope_totals", {})
        slowest_phases = sorted(
            [(scope, entry.get("total_ms", 0)) for scope, entry in scope_totals.items()],
            key=lambda x: x[1],
            reverse=True,
        )
    lines = [
        f"[GH-AUDIT] total_calls={total} errors={errors} items={total_items} bytes={total_bytes} full_scans={full_scans} full_items={full_items}",
        f"[GH-AUDIT] top_callers={by_caller[:5]}",
        f"[GH-AUDIT] top_commands={by_cmd[:5]}",
        f"[GH-AUDIT] slowest_phases={slowest_phases[:5]}",
    ]
    return lines


def _resolve_audit_path() -> str:
    path = _AUDIT_PATH or f"/tmp/gh-audit-{os.getpid()}.json"
    if "{pid}" in path:
        path = path.replace("{pid}", str(os.getpid()))
    return path


def emit_report() -> str | None:
    if not _ENABLED:
        return None
    path = _resolve_audit_path()
    with _lock:
        data = dict(_stats)
        data["usage_units"] = data.get("total_calls", 0) + int(data.get("total_items_returned", 0))
        for entry in (data.get("by_scope_totals") or {}).values():
            entry["usage_units"] = entry.get("calls", 0) + int(entry.get("items_returned", 0))
        for entry in (data.get("by_reason_totals") or {}).values():
            entry["usage_units"] = entry.get("calls", 0) + int(entry.get("items_returned", 0))
        for entry in (data.get("by_scope_reason") or {}).values():
            entry["usage_units"] = entry.get("count", 0) + int(entry.get("items_returned", 0))
    try:
        with open(path, "w") as fh:
            json.dump(data, fh, indent=2, sort_keys=True)
    except Exception as exc:
        logger.warning("GH audit write failed: %s", exc)
    for line in _summary_lines():
        try:
            logger.info(line)
        except Exception:
            pass
        print(line)
    return path


def get_rate_limit_snapshot() -> dict[str, Any] | None:
    with _lock:
        return _stats.get("last_rate_limit")


def get_rate_limit_checked_at() -> float | None:
    with _lock:
        return _stats.get("last_rate_limit_checked_at")


if _ENABLED:
    _ensure_atexit()
