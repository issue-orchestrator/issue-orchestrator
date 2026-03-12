"""Dashboard view model builder."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import copy
import json
import threading
import time
from typing import Any, Callable

from ..domain.session_key import TaskKind
from ..history import latest_history_entries_by_issue
from ..control.label_manager import LabelManager
from ..infra.audit import get_issue_dependencies
from ..infra.e2e_runner import get_e2e_runner_manager, get_next_run_info
from ..infra import gh_audit

QUEUE_PAGE_SIZE = 20
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


@dataclass(frozen=True)
class DashboardViewModel:
    """View model for the web dashboard."""

    issues: list[dict[str, Any]]
    active_items: list[dict[str, Any]]
    queue_items: list[dict[str, Any]]
    blocked_items: list[dict[str, Any]]
    history_items: list[dict[str, Any]]
    e2e_items: list[dict[str, Any]]
    completed_items: list[dict[str, Any]]
    awaiting_merge_items: list[dict[str, Any]]
    flow_columns: list[dict[str, Any]]
    scope_summary: dict[str, Any]

    active_count: int
    queue_count: int
    blocked_count: int
    e2e_count: int
    completed_count: int
    awaiting_merge_count: int

    active_tab: str
    paused: bool
    shutdown_requested: bool
    active_session_count: int
    startup_status: str
    startup_message: str

    repo: str
    repo_root: str
    config_name: str
    github_owner: str
    github_repo: str

    queue_page: int
    queue_total_pages: int
    queue_total: int
    queue_refresh_seconds: int

    e2e_status: dict[str, Any]
    e2e_page: int
    e2e_total_pages: int
    e2e_total: int

    agents: dict[str, Any]
    agent_names: list[str]
    open_provider_circuits: list[dict[str, Any]]

    def template_context(self) -> dict[str, Any]:
        return {
            "issues": self.issues,
            "active_items": self.active_items,
            "queue_items": self.queue_items,
            "blocked_items": self.blocked_items,
            "history_items": self.history_items,
            "e2e_items": self.e2e_items,
            "completed_items": self.completed_items,
            "awaiting_merge_items": self.awaiting_merge_items,
            "flow_columns": self.flow_columns,
            "scope_summary": self.scope_summary,
            "active_count": self.active_count,
            "queue_count": self.queue_count,
            "blocked_count": self.blocked_count,
            "e2e_count": self.e2e_count,
            "completed_count": self.completed_count,
            "awaiting_merge_count": self.awaiting_merge_count,
            "active_tab": self.active_tab,
            "paused": self.paused,
            "shutdown_requested": self.shutdown_requested,
            "active_session_count": self.active_session_count,
            "startup_status": self.startup_status,
            "startup_message": self.startup_message,
            "repo": self.repo,
            "repo_root": self.repo_root,
            "github_owner": self.github_owner,
            "github_repo": self.github_repo,
            "queue_page": self.queue_page,
            "queue_total_pages": self.queue_total_pages,
            "queue_total": self.queue_total,
            "queue_refresh_seconds": self.queue_refresh_seconds,
            "agents": self.agents,
            "e2e_status": self.e2e_status,
            "e2e_page": self.e2e_page,
            "e2e_total_pages": self.e2e_total_pages,
            "e2e_total": self.e2e_total,
            "open_provider_circuits": self.open_provider_circuits,
            "dashboard_data": self.dashboard_data(),
        }

    def dashboard_data(self) -> dict[str, Any]:
        github_usage = gh_audit.get_live_usage_snapshot()
        return {
            "startupComplete": self.startup_status == "complete",
            "paused": self.paused,
            "e2eRunning": bool(self.e2e_status.get("running")),
            "queueRefreshSeconds": self.queue_refresh_seconds,
            "repo": self.repo,
            "repoRoot": self.repo_root,
            "configName": self.config_name,
            "githubOwner": self.github_owner,
            "githubRepo": self.github_repo,
            "e2eLastRun": self.e2e_status.get("last_run"),
            "agents": self.agent_names,
            "scope": self.scope_summary,
            "refresh": self.scope_summary.get("refresh", {}),
            "githubUsage": github_usage,
            "fetchLayerVisibilityAwareEnabled": self.scope_summary.get("refresh", {}).get("visibilityAwareEnabled", False),
            "fetchLayerSelectiveSyncPlannerEnabled": self.scope_summary.get("refresh", {}).get("selectiveSyncPlannerEnabled", False),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "issues": self.issues,
            "active_items": self.active_items,
            "queue_items": self.queue_items,
            "blocked_items": self.blocked_items,
            "history_items": self.history_items,
            "e2e_items": self.e2e_items,
            "completed_items": self.completed_items,
            "awaiting_merge_items": self.awaiting_merge_items,
            "flow_columns": self.flow_columns,
            "scope_summary": self.scope_summary,
            "active_count": self.active_count,
            "queue_count": self.queue_count,
            "blocked_count": self.blocked_count,
            "e2e_count": self.e2e_count,
            "completed_count": self.completed_count,
            "awaiting_merge_count": self.awaiting_merge_count,
            "active_tab": self.active_tab,
            "paused": self.paused,
            "shutdown_requested": self.shutdown_requested,
            "active_session_count": self.active_session_count,
            "startup_status": self.startup_status,
            "startup_message": self.startup_message,
            "repo": self.repo,
            "repo_root": self.repo_root,
            "github_owner": self.github_owner,
            "github_repo": self.github_repo,
            "queue_page": self.queue_page,
            "queue_total_pages": self.queue_total_pages,
            "queue_total": self.queue_total,
            "queue_refresh_seconds": self.queue_refresh_seconds,
            "agents": self.agent_names,
            "e2e_status": self.e2e_status,
            "e2e_page": self.e2e_page,
            "e2e_total_pages": self.e2e_total_pages,
            "e2e_total": self.e2e_total,
            "open_provider_circuits": self.open_provider_circuits,
            "dashboard_data": self.dashboard_data(),
        }


def issue_url_for(config, issue_number: int) -> str:
    if config and config.repo:
        return f"https://github.com/{config.repo}/issues/{issue_number}"
    return ""


def flow_steps_for(stage: str) -> list[dict[str, str]]:
    if stage == "not_eligible":
        return [
            {"key": "not_eligible", "label": "Not Eligible"},
            {"key": "queued", "label": "Queued"},
            {"key": "in_progress", "label": "In Progress"},
            {"key": "review", "label": "Review"},
            {"key": "done", "label": "Done"},
        ]
    if stage == "rework":
        return [
            {"key": "queued", "label": "Queued"},
            {"key": "in_progress", "label": "In Progress"},
            {"key": "review", "label": "Review"},
            {"key": "rework", "label": "Rework"},
            {"key": "done", "label": "Done"},
        ]
    if stage == "triage":
        return [
            {"key": "queued", "label": "Queued"},
            {"key": "in_progress", "label": "In Progress"},
            {"key": "review", "label": "Review"},
            {"key": "triage", "label": "Triage"},
            {"key": "done", "label": "Done"},
        ]
    return [
        {"key": "queued", "label": "Queued"},
        {"key": "in_progress", "label": "In Progress"},
        {"key": "review", "label": "Review"},
        {"key": "done", "label": "Done"},
    ]


def flow_stage_label(steps: list[dict[str, str]], stage: str) -> str:
    for step in steps:
        if step["key"] == stage:
            return step["label"]
    return stage.replace("_", " ").title()


def blocked_summary(labels: list[str], lm: LabelManager, dependency_summary: str | None = None) -> str | None:
    reasons: list[str] = []
    blocking = lm.get_blocking(labels)
    if blocking:
        reasons.append(lm.describe(blocking[0]))
    if dependency_summary:
        reasons.append(dependency_summary)
    return " • ".join(reasons) if reasons else None


def _queue_wait_reason(
    *,
    state,
    config,
    issue_number: int,
    dep_problem: Any | None,
    queue_position: int,
    open_provider: str | None = None,
) -> str:
    if open_provider:
        return f"Waiting: {open_provider} unavailable"
    if state.paused:
        return "Waiting: orchestrator paused"

    active_count = len(state.active_sessions)
    max_sessions = max(1, int(getattr(config, "max_concurrent_sessions", 1)))
    if active_count >= max_sessions:
        return f"Waiting: at capacity ({active_count}/{max_sessions} running)"

    if dep_problem is not None and dep_problem.summary:
        return f"Waiting: {dep_problem.summary}"

    if issue_number in state.failed_this_cycle:
        return "Waiting: previous launch/action failed (manual retry may be needed)"

    if any(entry.issue_number == issue_number for entry in state.session_history):
        return "Waiting: previous run state"

    if queue_position <= 1:
        return "Waiting: next scheduler tick"
    return f"Waiting: {queue_position - 1} queued ahead"


def _display_labels(labels: list[str], lm: LabelManager) -> list[str]:
    """Labels shown as pills in UI cards.

    Include orchestrator-owned labels and agent routing labels.
    """
    visible = set(lm.get_ours(labels))
    visible.update(label for label in labels if label.startswith("agent:"))
    return sorted(visible)


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


def _format_age_seconds(seconds: float | int | None) -> str:
    if seconds is None:
        return "never"
    if seconds < 60:
        return f"{int(seconds)}s"
    if seconds < 3600:
        return f"{int(seconds // 60)}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def _refresh_meta_for_issue(state, config, issue_number: int, now_ts: float) -> dict[str, Any]:
    per_issue = state.issue_last_refreshed_at.get(issue_number)
    fallback = state.queue_last_refresh_at if state.queue_last_refresh_at > 0 else None
    last_refreshed_at = per_issue or fallback
    age_seconds = (now_ts - last_refreshed_at) if last_refreshed_at else None
    stale_threshold = max(60, int(getattr(config, "flow_refresh_stale_seconds", 900)))
    is_stale = age_seconds is None or age_seconds > stale_threshold
    freshness_label = f"{_format_age_seconds(age_seconds)} ago" if age_seconds is not None else "never refreshed"
    stale_reason = (
        "Not refreshed from GitHub yet"
        if age_seconds is None
        else f"Older than {_format_age_seconds(stale_threshold)} stale threshold"
        if is_stale
        else ""
    )
    return {
        "last_refreshed_at": last_refreshed_at or 0.0,
        "last_refreshed_age_seconds": age_seconds if age_seconds is not None else -1,
        "last_refreshed_label": freshness_label,
        "is_stale": is_stale,
        "stale_reason": stale_reason,
    }


def _attach_refresh_meta(items: list[dict[str, Any]], state, config, now_ts: float) -> None:
    for item in items:
        issue_number = item.get("issue_number")
        if not isinstance(issue_number, int):
            continue
        item.update(_refresh_meta_for_issue(state, config, issue_number, now_ts))


def _refresh_meta(state, config, issue_number: int) -> dict[str, Any]:
    refreshed_at = state.issue_refresh_timestamps.get(issue_number, 0.0)
    queue_interval = config.queue_refresh_seconds if config else 600
    full_scan_interval = config.fetch_layer_full_scan_interval_seconds if config else 1800
    if queue_interval > 0:
        stale_after = max(queue_interval * 2, 120)
    else:
        stale_after = max(full_scan_interval, 300)

    if refreshed_at <= 0:
        return {
            "refresh_age_seconds": None,
            "refresh_age_label": "not refreshed",
            "is_stale": True,
        }

    age_seconds = max(0, int(time.time() - refreshed_at))
    if age_seconds >= 3600:
        age_label = f"{age_seconds // 3600}h"
    elif age_seconds >= 60:
        age_label = f"{age_seconds // 60}m"
    else:
        age_label = f"{age_seconds}s"
    return {
        "refresh_age_seconds": age_seconds,
        "refresh_age_label": age_label,
        "is_stale": age_seconds >= stale_after,
    }


def _pending_issue_numbers(state) -> dict[str, set[int]]:
    pending_review_numbers = {r.issue_number for r in state.pending_reviews} | {
        r.issue_number for r in state.discovered_reviews
    }
    pending_rework_numbers = {r.issue_number for r in state.pending_reworks} | {
        r.issue_number for r in state.discovered_reworks
    }
    pending_triage_numbers = {r.issue_number for r in state.pending_triage_reviews}
    return {
        "review": pending_review_numbers,
        "rework": pending_rework_numbers,
        "triage": pending_triage_numbers,
    }


def _is_synthetic_session_title(title: str) -> bool:
    normalized = title.strip()
    return (
        normalized.startswith("Review PR #")
        or normalized.startswith("Rework #")
        or normalized.startswith("Rework PR #")
    )


def _canonical_issue_title(state, issue_number: int, fallback_title: str) -> str:
    for issue in state.cached_queue_issues:
        if issue.number == issue_number and issue.title:
            return issue.title
    for entry in reversed(state.session_history):
        if entry.issue_number != issue_number:
            continue
        title = (entry.title or "").strip()
        if title and not _is_synthetic_session_title(title):
            return title
    return fallback_title


def _build_active_items(state, config, queue_page: int, seen_issues: set[int], *, lm: LabelManager) -> tuple[list[dict[str, Any]], set[int]]:
    if queue_page != 1:
        return [], seen_issues

    items: list[dict[str, Any]] = []
    for session in state.active_sessions:
        runtime = session.runtime_minutes
        timeout = session.agent_config.timeout_minutes
        tmux_name = session.terminal_id or ""
        is_review = tmux_name.startswith("review-")
        phase = "Reviewing" if is_review else "Coding"

        agent_label = (session.issue.agent_type or "unknown").replace("agent:", "")
        if runtime >= timeout:
            status = "slow"
            status_reason = f"Over timeout ({runtime} min / {timeout} min)"
        else:
            status = "active"
            status_reason = f"Running for {runtime} min"

        seen_issues.add(session.issue.number)
        if session.key.task == TaskKind.REVIEW:
            flow_stage = "review"
        elif session.key.task == TaskKind.REWORK:
            flow_stage = "rework"
        elif session.key.task == TaskKind.TRIAGE:
            flow_stage = "triage"
        else:
            flow_stage = "in_progress"
        flow_steps = flow_steps_for(flow_stage)
        flow_stage_label_value = flow_stage_label(flow_steps, flow_stage)

        blocked = blocked_summary(
            list(session.issue.labels),
            lm,
            state.dependency_problems.get(session.issue.number).summary
            if session.issue.number in state.dependency_problems
            else None,
        )

        terminal_hint = "Click to focus terminal session"
        if config and config.terminal_adapter == "subprocess":
            terminal_hint = "Click to view agent UI log"

        items.append({
            "card_id": session.terminal_id,
            "issue_number": session.issue.number,
            "title": _canonical_issue_title(state, session.issue.number, session.issue.title),
            "agent_type": agent_label,
            "status": status,
            "status_reason": status_reason,
            "detail_label": f"agent: {agent_label}",
            "detail_reason": status_reason,
            "phase": phase,
            "time": f"{runtime} min",
            "action": "focus",
            "action_icon": "→",
            "action_hint": terminal_hint,
            "url": "",
            "issue_url": issue_url_for(config, session.issue.number),
            "pr_url": "",
            "has_terminal": True,
            "worktree_path": str(session.worktree_path) if session.worktree_path else "",
            "flow_stage": flow_stage,
            "flow_stage_label": flow_stage_label_value,
            "flow_steps": flow_steps,
            "blocked_summary": blocked,
            "orchestrator_labels": _display_labels(list(session.issue.labels), lm),
            **_refresh_meta(state, config, session.issue.number),
        })

    return items, seen_issues


def _build_queue_items(  # noqa: C901, PLR0912 — aggregates queue from multiple state sources
    state,
    config,
    queue_page: int,
    seen_issues: set[int],
    pending_numbers: dict[str, set[int]],
    *,
    lm: LabelManager,
    open_providers: set[str] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int, set[int]]:
    queue_items: list[dict[str, Any]] = []
    blocked_items: list[dict[str, Any]] = []
    queue_total = 0

    if state.startup_status != "complete":
        return queue_items, blocked_items, queue_total, seen_issues

    queue_issues = state.cached_queue_issues
    queue_total = len(queue_issues)
    dependency_info = get_issue_dependencies(queue_issues, config)

    start_idx = (queue_page - 1) * QUEUE_PAGE_SIZE
    end_idx = start_idx + QUEUE_PAGE_SIZE
    queued_position = 0
    for issue in queue_issues[start_idx:end_idx]:
        if issue.number in seen_issues:
            continue
        seen_issues.add(issue.number)

        dep_info = dependency_info.get(issue.number)
        has_deps = dep_info.has_dependencies if dep_info else False
        deps_json = json.dumps([
            {"number": d[0], "title": d[1]}
            for d in (dep_info.dependencies if dep_info else [])
        ])
        dep_summary = dep_info.summary if dep_info else ""

        dep_problem = state.dependency_problems.get(issue.number)
        blocked = blocked_summary(
            list(issue.labels),
            lm,
            dep_problem.summary if dep_problem else None,
        )
        # Separate dependency-blocked (stays in queue) from agent-blocked (goes to blocked column)
        is_dependency_blocked = dep_problem is not None
        is_agent_blocked = issue.is_blocked
        is_blocked = is_agent_blocked  # Only label-based blocks go to the blocked column
        agent_label = (issue.agent_type or "unknown").replace("agent:", "")
        if is_blocked:
            status = "blocked"
            status_reason = _normalize_status_reason(dep_summary) or "blocked"
            detail_label = blocked or "blocked"
        elif is_dependency_blocked:
            status = "queue"
            status_reason = _normalize_status_reason(dep_summary)
            detail_label = f"agent: {agent_label}"
        else:
            status = "queue"
            status_reason = _normalize_status_reason(dep_summary)
            detail_label = f"agent: {agent_label}"

        if is_blocked:
            flow_stage = "blocked"
        elif issue.number in pending_numbers["rework"]:
            flow_stage = "rework"
        elif issue.number in pending_numbers["triage"]:
            flow_stage = "triage"
        elif issue.number in pending_numbers["review"] or lm.is_pr_pending(issue.labels):
            flow_stage = "review"
        elif lm.is_in_progress(issue.labels):
            flow_stage = "in_progress"
        else:
            flow_stage = "queued"
            queued_position += 1
        flow_steps = flow_steps_for(flow_stage)
        flow_stage_label_value = flow_stage_label(flow_steps, flow_stage)
        issue_provider = (
            (config.agents.get(issue.agent_type) or config.agents.get(f"agent:{agent_label}") if config and config.agents else None)
        )
        issue_provider_name = issue_provider.provider if issue_provider else None
        open_provider = (
            issue_provider_name
            if issue_provider_name and open_providers and issue_provider_name in open_providers
            else None
        )
        queue_reason = (
            _queue_wait_reason(
                state=state,
                config=config,
                issue_number=issue.number,
                dep_problem=dep_problem,
                queue_position=queued_position,
                open_provider=open_provider,
            )
            if flow_stage == "queued"
            else None
        )
        if queue_reason:
            detail_label = queue_reason

        item = {
            "issue_number": issue.number,
            "title": issue.title,
            "agent_type": agent_label,
            "status": status,
            "status_reason": status_reason,
            "detail_label": detail_label,
            "detail_reason": status_reason,
            "time": "",
            "action": "open",
            "action_icon": "↗",
            "action_hint": "Click to open issue on GitHub",
            "url": issue_url_for(config, issue.number),
            "issue_url": issue_url_for(config, issue.number),
            "pr_url": "",
            "has_terminal": False,
            "worktree_path": "",
            "has_dependencies": has_deps,
            "dependencies": deps_json,
            "dependency_summary": dep_summary,
            "flow_stage": flow_stage,
            "flow_stage_label": flow_stage_label_value,
            "flow_steps": flow_steps,
            "blocked_summary": blocked,
            "queue_wait_reason": queue_reason,
            "merge_pending": lm.is_pr_pending(issue.labels),
            "dependency_blocked": is_dependency_blocked,
            "orchestrator_labels": _display_labels(list(issue.labels), lm),
            **_refresh_meta(state, config, issue.number),
        }
        if is_blocked:
            blocked_items.append(item)
        else:
            queue_items.append(item)  # Dependency-blocked items stay in queue

    return queue_items, blocked_items, queue_total, seen_issues


def _build_history_items(state, config) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    status_labels = {
        "completed": "Completed",
        "failed": "Failed",
        "blocked": "Blocked",
        "needs_human": "Needs Human",
        "timed_out": "Timed Out",
    }
    history_items: list[dict[str, Any]] = []
    blocked_items: list[dict[str, Any]] = []
    for entry in latest_history_entries_by_issue(state.session_history, limit=50):
        # Completed-without-PR is not a terminal lane; let queue data drive placement.
        if entry.status == "completed" and not entry.pr_url:
            continue
        url = entry.pr_url if entry.pr_url else issue_url_for(config, entry.issue_number)
        action_hint = "Click to open PR" if entry.pr_url else "Click to open issue on GitHub"
        status_reason = _normalize_status_reason(getattr(entry, "status_reason", None))
        if not status_reason:
            status_reason = status_labels.get(entry.status, entry.status)

        if entry.status == "completed":
            flow_stage = "done"
        elif entry.status in ("blocked", "needs_human", "failed", "timed_out"):
            flow_stage = "blocked"
        else:
            flow_stage = "in_progress"
        flow_steps = flow_steps_for(flow_stage)
        flow_stage_label_value = flow_stage_label(flow_steps, flow_stage)

        worktree_path = str(entry.worktree_path) if entry.worktree_path else ""

        item = {
            "issue_number": entry.issue_number,
            "title": entry.title,
            "agent_type": entry.agent_type.replace("agent:", ""),
            "status": entry.status,
            "status_reason": status_reason,
            "detail_label": status_labels.get(entry.status, entry.status),
            "detail_reason": status_reason,
            "time": _format_history_time(entry),
            "action": "open",
            "action_icon": "↗",
            "action_hint": action_hint,
            "url": url,
            "issue_url": issue_url_for(config, entry.issue_number),
            "pr_url": entry.pr_url or "",
            "has_terminal": False,
            "worktree_path": worktree_path,
            "flow_stage": flow_stage,
            "flow_stage_label": flow_stage_label_value,
            "flow_steps": flow_steps,
            "blocked_summary": status_reason if entry.status != "completed" else None,
            # History records with an open PR belong in Awaiting Merge, not Completed.
            "merge_pending": entry.status == "completed" and bool(entry.pr_url),
            **_refresh_meta(state, config, entry.issue_number),
        }
        if entry.status in ("blocked", "needs_human", "failed", "timed_out"):
            blocked_items.append(item)
        else:
            history_items.append(item)

    return history_items, blocked_items


def _normalize_status_reason(reason: str | None) -> str | None:
    if reason is None:
        return None
    trimmed = reason.strip()
    if not trimmed:
        return None
    # Freshness is represented by stale-dot metadata; avoid duplicating noisy sync text on cards.
    if trimmed.lower().startswith("synced "):
        return None
    return trimmed


def _sort_by_issue_number(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Deterministic default ordering for dashboard issue lists."""
    return sorted(items, key=lambda item: int(item.get("issue_number", 0)))


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
        "title": f"{untriaged} failure{'s' if untriaged != 1 else ''} need{'s' if untriaged == 1 else ''} triage",
        "status": "needs_attention",
        "detail_label": f"{untriaged} test{'s' if untriaged != 1 else ''} failed without issues",
        "action": "triage",
        "action_hint": "Click to open triage modal",
        "is_e2e": True,
        "e2e_failed_tests": failed_tests_data,
        "e2e_run_id": run_id,
        "relative_time": last_run.get("relative_time", ""),
        "time": last_run.get("relative_time", ""),
    }]


def _build_e2e_open_run_issue_items(db) -> list[dict[str, Any]]:
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


def _build_e2e_recent_run_items(db, config, e2e_status: dict[str, Any]) -> list[dict[str, Any]]:
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
        items.append({
            "issue_number": f"E2E-{run.id}",
            "title": run.commit_sha[:7] if run.commit_sha else "no commit",
            "status": run.status,
            "detail_label": "",
            "action": "details",
            "action_hint": "View run details",
            "is_e2e": True,
            "e2e_run_id": run.id,
            "relative_time": relative_time,
            "time": relative_time,
            "commit_sha": run.commit_sha[:7] if run.commit_sha else "",
        })
    return items


def _build_e2e_db_items(config, e2e_status: dict[str, Any]) -> list[dict[str, Any]]:
    db_path = config.repo_root / ".issue-orchestrator" / "e2e.db" if config else None
    if not (db_path and db_path.exists() and config):
        return []
    try:
        from ..infra.e2e_db import E2EDB

        db = E2EDB(db_path)
        items = _build_e2e_open_run_issue_items(db)
        items.extend(_build_e2e_recent_run_items(db, config, e2e_status))
        return items
    except Exception:
        return []


def _build_e2e_items(config, e2e_status: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    items.extend(_build_e2e_running_items(e2e_status))
    items.extend(_build_e2e_attention_items(e2e_status))
    items.extend(_build_e2e_db_items(config, e2e_status))
    return items


def _compact_card(item: dict[str, Any], state_label: str | None = None) -> dict[str, Any]:
    phase = item.get("flow_stage_label") or item.get("flow_stage") or ""
    phase_age = item.get("time") or ""
    blocked = item.get("blocked_summary") or ""
    summary_text = item.get("queue_wait_reason") or (f"Summary: {blocked}" if blocked else "")
    return {
        "card_id": item.get("card_id") or f"issue-{item.get('issue_number')}",
        "issue_number": item.get("issue_number"),
        "title": item.get("title", ""),
        "agent_type": item.get("agent_type", ""),
        "state_label": state_label or item.get("status", ""),
        "phase": phase,
        "phase_age": phase_age,
        "summary": summary_text,
        "queue_wait_reason": item.get("queue_wait_reason"),
        "blocked_summary": blocked,
        "badges": [],
        "orchestrator_labels": item.get("orchestrator_labels", []),
        "focus_action": "focus",
        "issue_url": item.get("issue_url") or item.get("url") or "",
        "focus_hint": "Focus issue",
        "github_hint": "Open in GitHub",
        "last_refreshed_label": item.get("last_refreshed_label", "unknown"),
        "is_stale": bool(item.get("is_stale", False)),
        "stale_reason": item.get("stale_reason", ""),
        "last_refreshed_age_seconds": item.get("last_refreshed_age_seconds", -1),
    }


def _build_backlog_items(state, config, *, lm: LabelManager) -> list[dict[str, Any]]:
    if state.startup_status != "complete":
        return []
    dependency_info = get_issue_dependencies(state.cached_queue_issues, config)
    cards: list[dict[str, Any]] = []
    for issue in state.cached_queue_issues:
        dep_info = dependency_info.get(issue.number)
        dep_summary = dep_info.summary if dep_info else ""
        blocked = blocked_summary(
            list(issue.labels),
            lm,
            dep_summary if dep_summary else None,
        )
        cards.append({
            "issue_number": issue.number,
            "title": issue.title,
            "status": "backlog",
            "detail_label": "In execution scope",
            "flow_stage": "queued",
            "flow_stage_label": "Queued",
            "blocked_summary": blocked,
            "time": "",
            "issue_url": issue_url_for(config, issue.number),
            "url": issue_url_for(config, issue.number),
            "orchestrator_labels": _display_labels(list(issue.labels), lm),
            **_refresh_meta(state, config, issue.number),
        })
    return cards


def _exclude_flow_overlaps(
    backlog_items: list[dict[str, Any]],
    queue_items: list[dict[str, Any]],
    active_items: list[dict[str, Any]],
    blocked_items: list[dict[str, Any]],
    completed_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep scope count accurate by removing items already in a kanban column.

    Backlog is used only for scope_summary.in_scope_total; anything already
    represented in queued/running/blocked/completed should not be double-counted.
    """
    def _to_issue_number(raw: Any) -> int | None:
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str) and raw.isdigit():
            return int(raw)
        return None

    occupied_numbers = {
        issue_number
        for item in queue_items + active_items + blocked_items + completed_items
        for issue_number in [_to_issue_number(item.get("issue_number"))]
        if issue_number is not None
    }
    return [
        item
        for item in backlog_items
        for issue_number in [_to_issue_number(item.get("issue_number"))]
        if issue_number is not None and issue_number not in occupied_numbers
    ]


def _issue_numbers(items: list[dict[str, Any]]) -> set[int]:
    """Extract numeric issue numbers from card items."""
    numbers: set[int] = set()
    for item in items:
        raw = item.get("issue_number")
        if isinstance(raw, int):
            numbers.add(raw)
        elif isinstance(raw, str) and raw.isdigit():
            numbers.add(int(raw))
    return numbers


def _exclude_issue_numbers(
    items: list[dict[str, Any]],
    excluded_numbers: set[int],
) -> list[dict[str, Any]]:
    """Return items whose issue number is not in excluded_numbers."""
    filtered: list[dict[str, Any]] = []
    for item in items:
        raw = item.get("issue_number")
        issue_number: int | None = None
        if isinstance(raw, int):
            issue_number = raw
        elif isinstance(raw, str) and raw.isdigit():
            issue_number = int(raw)
        if issue_number is None or issue_number not in excluded_numbers:
            filtered.append(item)
    return filtered


def _apply_lane_precedence(
    queue_items: list[dict[str, Any]],
    active_items: list[dict[str, Any]],
    blocked_items: list[dict[str, Any]],
    awaiting_merge_items: list[dict[str, Any]],
    completed_items: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Enforce single-lane ownership across non-running lanes.

    Precedence:
    running > blocked > awaiting-merge > queued > completed
    """
    active_numbers = _issue_numbers(active_items)
    blocked_filtered = _exclude_issue_numbers(blocked_items, active_numbers)
    blocked_numbers = _issue_numbers(blocked_filtered)

    awaiting_filtered = _exclude_issue_numbers(awaiting_merge_items, active_numbers | blocked_numbers)
    awaiting_numbers = _issue_numbers(awaiting_filtered)

    queue_filtered = _exclude_issue_numbers(queue_items, active_numbers | blocked_numbers | awaiting_numbers)
    queue_numbers = _issue_numbers(queue_filtered)

    completed_filtered = _exclude_issue_numbers(
        completed_items,
        active_numbers | blocked_numbers | awaiting_numbers | queue_numbers,
    )
    return queue_filtered, blocked_filtered, awaiting_filtered, completed_filtered


def _build_awaiting_merge_items(
    queue_items: list[dict[str, Any]],
    blocked_items: list[dict[str, Any]],
    history_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Items with PRs ready to merge — drawn from all lifecycle stages."""
    return [
        item for item in queue_items + blocked_items + history_items
        if item.get("merge_pending")
    ]


def _build_flow_columns(
    queue_items: list[dict[str, Any]],
    active_items: list[dict[str, Any]],
    blocked_items: list[dict[str, Any]],
    awaiting_merge_items: list[dict[str, Any]],
    completed_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    # Exclude merge-pending items from the queued column (they appear in awaiting-merge)
    awaiting_numbers = {item.get("issue_number") for item in awaiting_merge_items}
    queued_only = [item for item in queue_items if item.get("issue_number") not in awaiting_numbers]
    return [
        {
            "id": "queued",
            "title": "Queued",
            "count": len(queued_only),
            "items": [_compact_card(item, "queued") for item in queued_only[:12]],
            "expandable": True,
        },
        {
            "id": "running",
            "title": "Running",
            "count": len(active_items),
            "items": [_compact_card(item, "running") for item in active_items[:12]],
            "expandable": True,
        },
        {
            "id": "blocked",
            "title": "Blocked",
            "count": len(blocked_items),
            "items": [_compact_card(item, "blocked") for item in blocked_items[:12]],
            "expandable": True,
        },
        {
            "id": "awaiting-merge",
            "title": "Awaiting Merge",
            "count": len(awaiting_merge_items),
            "items": [_compact_card(item, "awaiting merge") for item in awaiting_merge_items[:12]],
            "expandable": True,
        },
        {
            "id": "completed",
            "title": "Completed",
            "count": len(completed_items),
            "items": [_compact_card(item, "completed") for item in completed_items[:12]],
            "expandable": True,
            "session_scoped": True,
        },
    ]


def _select_issues_for_tab(
    active_tab: str,
    active_items: list[dict[str, Any]],
    queue_items: list[dict[str, Any]],
    blocked_items: list[dict[str, Any]],
    e2e_items: list[dict[str, Any]],
    awaiting_merge_items: list[dict[str, Any]],
    completed_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if active_tab == "kanban":
        return active_items if active_items else queue_items
    if active_tab == "blocked":
        return blocked_items
    if active_tab == "awaiting-merge":
        return awaiting_merge_items
    if active_tab == "completed":
        return completed_items
    if active_tab == "e2e":
        return e2e_items
    return active_items


def _paginate(items: list[dict[str, Any]], page: int, page_size: int) -> tuple[list[dict[str, Any]], int, int]:
    total = len(items)
    total_pages = (total + page_size - 1) // page_size if total > 0 else 1
    if page > total_pages:
        page = total_pages
    start_idx = (page - 1) * page_size
    end_idx = start_idx + page_size
    return items[start_idx:end_idx], total_pages, page


def _format_history_time(entry) -> str:
    completed_at = getattr(entry, "completed_at", None)
    runtime = entry.runtime_minutes
    if runtime and completed_at:
        return f"{runtime} min @ {completed_at}"
    if runtime:
        return f"{runtime} min"
    if completed_at:
        return str(completed_at)
    return "-"


def _count_untriaged_failures(db, run_obj) -> int:
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


def _load_e2e_database_state(config, orchestrator_id: str) -> tuple[Any, dict[str, Any] | None, list[dict[str, Any]], dict[str, Any] | None, int, bool]:
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


def _get_e2e_status(config) -> dict[str, Any]:
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


def _build_e2e_view_model(
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
    badge_count = untriaged_count if untriaged_count > 0 else e2e_total
    badge_state = (
        "running"
        if running
        else "failed"
        if (last_run.get("status") == "failed" or needs_attention)
        else "passed"
        if last_run.get("status") == "passed"
        else "idle"
    )
    badge_icon = "⟳" if badge_state == "running" else "✗" if badge_state == "failed" else "✓" if badge_state == "passed" else "○"

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


def _normalize_tab(active_tab: str) -> str:
    # Map legacy tab names to new kanban-based tabs
    if active_tab in {"work", "active", "queue", "flow"}:
        return "kanban"
    if active_tab == "attention":
        return "kanban"
    if active_tab in {"history", "merged"}:
        return "kanban"
    if active_tab in {"kanban", "blocked", "awaiting-merge", "completed", "e2e"}:
        return active_tab
    return "kanban"


def build_dashboard_view_model(
    orchestrator,
    queue_page: int = 1,
    active_tab: str = "kanban",
    e2e_page: int = 1,
    e2e_status_provider: Callable[[Any], dict[str, Any]] | None = None,
) -> DashboardViewModel:
    """Build dashboard view model for templates and APIs."""
    active_tab = _normalize_tab(active_tab)
    queue_page = max(queue_page, 1)
    e2e_page = max(e2e_page, 1)

    state = orchestrator.state if orchestrator else None
    config = orchestrator.config if orchestrator else None

    active_items: list[dict[str, Any]] = []
    queue_items: list[dict[str, Any]] = []
    blocked_items: list[dict[str, Any]] = []
    history_items: list[dict[str, Any]] = []
    e2e_items: list[dict[str, Any]] = []
    backlog_items: list[dict[str, Any]] = []
    awaiting_merge_items: list[dict[str, Any]] = []
    completed_items: list[dict[str, Any]] = []
    flow_columns: list[dict[str, Any]] = []
    scope_summary: dict[str, Any] = {}
    seen_issues: set[int] = set()

    queue_total = 0
    open_provider_circuits: list[dict[str, Any]] = []
    open_provider_names: set[str] = set()

    if orchestrator and hasattr(orchestrator, "deps") and hasattr(orchestrator.deps, "provider_resilience"):
        _now = datetime.now(timezone.utc)
        for _cs in orchestrator.deps.provider_resilience.store.list_all():
            if _cs.open_until and _cs.open_until > _now:
                _cooldown = max(0, int((_cs.open_until - _now).total_seconds()))
                open_provider_circuits.append({
                    "provider": _cs.provider,
                    "open_until": _cs.open_until.isoformat(),
                    "cooldown_remaining_seconds": _cooldown,
                    "consecutive_outages": _cs.consecutive_outages,
                    "last_error_summary": _cs.last_error_summary or "",
                })
                open_provider_names.add(_cs.provider)

    if state and config:
        lm = LabelManager(config)
        active_numbers = {s.issue.number for s in state.active_sessions}
        seen_issues.update(active_numbers)

        pending_numbers = _pending_issue_numbers(state)
        active_items, seen_issues = _build_active_items(state, config, queue_page, seen_issues, lm=lm)
        queue_items, queue_blocked, queue_total, seen_issues = _build_queue_items(
            state, config, queue_page, seen_issues, pending_numbers, lm=lm,
            open_providers=open_provider_names,
        )
        backlog_items = _build_backlog_items(state, config, lm=lm)
        blocked_items.extend(queue_blocked)
        history_items, history_blocked = _build_history_items(state, config)
        blocked_items.extend(history_blocked)

        active_items = _sort_by_issue_number(active_items)
        queue_items = _sort_by_issue_number(queue_items)
        blocked_items = _sort_by_issue_number(blocked_items)
        history_items = _sort_by_issue_number(history_items)

        now_ts = datetime.now(timezone.utc).timestamp()
        _attach_refresh_meta(active_items, state, config, now_ts)
        _attach_refresh_meta(queue_items, state, config, now_ts)
        _attach_refresh_meta(blocked_items, state, config, now_ts)
        _attach_refresh_meta(history_items, state, config, now_ts)
        _attach_refresh_meta(backlog_items, state, config, now_ts)

        # Completed = items the agent finished this session
        completed_items = [item for item in history_items if item.get("status") == "completed"]
        completed_items = _sort_by_issue_number(completed_items)

        # Awaiting merge = items with PRs ready for human merge
        awaiting_merge_items = _build_awaiting_merge_items(queue_items, blocked_items, history_items)
        awaiting_merge_items = _sort_by_issue_number(awaiting_merge_items)

        queue_items, blocked_items, awaiting_merge_items, completed_items = _apply_lane_precedence(
            queue_items=queue_items,
            active_items=active_items,
            blocked_items=blocked_items,
            awaiting_merge_items=awaiting_merge_items,
            completed_items=completed_items,
        )

        queue_items = _sort_by_issue_number(queue_items)
        blocked_items = _sort_by_issue_number(blocked_items)
        awaiting_merge_items = _sort_by_issue_number(awaiting_merge_items)
        completed_items = _sort_by_issue_number(completed_items)

        # Backlog used only for scope_summary.in_scope_total (not a kanban column)
        backlog_items = _exclude_flow_overlaps(
            backlog_items,
            queue_items,
            active_items,
            blocked_items,
            completed_items,
        )
        flow_columns = _build_flow_columns(
            queue_items, active_items, blocked_items, awaiting_merge_items, completed_items
        )

    e2e_status_provider = e2e_status_provider or _get_e2e_status
    e2e_status = e2e_status_provider(config)

    e2e_items = _build_e2e_items(config, e2e_status)
    e2e_total = len(e2e_items)

    e2e_items_paginated, e2e_total_pages, e2e_page = _paginate(e2e_items, e2e_page, E2E_PAGE_SIZE)
    e2e_status = dict(e2e_status)
    e2e_status["view_model"] = _build_e2e_view_model(
        e2e_status,
        e2e_items_paginated,
        e2e_total,
        e2e_page,
        e2e_total_pages,
        list((config.agents if config else {}).keys()),
    )
    issues = _select_issues_for_tab(
        active_tab, active_items, queue_items, blocked_items,
        e2e_items_paginated, awaiting_merge_items, completed_items,
    )

    queue_total_pages = (queue_total + QUEUE_PAGE_SIZE - 1) // QUEUE_PAGE_SIZE if queue_total > 0 else 1
    if queue_page > queue_total_pages:
        queue_page = queue_total_pages

    active_count = len(state.active_sessions) if state else 0
    shutdown_requested = orchestrator.shutdown_requested if orchestrator else False

    agents = config.agents if config else {}

    repo = config.repo if config else ""
    repo_root = str(config.repo_root) if config and config.repo_root else ""
    config_name = config.config_path.name if config and config.config_path else ""
    github_owner = repo.split("/")[0] if repo and "/" in repo else ""
    github_repo = repo.split("/")[1] if repo and "/" in repo else ""

    queue_refresh_seconds = config.queue_refresh_seconds if config else 600
    queue_last_refresh_age = (
        max(0.0, datetime.now(timezone.utc).timestamp() - state.queue_last_refresh_at)
        if state and state.queue_last_refresh_at > 0
        else -1
    )
    refresh_status = {
        "mode": state.queue_last_refresh_mode if state else "none",
        "lastRefreshAt": state.queue_last_refresh_at if state else 0.0,
        "lastNetworkSyncAt": state.queue_last_network_sync_at if state else 0.0,
        "lastRefreshAgeSeconds": queue_last_refresh_age,
        "lastRefreshLabel": (
            f"{_format_age_seconds(queue_last_refresh_age)} ago"
            if queue_last_refresh_age >= 0
            else "never"
        ),
        "inProgress": bool(state.queue_refresh_in_progress) if state else False,
        "requested": bool(state.queue_refresh_requested) if state else False,
        "lastFullScanAt": state.queue_last_full_scan_at if state else 0.0,
        "deltaWatermark": state.queue_delta_watermark if state else None,
        "refreshCount": state.queue_refresh_count if state else 0,
        "fetchLayerEnabled": config.fetch_layer_enabled if config else True,
        "networkSyncSeconds": config.fetch_layer_network_sync_seconds if config else 60,
        "fullScanIntervalSeconds": config.fetch_layer_full_scan_interval_seconds if config else 1800,
        "discoveryLimit": config.fetch_layer_discovery_limit if config else 25,
        "maxHotIssuesPerCycle": config.fetch_layer_max_hot_issues_per_cycle if config else 40,
        "prScanEveryNRefreshes": config.fetch_layer_pr_scan_every_n_refreshes if config else 2,
        "dependencyScanEveryNRefreshes": config.fetch_layer_dependency_scan_every_n_refreshes if config else 1,
        "flowLazyEnabled": bool(config.flow_refresh_enabled) if config else True,
        "flowStaleSeconds": int(config.flow_refresh_stale_seconds) if config else 900,
        "flowCooldownSeconds": int(config.flow_refresh_cooldown_seconds) if config else 120,
        "freshnessMode": str(config.flow_freshness_mode) if config else "balanced",
        "apiBudget": str(config.flow_api_budget) if config else "medium",
        "attentionPriority": str(config.flow_attention_priority) if config else "strict",
        "visibilityAwareEnabled": config.fetch_layer_visibility_aware_enabled if config else False,
        "selectiveSyncPlannerEnabled": config.fetch_layer_selective_sync_planner_enabled if config else False,
    }
    if config:
        milestones = config.get_filter_milestones()
        scope_summary = {
            "repo_open_total": queue_total,
            "in_scope_total": len(backlog_items),
            "filter_label": config.filtering.label or "",
            "filter_milestones": milestones,
            "exclude_labels": list(config.filtering.exclude_labels),
            "refresh_mode": state.queue_last_refresh_mode if state else "none",
            "refresh": refresh_status,
        }
    else:
        scope_summary = {
            "repo_open_total": queue_total,
            "in_scope_total": len(backlog_items),
            "filter_label": "",
            "filter_milestones": [],
            "exclude_labels": [],
            "refresh_mode": "none",
            "refresh": refresh_status,
        }

    return DashboardViewModel(
        issues=issues,
        active_items=active_items,
        queue_items=queue_items,
        blocked_items=blocked_items,
        history_items=history_items,
        e2e_items=e2e_items_paginated,
        completed_items=completed_items,
        awaiting_merge_items=awaiting_merge_items,
        flow_columns=flow_columns,
        scope_summary=scope_summary,
        active_count=len(active_items),
        queue_count=len(queue_items),
        blocked_count=len(blocked_items),
        e2e_count=e2e_total,
        completed_count=len(completed_items),
        awaiting_merge_count=len(awaiting_merge_items),
        active_tab=active_tab,
        paused=state.paused if state else False,
        shutdown_requested=shutdown_requested,
        active_session_count=active_count,
        startup_status=state.startup_status if state else "pending",
        startup_message=state.startup_message if state else "",
        repo=repo,
        repo_root=repo_root,
        config_name=config_name,
        github_owner=github_owner,
        github_repo=github_repo,
        queue_page=queue_page,
        queue_total_pages=queue_total_pages,
        queue_total=queue_total,
        queue_refresh_seconds=queue_refresh_seconds,
        e2e_status=e2e_status,
        e2e_page=e2e_page,
        e2e_total_pages=e2e_total_pages,
        e2e_total=e2e_total,
        agents=agents,
        agent_names=list(agents.keys()) if agents else [],
        open_provider_circuits=open_provider_circuits,
    )
