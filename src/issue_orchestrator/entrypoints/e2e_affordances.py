"""E2E run affordance matching shared by control and web entrypoints.

The control API and web UI must present the same test-to-issue mapping for a
run. Keeping the matching rules in one module avoids cross-surface drift while
letting the entrypoints stay focused on HTTP composition.

This module is also the canonical owner for script-facing affordance helpers so
debug tooling and HTTP surfaces keep the same windowing and label semantics as
the code is further decomposed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

_BRANCH_LABEL_MAX_LEN = 24
_BRANCH_LABEL_STRIP_SUFFIXES = (
    "-test-issue",
    "-test-data",
    "-checkpoint",
    "-status",
    "-test",
)


def _load_worktree_agent_events(repo_root: Path, run_id: int) -> list[dict]:
    """Load agent events from the E2E worktree timeline for a run."""
    from ..infra.e2e_db import E2EDB
    from ..infra.e2e_timeline import read_orchestrator_events_by_window
    from ..infra.e2e_worktree import get_e2e_worktree_path

    db_path = repo_root / ".issue-orchestrator" / "e2e.db"
    wt_timeline = get_e2e_worktree_path(repo_root) / ".issue-orchestrator" / "state" / "timeline.sqlite"
    if not db_path.exists():
        return []
    db = E2EDB(db_path)
    run = db.get_run(run_id)
    if not run or not wt_timeline.exists():
        return []
    return read_orchestrator_events_by_window(
        wt_timeline,
        started_at=run.started_at,
        finished_at=run.finished_at,
    )


def _build_test_windows(
    e2e_events: list[dict],
) -> list[tuple[str, str | None, dict]]:
    """Pair test_started/test_completed events into canonical matching windows."""
    windows: list[tuple[str, str | None, dict]] = []
    started_map: dict[str, tuple[str, dict]] = {}
    for evt in e2e_events:
        name = evt.get("event", "")
        nodeid = (evt.get("nodeid") or "").strip()
        if not nodeid:
            continue
        if name == "e2e.test_started":
            started_map[nodeid] = (evt["timestamp"], evt)
            evt.setdefault("issue_affordances", [])
        elif name == "e2e.test_completed" and nodeid in started_map:
            start_ts, started_dict = started_map.pop(nodeid)
            windows.append((start_ts, evt["timestamp"], started_dict))
            evt.setdefault("issue_affordances", started_dict["issue_affordances"])
    for start_ts, started_dict in started_map.values():
        windows.append((start_ts, None, started_dict))
    return windows


def _compact_branch_label(branch_name: str, issue_number: int) -> str | None:
    """Derive a short human-readable label from a GitHub branch name."""
    import re

    if not isinstance(branch_name, str) or not branch_name.strip():
        return None
    branch = branch_name.strip()
    prefix = f"{issue_number}-"
    if branch.startswith(prefix):
        branch = branch[len(prefix):]
    branch = re.sub(r"^m\d+-\d+-", "", branch)
    branch = re.sub(r"(^|-)e2e-", r"\1", branch)
    for suffix in _BRANCH_LABEL_STRIP_SUFFIXES:
        if branch.endswith(suffix):
            branch = branch[:-len(suffix)]
            break
    parts = [part for part in branch.split("-") if part]
    seen: set[str] = set()
    unique: list[str] = []
    for part in parts:
        if part in seen:
            continue
        seen.add(part)
        unique.append(part)
    branch = "-".join(unique)
    if not branch:
        return None
    if len(branch) > _BRANCH_LABEL_MAX_LEN:
        branch = branch[: _BRANCH_LABEL_MAX_LEN - 1] + "\u2026"
    return branch


def _collect_branch_names_by_issue(agent_events: list[dict]) -> dict[int, str]:
    """Index the first non-empty ``branch_name`` seen per issue."""
    by_issue: dict[int, str] = {}
    for evt in agent_events:
        issue_num = evt.get("issue_number")
        if not isinstance(issue_num, int) or issue_num <= 0:
            continue
        if issue_num in by_issue:
            continue
        branch = evt.get("branch_name")
        if isinstance(branch, str) and branch.strip():
            by_issue[issue_num] = branch.strip()
    return by_issue


def _assign_issue_to_window(
    windows: list[tuple[str, str | None, dict]],
    ts: str,
    issue_num: int,
    run_id: int,
    label: str | None = None,
    branch_name: str | None = None,
) -> None:
    """Append an affordance for ``issue_num`` to the first window containing ``ts``."""
    for start_ts, end_ts, parent_evt in windows:
        if ts < start_ts:
            continue
        if end_ts is not None and ts > end_ts:
            continue
        affordances = parent_evt["issue_affordances"]
        if not any(a["issue_number"] == issue_num for a in affordances):
            entry: dict[str, Any] = {"issue_number": issue_num, "run_id": run_id}
            if label:
                entry["label"] = label
            if branch_name:
                entry["branch_name"] = branch_name
            affordances.append(entry)
        return


def _issues_visible_in_view(
    agent_events: list[dict],
    view: str,
) -> set[int]:
    """Return the set of issue numbers that have at least one event visible in ``view``."""
    visible: set[int] = set()
    for evt in agent_events:
        issue_num = evt.get("issue_number")
        if not isinstance(issue_num, int) or issue_num <= 0:
            continue
        if issue_num in visible:
            continue
        if view == "raw":
            visible.add(issue_num)
            continue
        views = evt.get("views")
        if views is None or (isinstance(views, list) and view in views):
            visible.add(issue_num)
    return visible


def collect_issue_affordances(
    agent_events: list[dict],
    run_id: int,
    view: str = "user",
) -> list[dict[str, Any]]:
    """Build run-level issue timeline affordances from visible agent activity."""
    if not agent_events:
        return []

    visible_issues = _issues_visible_in_view(agent_events, view)
    branch_by_issue = _collect_branch_names_by_issue(agent_events)
    affordances: list[dict[str, Any]] = []
    seen: set[int] = set()
    for agent_evt in agent_events:
        issue_num = agent_evt.get("issue_number")
        if not isinstance(issue_num, int) or issue_num <= 0:
            continue
        if issue_num in seen or issue_num not in visible_issues:
            continue
        seen.add(issue_num)
        branch = branch_by_issue.get(issue_num)
        label = _compact_branch_label(branch, issue_num) if branch else None
        entry: dict[str, Any] = {"issue_number": issue_num, "run_id": run_id}
        if label:
            entry["label"] = label
        if branch:
            entry["branch_name"] = branch
        affordances.append(entry)
    return affordances


def _attach_issue_numbers_to_test_windows(
    e2e_events: list[dict],
    agent_events: list[dict],
    run_id: int,
    view: str = "user",
) -> list[dict]:
    """Attach issue affordances to E2E test events based on time-window matching."""
    if not agent_events:
        return e2e_events

    visible_issues = _issues_visible_in_view(agent_events, view)
    branch_by_issue = _collect_branch_names_by_issue(agent_events)
    windows = _build_test_windows(e2e_events)
    for agent_evt in agent_events:
        issue_num = agent_evt.get("issue_number")
        if not isinstance(issue_num, int) or issue_num <= 0:
            continue
        if issue_num not in visible_issues:
            continue
        branch = branch_by_issue.get(issue_num)
        label = _compact_branch_label(branch, issue_num) if branch else None
        _assign_issue_to_window(
            windows,
            agent_evt.get("timestamp", ""),
            issue_num,
            run_id,
            label=label,
            branch_name=branch,
        )

    return e2e_events


def _filter_nest_and_project_agent_events(
    e2e_events: list[dict],
    agent_events: list[dict],
    view: str,
    run_id: int,
) -> list[dict]:
    """Annotate test events with issue affordances from agent activity windows.

    The function name is preserved for compatibility, but the behavior now lives
    here so both extracted entrypoints and external tooling share one owner.
    """
    return _attach_issue_numbers_to_test_windows(
        e2e_events,
        agent_events,
        run_id=run_id,
        view=view,
    )


__all__ = [
    "_attach_issue_numbers_to_test_windows",
    "_compact_branch_label",
    "_filter_nest_and_project_agent_events",
    "_load_worktree_agent_events",
    "collect_issue_affordances",
]
