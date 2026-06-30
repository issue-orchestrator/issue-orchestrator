"""E2E async wait helper functions."""

import asyncio
import json
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any

from issue_orchestrator.events.catalog import EventName

if TYPE_CHECKING:
    from issue_orchestrator.testing.asyncdsl import OrchestratorWatcher



def _raise_if_blocked_failed(issue_view: Any | None, issue_key: str) -> None:
    if issue_view and "blocked-failed" in issue_view.labels:
        raise AssertionError(f"Issue {issue_key} hit blocked-failed while waiting")


async def wait_for_file_with_content(
    file_path: Path,
    content_check: str | None = None,
    timeout_s: float = 60,
    poll_interval_s: float = 0.5,
) -> dict[str, Any]:
    """Wait for a JSON file to exist and be fully written.

    For JSON files, "fully written" means the file can be parsed as valid JSON.
    This avoids race conditions where the file exists but is only partially written.

    Args:
        file_path: Path to the JSON file to wait for
        content_check: Optional string that must appear in the file (e.g., '"outcome": "completed"')
        timeout_s: Maximum time to wait
        poll_interval_s: How often to check

    Returns:
        Parsed JSON content

    Raises:
        TimeoutError: If file doesn't exist or isn't valid JSON within timeout
    """
    deadline = time.monotonic() + timeout_s
    last_error = None

    while time.monotonic() < deadline:
        if file_path.exists():
            try:
                content = file_path.read_text()
                # Check for optional content marker
                if content_check and content_check not in content:
                    last_error = f"Content check '{content_check}' not found"
                    await asyncio.sleep(poll_interval_s)
                    continue
                # Try to parse as JSON - if this succeeds, file is fully written
                data = json.loads(content)
                return data
            except json.JSONDecodeError as e:
                # File exists but isn't valid JSON yet - still being written
                last_error = f"JSON parse error: {e}"
            except Exception as e:
                last_error = f"Read error: {e}"

        await asyncio.sleep(poll_interval_s)

    raise TimeoutError(
        f"Timed out waiting for valid JSON file at {file_path}.\n"
        f"File exists: {file_path.exists()}\n"
        f"Last error: {last_error}"
    )


async def wait_for_issue_seen(
    watcher: "OrchestratorWatcher",
    issue_key: str,
    timeout_s: float,
    fail_on_blocked_failed: bool = False,
) -> None:
    """Wait for an issue to appear via SSE events (queue.changed)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        issue_view = watcher.view.issues.get(issue_key)
        if fail_on_blocked_failed:
            _raise_if_blocked_failed(issue_view, issue_key)
        if issue_view:
            return
        try:
            # noqa: SLF001 - E2E test infrastructure uses _notify for event-driven waiting
            await asyncio.wait_for(watcher._notify.wait(), timeout=1.0)  # noqa: SLF001
        except asyncio.TimeoutError:
            pass
        watcher._notify.clear()  # noqa: SLF001
    raise TimeoutError(f"Timed out waiting for issue {issue_key} to appear in snapshot")


async def wait_for_session_started(
    watcher: "OrchestratorWatcher",
    issue_key: str,
    timeout_s: float,
    fail_on_blocked_failed: bool = False,
) -> None:
    """Wait until a session starts (in-progress label or PR created)."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        issue_view = watcher.view.issues.get(issue_key)
        if fail_on_blocked_failed:
            _raise_if_blocked_failed(issue_view, issue_key)
        if issue_view:
            if "in-progress" in issue_view.labels or issue_view.pr.number is not None:
                return
        try:
            # noqa: SLF001 - E2E test infrastructure uses _notify for event-driven waiting
            await asyncio.wait_for(watcher._notify.wait(), timeout=1.0)  # noqa: SLF001
        except asyncio.TimeoutError:
            pass
        watcher._notify.clear()  # noqa: SLF001
    raise TimeoutError(f"Timed out waiting for session start or PR for {issue_key}")


async def wait_for_issue_label_snapshot(
    watcher: "OrchestratorWatcher",
    issue_key: str,
    label: str,
    timeout_s: float,
    fail_on_blocked_failed: bool = False,
) -> None:
    """Wait for a label to appear on an issue via SSE events."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        issue_view = watcher.view.issues.get(issue_key)
        if fail_on_blocked_failed:
            _raise_if_blocked_failed(issue_view, issue_key)
        if issue_view and label in issue_view.labels:
            return
        try:
            # noqa: SLF001 - E2E test infrastructure uses _notify for event-driven waiting
            await asyncio.wait_for(watcher._notify.wait(), timeout=1.0)  # noqa: SLF001
        except asyncio.TimeoutError:
            pass
        watcher._notify.clear()  # noqa: SLF001
    raise TimeoutError(f"Timed out waiting for label {label} on {issue_key}")


async def wait_for_session_completed(
    watcher: "OrchestratorWatcher",
    issue_key: str,
    timeout_s: float,
    fail_on_blocked_failed: bool = False,
) -> dict:
    """Wait for a session.completed or session.processing_completed event.

    This verifies that:
    1. coding-done/reviewer-done wrote completion.json
    2. The orchestrator FOUND it (in the worktree, not main repo)
    3. The orchestrator PROCESSED it

    Returns the completion event payload for further verification.

    Raises TimeoutError if no completion event is received.
    """
    import logging

    logger = logging.getLogger(__name__)

    deadline = time.monotonic() + timeout_s
    # Events that indicate session completion was processed
    # Use EventName constants for type-safe event matching
    completion_events = {
        EventName.SESSION_COMPLETED.value,
        EventName.SESSION_PROCESSING_COMPLETED.value,
        EventName.SESSION_FAILED.value,
    }

    while time.monotonic() < deadline:
        if fail_on_blocked_failed:
            issue_view = watcher.view.issues.get(issue_key)
            _raise_if_blocked_failed(issue_view, issue_key)
        # Check all global events for session completion
        for event in watcher.view.global_events:
            etype = event.get("type", "")
            payload = event.get("payload", {}) or {}

            # Match session events by issue_number in payload
            if etype in completion_events:
                event_issue = str(payload.get("issue_number", ""))
                if event_issue == issue_key:
                    logger.info("Found completion event: type=%s issue=%s", etype, event_issue)
                    return event

        try:
            # noqa: SLF001 - E2E test infrastructure uses _notify for event-driven waiting
            await asyncio.wait_for(watcher._notify.wait(), timeout=2.0)  # noqa: SLF001
        except asyncio.TimeoutError:
            pass
        watcher._notify.clear()  # noqa: SLF001

    # Build diagnostic info
    all_event_types = [e.get("type") for e in watcher.view.global_events]
    session_events = [
        {"type": e.get("type"), "payload": e.get("payload", {})}
        for e in watcher.view.global_events
        if e.get("type", "").startswith("session.")
    ]
    issue_view = watcher.view.issues.get(issue_key)
    labels = list(issue_view.labels) if issue_view else []

    raise TimeoutError(
        f"Timed out waiting for session.completed event on issue {issue_key}.\n"
        f"This means either:\n"
        f"  1. coding-done/reviewer-done was never called\n"
        f"  2. completion.json was written to wrong location (cd fix broken?)\n"
        f"  3. orchestrator failed to process completion\n"
        f"Labels: {labels}\n"
        f"All event types seen: {set(all_event_types)}\n"
        f"Session events: {session_events}"
    )


async def wait_for_review_exchange_completed(
    watcher: "OrchestratorWatcher",
    *,
    issue_number: int,
    timeout_s: float,
) -> dict[str, Any]:
    """Wait for the latest successful review-exchange completion event."""
    deadline = time.monotonic() + timeout_s
    last_payload: dict[str, Any] | None = None

    while time.monotonic() < deadline:
        for event in reversed(watcher.view.global_events):
            payload = event.get("payload") or {}
            if not isinstance(payload, dict):
                continue
            if (
                event.get("type") == EventName.REVIEW_EXCHANGE_FAILED.value
                and str(payload.get("issue_number")) == str(issue_number)
            ):
                raise AssertionError(
                    f"Review exchange failed for issue {issue_number}: {payload}"
                )
            if (
                event.get("type") == EventName.REVIEW_EXCHANGE_COMPLETED.value
                and str(payload.get("issue_number")) == str(issue_number)
            ):
                last_payload = payload
                if payload.get("status") != "ok":
                    raise AssertionError(
                        "Review exchange completed without ok status: "
                        f"{payload}"
                    )
                run_dir = payload.get("run_dir")
                if isinstance(run_dir, str) and run_dir:
                    return payload

        try:
            # noqa: SLF001 - E2E test infrastructure uses _notify for event-driven waiting
            await asyncio.wait_for(watcher._notify.wait(), timeout=2.0)  # noqa: SLF001
        except asyncio.TimeoutError:
            pass
        watcher._notify.clear()  # noqa: SLF001

    raise TimeoutError(
        f"Timed out waiting for review exchange completion for issue {issue_number}. "
        f"Last payload: {last_payload}. Recent events: "
        f"{list(watcher.view.global_events)[-20:]}"
    )
