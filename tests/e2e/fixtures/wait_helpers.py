"""E2E async wait helper functions."""

import asyncio
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from issue_orchestrator.testing.asyncdsl import OrchestratorWatcher


async def wait_for_issue_seen(
    watcher: "OrchestratorWatcher",
    issue_key: str,
    timeout_s: float,
) -> None:
    """Wait for an issue to appear via SSE events (queue.changed)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if issue_key in watcher.view.issues:
            return
        try:
            await asyncio.wait_for(watcher._notify.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
        watcher._notify.clear()
    raise TimeoutError(f"Timed out waiting for issue {issue_key} to appear in snapshot")


async def wait_for_session_started(
    watcher: "OrchestratorWatcher",
    issue_key: str,
    timeout_s: float,
) -> None:
    """Wait until a session starts (in-progress label or PR created)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        issue_view = watcher.view.issues.get(issue_key)
        if issue_view:
            if "in-progress" in issue_view.labels or issue_view.pr.number is not None:
                return
        try:
            await asyncio.wait_for(watcher._notify.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
        watcher._notify.clear()
    raise TimeoutError(f"Timed out waiting for session start or PR for {issue_key}")


async def wait_for_issue_label_snapshot(
    watcher: "OrchestratorWatcher",
    issue_key: str,
    label: str,
    timeout_s: float,
) -> None:
    """Wait for a label to appear on an issue via SSE events."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        issue_view = watcher.view.issues.get(issue_key)
        if issue_view and label in issue_view.labels:
            return
        try:
            await asyncio.wait_for(watcher._notify.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
        watcher._notify.clear()
    raise TimeoutError(f"Timed out waiting for label {label} on {issue_key}")
