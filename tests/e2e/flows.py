"""High-level E2E flows for orchestrator tests."""

from __future__ import annotations

import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Callable, Iterable

from issue_orchestrator.domain.issue_key import IssueKey, GitHubIssueKey
from issue_orchestrator.testing.asyncdsl import (
    OrchestratorWatcher,
    SSEEventStream,
    HTTPSnapshotProvider,
    HTTPReplayProvider,
    WatcherConfig,
)
from issue_orchestrator.testing.support.test_data import close_issue, _ensure_label
from tests.e2e.conftest import (
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
    watcher, stream = await create_watcher_for_port(control_api_port)
    return OrchestratorRuntime(orchestrator=orchestrator, watcher=watcher, stream=stream)


async def create_watcher_for_port(port: int) -> tuple[OrchestratorWatcher, SSEEventStream]:
    stream = SSEEventStream(f"http://localhost:{port}/api/events")
    await stream.start()
    snapshot_provider = HTTPSnapshotProvider(f"http://localhost:{port}/api/snapshot")
    replay_provider = HTTPReplayProvider(f"http://localhost:{port}/api/events_since")
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
) -> None:
    tasks = [
        asyncio.create_task(
            watcher.issue(issue_key).pr_has_label(label, timeout_s=timeout_s)
        )
        for label in labels
    ]
    done, pending = await asyncio.wait(
        tasks,
        timeout=timeout_s,
        return_when=asyncio.FIRST_COMPLETED,
    )
    for task in pending:
        task.cancel()
    if not done:
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
            await asyncio.wait_for(watcher._notify.wait(), timeout=1.0)
        except asyncio.TimeoutError:
            pass
        watcher._notify.clear()
    raise TimeoutError(f"Timed out waiting for issue with label {label}")


async def wait_for_rework_progress(
    watcher: OrchestratorWatcher,
    issue_key: str,
    timeout_s: float,
) -> tuple[bool, set[str]]:
    deadline = time.monotonic() + timeout_s
    seen: set[str] = set()
    while time.monotonic() < deadline:
        issue_view = watcher.view.issues.get(issue_key)
        if issue_view:
            labels = issue_view.pr.labels
            for label in labels:
                if label.startswith("rework-cycle-"):
                    seen.add(label)
            if "blocked-needs-human" in labels or "needs-human" in labels:
                return True, seen
            if seen:
                return False, seen
        try:
            await asyncio.wait_for(watcher._notify.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass
        watcher._notify.clear()
    return False, seen


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
    _created_issues: list[int] = field(default_factory=list, repr=False)

    def _control_api_port(self) -> int | None:
        """Extract control API port from watcher's snapshot provider URL.

        Returns None if watcher is not available (falls back to env var in trigger_refresh).
        """
        if self.watcher is None:
            return None
        # Extract port from snapshot provider URL (e.g., http://localhost:19080/api/snapshot)
        url = self.watcher._snapshot_provider.url
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
    ) -> IssueKey:
        merged = list(labels)
        if self.filter_label and self.filter_label not in merged:
            merged.append(self.filter_label)
        issue_key, issue_number = inflight_create(self.repo, title, merged, body=body)
        self._created_issues.append(issue_number)
        if self.watcher is not None:
            register_inflight_issue(issue_key)
        return issue_key

    def cleanup_created_issues(self) -> None:
        """Close all issues created by this flow."""
        from issue_orchestrator.testing.support.test_data import close_issue
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
        await wait_for_issue_seen(self._watcher(), issue.stable_id(), timeout_s=timeout_s)

    async def session_started(self, issue: IssueKey, timeout_s: float = 120) -> None:
        self._refresh_if_needed()
        await wait_for_session_started(self._watcher(), issue.stable_id(), timeout_s=timeout_s)

    async def issue_has_label(self, issue: IssueKey, label: str, timeout_s: float = 60) -> None:
        self._refresh_if_needed()
        await wait_for_issue_label_snapshot(self._watcher(), issue.stable_id(), label, timeout_s=timeout_s)

    async def pr_created(self, issue: IssueKey, timeout_s: float = 120) -> int:
        self._refresh_if_needed()
        watcher = self._watcher()
        await watcher.issue(issue.stable_id()).has_pr(timeout_s=timeout_s)
        issue_view = watcher.view.issues.get(issue.stable_id())
        if not issue_view or not issue_view.pr.number:
            raise AssertionError(f"PR should be created for issue {issue.stable_id()}")
        return issue_view.pr.number

    async def pr_has_any_label(
        self,
        issue: IssueKey,
        labels: list[str],
        timeout_s: float,
    ) -> None:
        self._refresh_if_needed()
        await wait_for_any_pr_label(self._watcher(), issue.stable_id(), labels, timeout_s=timeout_s)

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
    ) -> tuple[bool, set[str]]:
        self._refresh_if_needed()
        return await wait_for_rework_progress(self._watcher(), issue.stable_id(), timeout_s=timeout_s)

    def close_pr(self, pr_number: int) -> None:
        close_pr(self.repo, pr_number)

    def close_issue(self, issue: IssueKey, comment: str) -> None:
        close_issue(self.repo, int(issue.stable_id()), comment)

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
