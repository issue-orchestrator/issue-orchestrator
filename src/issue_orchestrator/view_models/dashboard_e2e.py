"""E2E dashboard status and card builders."""

from __future__ import annotations

from datetime import datetime, timezone
import copy
import logging
import threading
import time
from typing import Any

from ..infra.e2e_runner import get_e2e_runner_manager, get_next_run_info

logger = logging.getLogger(__name__)

E2E_PAGE_SIZE = 15
E2E_STATUS_CACHE_TTL_SECONDS = 1.5
_E2E_STATUS_CACHE: dict[str, tuple[float, dict[str, Any]]] = {}
_E2E_STATUS_CACHE_LOCK = threading.Lock()


def _e2e_status_cache_key(config: Any) -> str:
    return f"{str(config.repo_root)}::{config.orchestrator_id}"


def invalidate_e2e_status_cache(config: Any) -> None:
    key = _e2e_status_cache_key(config)
    with _E2E_STATUS_CACHE_LOCK:
        _E2E_STATUS_CACHE.pop(key, None)


def _relative_time(dt_str: str) -> str:
    """Convert ISO timestamp to relative time like '2h ago'."""
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        delta = now - dt

        if delta.days > 0:
            return f"{delta.days}d ago"
        hours = delta.seconds // 3600
        if hours > 0:
            return f"{hours}h ago"
        minutes = delta.seconds // 60
        return f"{minutes}m ago" if minutes > 0 else "just now"
    except (ValueError, TypeError):
        return ""


def _build_e2e_running_items(e2e_status: dict[str, Any]) -> list[dict[str, Any]]:
    if not e2e_status.get("running"):
        return []
    return [{
        "issue_number": "E2E-running",
        "title": "E2E Run in Progress",
        "status": "running",
        "detail_label": "Tests are executing...",
        "action": "stop",
        "action_hint": "Click to stop E2E run",
        "is_e2e": True,
        "e2e_running": True,
        "time": "now",
    }]


def _build_e2e_attention_items(e2e_status: dict[str, Any]) -> list[dict[str, Any]]:
    if not (e2e_status.get("needs_attention") and e2e_status.get("untriaged_count", 0) > 0):
        return []
    untriaged = e2e_status["untriaged_count"]
    last_run = e2e_status.get("last_run", {})
    run_id = last_run.get("id", "?")
    failed_tests_data = []
    failed_tests = e2e_status.get("failed_tests", [])
    for ft in failed_tests:
        nodeid = ft.get("nodeid", "")
        short_name = nodeid.split("::")[-1] if "::" in nodeid else nodeid
        failed_tests_data.append({
            "nodeid": nodeid,
            "short_name": short_name,
            "outcome": ft.get("outcome", "failed"),
            "duration": ft.get("duration_seconds"),
        })
    return [{
        "issue_number": f"E2E-{run_id}",
        "title": f"Action needed: {untriaged} failed test{'s' if untriaged != 1 else ''}",
        "status": "needs_attention",
        "status_label": "Action needed",
        "detail_label": f"{untriaged} test{'s' if untriaged != 1 else ''} failed without a linked issue",
        "action": "triage",
        "action_hint": "Click to open triage modal",
        "is_e2e": True,
        "e2e_failed_tests": failed_tests_data,
        "e2e_run_id": run_id,
        "results_action": _e2e_run_results_action(run_id),
        "relative_time": last_run.get("relative_time", ""),
        "time": last_run.get("relative_time", ""),
    }]


def _build_e2e_open_run_issue_items(db: Any) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for run_issue in db.get_open_run_issues():
        sub_issues = db.get_failure_issues_for_parent(run_issue.github_issue_number)
        if not sub_issues:
            continue
        resolved = sum(1 for s in sub_issues if s.resolved_at)
        total = len(sub_issues)
        pct = int((resolved / total * 100)) if total > 0 else 0
        sub_issues_data = []
        for si in sub_issues:
            short_name = si.nodeid.split("::")[-1] if "::" in si.nodeid else si.nodeid
            sub_issues_data.append({
                "issue_number": si.github_issue_number,
                "nodeid": si.nodeid,
                "short_name": short_name,
                "status": "resolved" if si.resolved_at else "open",
                "resolved_at": si.resolved_at,
            })
        run_issue_number = getattr(run_issue, "github_issue_number", None)
        run_issue_title = getattr(run_issue, "title", "") or ""
        run_issue_url = getattr(run_issue, "github_issue_url", "") or ""
        items.append({
            "issue_number": run_issue_number,
            "title": run_issue_title,
            "status": "triage",
            "detail_label": f"{resolved}/{total} resolved",
            "action": "open",
            "action_hint": f"View issue #{run_issue_number} on GitHub" if run_issue_number else "View issue on GitHub",
            "url": run_issue_url,
            "is_e2e": True,
            "e2e_progress": {"resolved": resolved, "total": total, "percent": pct},
            "e2e_sub_issues": sub_issues_data,
            "flow_steps": [
                {"key": "triage", "label": "Triage"},
                {"key": "fixing", "label": "Fixing"},
                {"key": "done", "label": "Done"},
            ],
            "flow_stage": "fixing" if resolved < total else "done",
        })
    return items


def build_e2e_recent_run_items(db: Any, config: Any, e2e_status: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    recent_runs = db.list_runs(orchestrator_id=config.orchestrator_id, limit=100)
    for run in recent_runs:
        if e2e_status.get("running") and run.status == "running":
            continue
        if e2e_status.get("last_run", {}).get("id") == run.id and e2e_status.get("needs_attention"):
            continue
        run_issue = db.get_run_issue(run.id)
        if run_issue and not run_issue.closed_at:
            continue

        relative_time = _relative_time(run.started_at) if run.started_at else ""
        item: dict[str, Any] = {
            "issue_number": f"E2E-{run.id}",
            "title": run.commit_sha[:7] if run.commit_sha else "no commit",
            "status": run.status,
            "status_label": _e2e_run_status_label(run.status),
            "detail_label": "",
            "action": "details",
            "action_hint": "View run details",
            "is_e2e": True,
            "e2e_run_id": run.id,
            "results_action": _e2e_run_results_action(run.id),
            "relative_time": relative_time,
            "time": relative_time,
            "commit_sha": run.commit_sha[:7] if run.commit_sha else "",
        }
        if run.note:
            item["note"] = run.note
        items.append(item)
    return items


def _e2e_run_status_label(status: str | None) -> str:
    return {
        "passed": "Passed",
        "failed": "Failed",
        "warning": "Passed on retry",
        "running": "Running",
        "canceled": "Canceled",
        "error": "Error",
    }.get(str(status or "").lower(), str(status or "Unknown"))


def _e2e_run_results_action(run_id: Any) -> dict[str, Any] | None:
    if run_id in (None, ""):
        return None
    try:
        parsed = int(run_id)
    except (TypeError, ValueError):
        logger.debug("dropping non-integer e2e results run_id %r", run_id)
        return None
    if parsed <= 0:
        logger.debug("dropping non-positive e2e results run_id %r", run_id)
        return None
    return {
        "kind": "e2e_run_results",
        "run_id": parsed,
        "label": "View Results",
    }


def _build_e2e_db_items(config: Any, e2e_status: dict[str, Any]) -> list[dict[str, Any]]:
    db_path = config.repo_root / ".issue-orchestrator" / "e2e.db" if config else None
    if not (db_path and db_path.exists() and config):
        return []
    try:
        from ..infra.e2e_db import E2EDB

        db = E2EDB(db_path)
        items = _build_e2e_open_run_issue_items(db)
        items.extend(build_e2e_recent_run_items(db, config, e2e_status))
        return items
    except Exception:
        return []


def build_e2e_items(config: Any, e2e_status: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    items.extend(_build_e2e_running_items(e2e_status))
    items.extend(_build_e2e_attention_items(e2e_status))
    items.extend(_build_e2e_db_items(config, e2e_status))
    return items


def _count_untriaged_failures(db: Any, run_obj: Any) -> int:
    count = 0
    for result in db.get_failed_tests(run_obj.id):
        if not db.find_open_failure_issue(result.nodeid):
            count += 1
    return count


def _e2e_cached_status(cache_key: str, *, now_mono: float, proc_running: bool) -> dict[str, Any] | None:
    with _E2E_STATUS_CACHE_LOCK:
        cached_entry = _E2E_STATUS_CACHE.get(cache_key)
    if cached_entry is None:
        return None
    cached_at, cached_payload = cached_entry
    if (now_mono - cached_at) >= E2E_STATUS_CACHE_TTL_SECONDS:
        return None
    cached_running = bool(cached_payload.get("running"))
    if cached_running != proc_running:
        return None
    return copy.deepcopy(cached_payload)


def _load_e2e_database_state(
    config: Any,
    orchestrator_id: str,
) -> tuple[Any, dict[str, Any] | None, list[dict[str, Any]], dict[str, Any] | None, int, bool]:
    from ..infra.e2e_db import E2EDB

    db_path = config.repo_root / ".issue-orchestrator" / "e2e.db"
    if not db_path.exists():
        return None, None, [], None, 0, False

    try:
        db = E2EDB(db_path)
        run_obj = db.latest_run(orchestrator_id)
        last_run = run_obj.to_dict() if run_obj else None
        failed_tests = [t.to_dict() for t in db.get_failed_tests(run_obj.id)] if run_obj else []
        if last_run and last_run.get("started_at"):
            last_run["relative_time"] = _relative_time(last_run["started_at"])
        untriaged_count = (
            _count_untriaged_failures(db, run_obj)
            if run_obj and run_obj.status == "failed" and failed_tests
            else 0
        )
        signal_score = db.compute_signal_score(orchestrator_id)
        low_stability = bool(signal_score and signal_score.get("pass_rate") is not None and signal_score["pass_rate"] < 0.5)
        return run_obj, last_run, failed_tests, signal_score, untriaged_count, low_stability
    except Exception:
        return None, None, [], None, 0, False


def _e2e_badge_state(e2e_status: dict[str, Any]) -> str:
    if e2e_status.get("running"):
        return "running"

    last_run = e2e_status.get("last_run") or {}
    last_status = last_run.get("status")
    failed_test_count = len(e2e_status.get("failed_tests") or [])
    if last_status == "failed" or e2e_status.get("needs_attention") or failed_test_count > 0:
        return "failed"
    if last_status == "warning":
        return "warning"
    if last_status == "passed":
        return "passed"
    return "idle"


def get_e2e_status(config: Any) -> dict[str, Any]:
    if not config or not config.e2e.enabled:
        return {"enabled": False, "running": False}

    orchestrator_id = config.orchestrator_id
    runner = get_e2e_runner_manager()
    proc_status = runner.status(orchestrator_id)
    cache_key = _e2e_status_cache_key(config)
    now_mono = time.monotonic()
    cached_payload = _e2e_cached_status(
        cache_key,
        now_mono=now_mono,
        proc_running=bool(proc_status.get("running")),
    )
    if cached_payload is not None:
        return cached_payload

    run_obj, last_run, failed_tests, signal_score, untriaged_count, low_stability = _load_e2e_database_state(
        config,
        orchestrator_id,
    )

    next_run = get_next_run_info(config, config.repo_root, run_obj)

    payload = {
        "enabled": True,
        "running": proc_status["running"],
        "pid": proc_status.get("pid"),
        "last_run": last_run,
        "failed_tests": failed_tests,
        "signal_score": signal_score,
        "next_run": next_run,
        "needs_attention": untriaged_count > 0,
        "untriaged_count": untriaged_count,
        "low_stability": low_stability,
    }
    with _E2E_STATUS_CACHE_LOCK:
        _E2E_STATUS_CACHE[cache_key] = (now_mono, payload)
    return copy.deepcopy(payload)


def build_e2e_view_model(
    e2e_status: dict[str, Any],
    e2e_items: list[dict[str, Any]],
    e2e_total: int,
    e2e_page: int,
    e2e_total_pages: int,
    agents: list[str],
) -> dict[str, Any]:
    """Build dedicated E2E tab view model (UI-facing, template-ready)."""
    last_run = e2e_status.get("last_run") or {}
    next_run = e2e_status.get("next_run") or {}
    running = bool(e2e_status.get("running"))
    untriaged_count = int(e2e_status.get("untriaged_count", 0) or 0)
    needs_attention = bool(e2e_status.get("needs_attention"))
    failed_test_count = len(e2e_status.get("failed_tests") or [])
    badge_count = untriaged_count if untriaged_count > 0 else e2e_total
    if failed_test_count > 0 and untriaged_count == 0 and badge_count == 0:
        badge_count = failed_test_count
    badge_state = _e2e_badge_state(e2e_status)
    badge_icons = {"running": "⟳", "failed": "✗", "warning": "⚠", "passed": "✓"}
    badge_icon = badge_icons.get(badge_state, "○")

    return {
        "badge": {
            "count": badge_count,
            "state": badge_state,
            "icon": badge_icon,
        },
        "summary": {
            "running": running,
            "needs_attention": needs_attention,
            "untriaged_count": untriaged_count,
            "last_status": last_run.get("status", "unknown"),
            "last_run_label": last_run.get("relative_time") or last_run.get("started_at") or "No runs yet",
            "results_action": _e2e_run_results_action(last_run.get("id")),
            "next_run_at": next_run.get("next_run_at", ""),
            "next_run_reason": next_run.get("next_run_reason", ""),
        },
        "controls": {
            "can_start": not running,
            "can_stop": running,
        },
        "runs": e2e_items,
        "pagination": {
            "page": e2e_page,
            "total_pages": e2e_total_pages,
            "total": e2e_total,
        },
        "agents": agents,
    }
