"""High-level E2E flows for orchestrator tests."""

from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

import httpx

from issue_orchestrator.control.review_scope import pr_fields_reference_issue
from issue_orchestrator.domain.issue_key import IssueKey, GitHubIssueKey

logger = logging.getLogger(__name__)
from issue_orchestrator.testing.asyncdsl import (
    OrchestratorWatcher,
    SSEEventStream,
    HTTPSnapshotProvider,
    HTTPReplayProvider,
    WatcherConfig,
)
from issue_orchestrator.testing.support.test_data import close_issue, _ensure_label
from tests.e2e.conftest import (
    DEFAULT_E2E_FILTER_LABEL,
    E2E_RUN_LABEL_PREFIX,
    E2E_TEST_LABEL_PREFIX,
    inflight_create,
    inflight_update,
    register_inflight_issue,
    ensure_inflight_refresh,
    trigger_refresh,
    wait_for_issue_seen,
    wait_for_session_started,
    wait_for_issue_label_snapshot,
    get_issue_comments,
    OrchestratorProcess,
    _github_adapter,
    poll_issue_label,
)


def review_timeout_from_config(config, default_s: float = 240.0) -> float:
    """Compute review timeout from agent config (code + review agent)."""
    try:
        code_timeout = config.agents["agent:e2e-test"].timeout_minutes
        review_agent = config.code_review_agent or "agent:script-review"
        review_timeout = config.agents[review_agent].timeout_minutes
        return float((code_timeout + review_timeout) * 60)
    except Exception:
        return float(default_s)


@dataclass
class OrchestratorRuntime:
    """Bundle for a running orchestrator + watcher."""

    orchestrator: OrchestratorProcess
    watcher: OrchestratorWatcher
    stream: SSEEventStream

    async def close(self) -> None:
        await self.watcher.close()
        await self.stream.close()
        if self.orchestrator.is_running():
            self.orchestrator.stop()


async def start_orchestrator_runtime(
    orchestrator: OrchestratorProcess,
    control_api_port: int,
    max_issues: int = 10,
    extra_args: list[str] | None = None,
) -> OrchestratorRuntime:
    orchestrator.start(max_issues=max_issues, extra_args=extra_args)
    assert orchestrator.is_running(), "Orchestrator should start"
    # Startup can take longer in production-parity runs (doctor/worktree checks).
    # Wait for control API readiness before wiring the watcher.
    deadline = time.monotonic() + 60.0
    while time.monotonic() < deadline:
        if not orchestrator.is_running():
            raise AssertionError("Orchestrator exited before control API became ready")
        if orchestrator._check_api_running():  # noqa: SLF001 - E2E helper needs process readiness probe
            break
        await asyncio.sleep(0.25)
    if not orchestrator._check_api_running():  # noqa: SLF001 - E2E helper needs process readiness probe
        raise AssertionError("Timed out waiting for control API readiness before watcher startup")
    watcher, stream = await create_watcher_for_port(control_api_port)
    return OrchestratorRuntime(orchestrator=orchestrator, watcher=watcher, stream=stream)


async def create_watcher_for_port(port: int) -> tuple[OrchestratorWatcher, SSEEventStream]:
    # Both watcher paths (this one and ``orchestrator_watcher`` in
    # conftest.py) route through ``build_watcher_clients`` so the
    # auth-wiring contract has one owner; see
    # ``tests/e2e/_watcher_auth.py``.
    from tests.e2e._watcher_auth import build_watcher_clients
    stream, snapshot_provider, replay_provider = build_watcher_clients(port)
    await stream.start()
    watcher = await OrchestratorWatcher.create(
        event_stream=stream,
        snapshot_provider=snapshot_provider,
        replay_provider=replay_provider,
        config=WatcherConfig(),
    )
    return watcher, stream


def close_pr(repo: str, pr_number: int) -> None:
    """Close a PR and delete its branch."""
    adapter = _github_adapter(repo)
    pr = adapter.get_pr(pr_number)
    if not pr:
        return
    # PRInfo has .branch attribute directly
    branch = pr.branch
    adapter.close_pr(pr_number)
    if branch:
        try:
            adapter.delete_branch(branch)
        except Exception:
            pass


def _is_e2e_cleanup_label(label: str) -> bool:
    return (
        label == DEFAULT_E2E_FILTER_LABEL
        or label.startswith(E2E_TEST_LABEL_PREFIX)
        or label.startswith(E2E_RUN_LABEL_PREFIX)
    )


def _pr_matches_issue(branch: str | None, title: str, body: str, issue_numbers: set[int]) -> bool:
    return pr_fields_reference_issue(
        branch=branch,
        title=title,
        body=body,
        issue_numbers=issue_numbers,
    )


def cleanup_test_prs_for_issues(
    repo: str,
    issue_numbers: Iterable[int],
    labels: Iterable[str],
) -> int:
    """Close e2e PRs matching created issue numbers and e2e artifact labels."""
    issue_number_set = set(issue_numbers)
    if not issue_number_set:
        return 0

    cleanup_labels = sorted({label for label in labels if _is_e2e_cleanup_label(label)})
    if not cleanup_labels:
        return 0

    closed_prs: set[int] = set()
    adapter = _github_adapter(repo)
    for label in cleanup_labels:
        try:
            prs = adapter.get_prs_with_label(label, state="open")
        except Exception:
            logger.warning("[E2E CLEANUP] Failed listing PRs for label '%s'", label)
            prs = []
        for pr in prs:
            pr_num = pr.number
            if not pr_num or pr_num in closed_prs:
                continue
            if not _pr_matches_issue(pr.branch, pr.title, pr.body, issue_number_set):
                continue
            try:
                adapter.close_pr(pr_num)
                closed_prs.add(pr_num)
                if pr.branch:
                    try:
                        adapter.delete_branch(pr.branch)
                    except Exception:
                        pass
                logger.info("[E2E CLEANUP] Closed PR #%d for test issue", pr_num)
            except Exception:
                logger.warning("[E2E CLEANUP] Failed closing PR #%d", pr_num)

    return len(closed_prs)


def cleanup_test_prs(repo: str, labels: Iterable[str]) -> int:
    """Close test PRs matching any of the provided labels."""
    closed_prs: set[int] = set()
    adapter = _github_adapter(repo)
    for label in labels:
        try:
            prs = adapter.get_prs_with_label(label, state="open")
        except Exception:
            prs = []
        for pr in prs:
            # PRInfo has .number attribute
            pr_num = pr.number
            if pr_num and pr_num not in closed_prs:
                close_pr(repo, pr_num)
                closed_prs.add(pr_num)
    return len(closed_prs)


async def wait_for_any_pr_label(
    watcher: OrchestratorWatcher,
    issue_key: str,
    labels: list[str],
    timeout_s: float,
    fail_on_blocked_failed: bool = False,
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        issue_view = watcher.view.issues.get(issue_key)
        if fail_on_blocked_failed and issue_view and "blocked-failed" in issue_view.labels:
            raise AssertionError(
                f"Issue {issue_key} hit 'blocked-failed' while waiting for PR labels"
            )
        if issue_view and issue_view.pr and issue_view.pr.labels:
            if any(label in issue_view.pr.labels for label in labels):
                return
        try:
            # noqa: SLF001 - E2E test infrastructure uses _notify for event-driven waiting
            await asyncio.wait_for(watcher._notify.wait(), timeout=1.0)  # noqa: SLF001
        except asyncio.TimeoutError:
            pass
        watcher._notify.clear()  # noqa: SLF001
    raise TimeoutError(f"Timed out waiting for PR labels {labels} on {issue_key}")


async def wait_for_issue_with_label(
    watcher: OrchestratorWatcher,
    label: str,
    timeout_s: float,
) -> str:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        for issue_key, issue_view in watcher.view.issues.items():
            if label in issue_view.labels:
                return issue_key
        try:
            # noqa: SLF001 - E2E test infrastructure uses _notify for event-driven waiting
            await asyncio.wait_for(watcher._notify.wait(), timeout=1.0)  # noqa: SLF001
        except asyncio.TimeoutError:
            pass
        watcher._notify.clear()  # noqa: SLF001
    raise TimeoutError(f"Timed out waiting for issue with label {label}")


async def wait_for_rework_progress(
    watcher: OrchestratorWatcher,
    issue_key: str,
    timeout_s: float,
    *,
    wait_for_escalation: bool = False,
    pr_number: int | None = None,
) -> tuple[bool, set[str]]:
    deadline = time.monotonic() + timeout_s
    seen: set[str] = set()
    last_labels_log = ""
    while time.monotonic() < deadline:
        # Collect labels from ALL views matching this issue.
        # The watcher may split an issue across multiple keys (e.g. 'M0-721' and '4790')
        # due to key remapping during queue refreshes.  Merge them all.
        all_labels: set[str] = set()
        for iv in _iter_issue_views(watcher, issue_key, pr_number=pr_number):
            all_labels |= set(iv.labels) | set(iv.pr.labels)

        if all_labels:
            labels_str = str(sorted(all_labels))
            if labels_str != last_labels_log:
                logger.info("[REWORK_WAIT] key=%s labels=%s", issue_key, labels_str)
                last_labels_log = labels_str
            for label in all_labels:
                if label.startswith("rework-cycle-"):
                    seen.add(label)
            if "blocked-needs-human" in all_labels or "needs-human" in all_labels:
                return True, seen
            # In non-escalation mode, return as soon as any rework label is seen
            if not wait_for_escalation and seen:
                return False, seen
        try:
            # noqa: SLF001 - E2E test infrastructure uses _notify for event-driven waiting
            await asyncio.wait_for(watcher._notify.wait(), timeout=2.0)  # noqa: SLF001
        except asyncio.TimeoutError:
            pass
        watcher._notify.clear()  # noqa: SLF001
    return False, seen


def _iter_issue_views(
    watcher: OrchestratorWatcher,
    issue_key: str,
    *,
    pr_number: int | None = None,
) -> list[IssueView]:
    """Collect all IssueView entries that might represent this issue.

    The watcher materializer can create multiple entries for the same logical
    issue under different keys (stable_id like 'M0-721', issue number like '4790',
    or even PR number like '4791') because queue.changed and label events may use
    different key formats across ticks.

    We gather all of them so callers can merge their labels.

    Note: most pr.view_changed events now emit the stable issue_key, but
    queue.changed and label events from the tracker may still use numeric keys.
    Keep the merge-all fallback until all event sources are aligned.
    """
    # When we have a pr_number, return ALL views.
    # The orchestrator test runs a single issue, and the materializer may split
    # events across multiple keys.  Merging all views ensures we see every label.
    if pr_number is not None:
        return list(watcher.view.issues.values())
    # Without pr_number, fall back to direct key match only.
    iv = watcher.view.issues.get(issue_key)
    return [iv] if iv is not None else []


def check_issue_comment(
    repo: str,
    issue_number: int,
    predicate: Callable[[dict], bool],
) -> dict | None:
    """Single-shot check for a comment matching predicate.

    This is a boundary check - it reads GitHub once at a known point
    (after a flow completes) rather than polling in a loop.

    Per architecture objectives: "Direct GH reads only for setup/cleanup"
    and "refresh only at known boundaries."
    """
    comments = get_issue_comments(repo, issue_number)
    for comment in comments:
        if predicate(comment):
            return comment
    return None


async def wait_for_issue_comment(
    repo: str,
    issue_number: int,
    predicate: Callable[[dict], bool],
    timeout_s: float = 10,
    interval_s: float = 2,
) -> dict | None:
    """DEPRECATED: Use check_issue_comment instead.

    This function polls GitHub in a loop, which violates the architecture
    objective: "no direct GH reads in waits". Use check_issue_comment
    for a single boundary check after the flow completes.
    """
    import warnings
    warnings.warn(
        "wait_for_issue_comment polls GitHub directly. "
        "Use check_issue_comment for a single boundary check.",
        DeprecationWarning,
        stacklevel=2,
    )
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        comments = get_issue_comments(repo, issue_number)
        for comment in comments:
            if predicate(comment):
                return comment
        await asyncio.sleep(interval_s)
    return None


@dataclass
class E2EFlow:
    """High-level test flow helpers for e2e tests.

    The control API port is derived from the watcher's snapshot provider URL,
    not passed as a parameter. This ensures tests can't accidentally send
    refresh requests to the wrong port.
    """

    repo: str
    watcher: OrchestratorWatcher | None
    filter_label: str | None = None
    review_timeout_s: float | None = None
    fail_on_blocked_failed: bool = False
    _created_issues: list[int] = field(default_factory=list, repr=False)
    _created_issue_labels: dict[int, tuple[str, ...]] = field(default_factory=dict, repr=False)

    def _control_api_port(self) -> int | None:
        """Extract control API port from watcher's snapshot provider URL.

        Returns None if watcher is not available (falls back to env var in trigger_refresh).
        """
        if self.watcher is None:
            return None
        # Extract port from snapshot provider URL (e.g., http://localhost:19080/api/snapshot)
        # noqa: SLF001 - E2E test infrastructure needs port from provider for control API
        url = self.watcher._snapshot_provider.url  # noqa: SLF001
        from urllib.parse import urlparse
        parsed = urlparse(url)
        return parsed.port

    def _default_review_timeout_s(self) -> float:
        if self.review_timeout_s is not None:
            return self.review_timeout_s
        env_timeout = os.environ.get("E2E_REVIEW_TIMEOUT_S")
        if env_timeout:
            try:
                return float(env_timeout)
            except ValueError:
                pass
        return 240.0

    def refresh(self) -> bool:
        return trigger_refresh(self._control_api_port())

    def create_issue(
        self,
        title: str,
        labels: list[str],
        body: str = "Created mid-test.",
    ) -> tuple[IssueKey, int]:
        """Create a GitHub issue and return (key, issue_number).

        Returns:
            Tuple of (IssueKey, issue_number) - the key uses external_id from title prefix,
            issue_number is the GitHub issue number
        """
        merged = list(labels)
        if self.filter_label and self.filter_label not in merged:
            merged.append(self.filter_label)
        issue_key, issue_number = inflight_create(self.repo, title, merged, body=body)
        self._created_issues.append(issue_number)
        self._created_issue_labels[issue_number] = tuple(merged)
        if self.watcher is not None:
            register_inflight_issue(issue_key)
        return issue_key, issue_number

    def cleanup_created_prs(self) -> int:
        """Close PRs created for issues owned by this flow."""
        labels: set[str] = set()
        for issue_labels in self._created_issue_labels.values():
            labels.update(issue_labels)
        return cleanup_test_prs_for_issues(self.repo, self._created_issues, labels)

    def cleanup_created_issues(self) -> None:
        """Close PRs and issues created by this flow."""
        from issue_orchestrator.testing.support.test_data import close_issue
        self.cleanup_created_prs()
        for issue_number in self._created_issues:
            try:
                close_issue(self.repo, issue_number)
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(
                    "Failed to close issue #%d: %s", issue_number, e
                )

    def update_issue(
        self,
        issue: IssueKey,
        add_labels: list[str] | None = None,
        remove_labels: list[str] | None = None,
    ) -> None:
        inflight_update(issue, add_labels=add_labels, remove_labels=remove_labels, port=self._control_api_port())

    async def issue_seen(self, issue: IssueKey, timeout_s: float = 120) -> None:
        self._refresh_if_needed()
        await wait_for_issue_seen(
            self._watcher(),
            issue.stable_id(),
            timeout_s=timeout_s,
            fail_on_blocked_failed=self.fail_on_blocked_failed,
        )

    async def session_started(self, issue: IssueKey, timeout_s: float = 120) -> None:
        self._refresh_if_needed()
        await wait_for_session_started(
            self._watcher(),
            issue.stable_id(),
            timeout_s=timeout_s,
            fail_on_blocked_failed=self.fail_on_blocked_failed,
        )

    async def issue_has_label(self, issue: IssueKey, label: str, timeout_s: float = 60) -> None:
        self._refresh_if_needed()
        await wait_for_issue_label_snapshot(
            self._watcher(),
            issue.stable_id(),
            label,
            timeout_s=timeout_s,
            fail_on_blocked_failed=self.fail_on_blocked_failed,
        )

    async def event(
        self,
        event_type: str | "EventName",
        predicate: Callable[[dict], bool] | None = None,
        timeout_s: float = 60,
    ) -> None:
        self._refresh_if_needed()
        await self._watcher().system().event(event_type, predicate=predicate, timeout_s=timeout_s)

    async def issue_event(
        self,
        issue: IssueKey,
        event_type: str | "EventName",
        predicate: Callable[[dict], bool] | None = None,
        timeout_s: float = 60,
    ) -> None:
        self._refresh_if_needed()
        await self._watcher().issue(issue.stable_id()).event(
            event_type,
            predicate=predicate,
            timeout_s=timeout_s,
        )

    async def pr_created(self, issue: IssueKey, timeout_s: float = 120) -> int:
        self._refresh_if_needed()
        watcher = self._watcher()
        deadline = time.monotonic() + timeout_s
        issue_key = issue.stable_id()
        while time.monotonic() < deadline:
            issue_view = watcher.view.issues.get(issue_key)
            if self.fail_on_blocked_failed and issue_view and "blocked-failed" in issue_view.labels:
                raise AssertionError(
                    f"Issue {issue_key} hit 'blocked-failed' while waiting for PR"
                )
            if issue_view and issue_view.pr.number:
                return issue_view.pr.number
            try:
                # noqa: SLF001 - E2E test infrastructure uses _notify for event-driven waiting
                await asyncio.wait_for(watcher._notify.wait(), timeout=1.0)  # noqa: SLF001
            except asyncio.TimeoutError:
                pass
            watcher._notify.clear()  # noqa: SLF001
        raise TimeoutError(f"Timed out waiting for PR for issue {issue_key}")

    async def pr_has_any_label(
        self,
        issue: IssueKey,
        labels: list[str],
        timeout_s: float,
    ) -> None:
        self._refresh_if_needed()
        await wait_for_any_pr_label(
            self._watcher(),
            issue.stable_id(),
            labels,
            timeout_s=timeout_s,
            fail_on_blocked_failed=self.fail_on_blocked_failed,
        )

    async def review_outcomes_any_of(
        self,
        issues: list[IssueKey],
        any_of_labels: list[str],
        timeout_s: float | None = None,
    ) -> None:
        """Wait for any of the provided review outcome labels on all issues."""
        timeout_s = timeout_s if timeout_s is not None else self._default_review_timeout_s()
        await asyncio.gather(*[
            self.pr_has_any_label(issue, labels=any_of_labels, timeout_s=timeout_s)
            for issue in issues
        ])


    async def rework_progress(
        self,
        issue: IssueKey,
        timeout_s: float,
        *,
        wait_for_escalation: bool = False,
        pr_number: int | None = None,
    ) -> tuple[bool, set[str]]:
        self._refresh_if_needed()
        return await wait_for_rework_progress(
            self._watcher(), issue.stable_id(), timeout_s=timeout_s,
            wait_for_escalation=wait_for_escalation,
            pr_number=pr_number,
        )

    def close_pr(self, pr_number: int) -> None:
        close_pr(self.repo, pr_number)

    def close_issue(self, issue_number: int, comment: str) -> None:
        """Close an issue by its GitHub issue number."""
        close_issue(self.repo, issue_number, comment)

    def ensure_labels(self, labels: Iterable[str]) -> None:
        for label in labels:
            _ensure_label(self.repo, label)

    def _refresh_if_needed(self) -> None:
        if self.watcher is None:
            return
        ensure_inflight_refresh(self._control_api_port())

    def _watcher(self) -> OrchestratorWatcher:
        if self.watcher is None:
            raise RuntimeError("Watcher required for this operation")
        return self.watcher


def issue_key_for_number(repo: str, issue_number: int) -> IssueKey:
    return GitHubIssueKey(repo=repo, external_id=str(issue_number))
