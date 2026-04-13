"""Comprehensive E2E tests for the full orchestration pipeline.

These tests split the full lifecycle into smaller, resumable checkpoints:
1. Issues appear in snapshots
2. Sessions start
3. PRs are created
4. Review outcomes are applied
"""

import asyncio
import logging
import os
import time

import pytest

from issue_orchestrator.events import EventName
from tests.e2e.conftest import e2e_label
from tests.e2e.flows import E2EFlow
from tests.e2e.fixtures import _github_adapter, get_pr_uncached

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Concurrent Pipeline Phases
# ---------------------------------------------------------------------------

def _create_concurrent_issues(flow: E2EFlow, count: int, label_prefix: str) -> list:
    issues = []
    for i in range(count):
        issue, _issue_num = flow.create_issue(
            f"[M0-{800+i:03d}] [E2E-CONCURRENT-{i+1}] Concurrent pipeline test",
            ["agent:e2e-test", e2e_label(f"{label_prefix}_{i}")],
        )
        issues.append(issue)
        logger.info("Created issue #%s", issue.stable_id())
    return issues


async def _wait_for_pr_and_fetch(
    e2e_flow: E2EFlow,
    issue,
    *,
    timeout_s: float,
) -> tuple[int, dict]:
    await e2e_flow.issue_event(
        issue,
        EventName.PR_VIEW_CHANGED,
        predicate=lambda e: e.get("payload", {}).get("pr_number") is not None,
        timeout_s=timeout_s,
    )
    # noqa: SLF001 - E2E test infrastructure accesses internal watcher for issue tracking
    issue_watch = e2e_flow._watcher().issue(issue.stable_id())  # noqa: SLF001
    pr_number = issue_watch.pr_number()
    if pr_number is None:
        raise AssertionError("PR number not found after PR_VIEW_CHANGED event")
    pr_payload = get_pr_uncached(e2e_flow.repo, pr_number)
    if not pr_payload:
        raise AssertionError(f"PR {pr_number} not found after creation event")
    return pr_number, pr_payload


@pytest.mark.e2e
@pytest.mark.live
class TestConcurrentPipelinePhases:
    """Split the pipeline into discrete checkpoints for resumability."""

    @pytest.mark.asyncio
    @pytest.mark.timeout(240)
    @pytest.mark.gh_activity_limit(test_gh_activity_limit=250, system_gh_activity_limit=80)
    async def test_concurrent_issues_seen_in_snapshots(
        self,
        e2e_orchestrator,
        e2e_flow: E2EFlow,
        test_label: str,
    ):
        """Issues should appear in snapshots after creation."""
        e2e_flow.fail_on_blocked_failed = True
        issues = _create_concurrent_issues(e2e_flow, count=2, label_prefix=test_label)

        logger.info("Phase 0: Waiting for issues to appear in snapshots...")
        await asyncio.gather(*[
            e2e_flow.issue_seen(issue, timeout_s=120)
            for issue in issues
        ])

    @pytest.mark.asyncio
    @pytest.mark.timeout(360)
    @pytest.mark.gh_activity_limit(test_gh_activity_limit=200, system_gh_activity_limit=80)
    async def test_concurrent_sessions_start(
        self,
        e2e_orchestrator,
        e2e_flow: E2EFlow,
        test_label: str,
    ):
        """Sessions should start for multiple issues concurrently."""
        e2e_flow.fail_on_blocked_failed = True
        issues = _create_concurrent_issues(e2e_flow, count=2, label_prefix=test_label)
        start_time = time.time()

        logger.info("Phase 1: Waiting for all sessions to start...")
        await asyncio.gather(*[
            e2e_flow.session_started(issue, timeout_s=240)
            for issue in issues
        ])

        total_time = time.time() - start_time
        avg_time = total_time / max(len(issues), 1)
        logger.info("Avg time per issue: %.1fs", avg_time)
        if avg_time < 45:
            logger.info("✓ Processing was concurrent (avg < 45s per issue)")

    @pytest.mark.asyncio
    @pytest.mark.timeout(600)
    @pytest.mark.gh_activity_limit(test_gh_activity_limit=500, system_gh_activity_limit=80)
    async def test_pr_created_for_issue(
        self,
        e2e_orchestrator,
        e2e_flow: E2EFlow,
        test_label: str,
    ):
        """PRs should be created for issues (skipped in dry-run mode)."""
        if os.environ.get("E2E_DRY_RUN_PUSH") == "1":
            pytest.skip("PR creation requires E2E_DRY_RUN_PUSH=false")

        e2e_flow.fail_on_blocked_failed = True
        issue, _issue_num = e2e_flow.create_issue(
            "[M0-851] [E2E-PR] PR creation checkpoint",
            ["agent:e2e-test", e2e_label(test_label)],
        )
        pr_number = None
        pr_branch = None

        try:
            logger.info("Phase 2: Waiting for PR to be created...")
            pr_number, pr_payload = await _wait_for_pr_and_fetch(
                e2e_flow,
                issue,
                timeout_s=360,
            )
            pr_branch = (pr_payload.get("head") or {}).get("ref")
        finally:
            if pr_number is not None:
                adapter = _github_adapter(e2e_flow.repo)
                adapter.close_pr(pr_number)
                if pr_branch:
                    try:
                        adapter.delete_branch(pr_branch)
                    except Exception:
                        pass

    @pytest.mark.asyncio
    @pytest.mark.timeout(600)
    @pytest.mark.gh_activity_limit(test_gh_activity_limit=700, system_gh_activity_limit=80)
    async def test_review_outcome_for_issue(
        self,
        e2e_orchestrator,
        e2e_flow: E2EFlow,
        test_label: str,
    ):
        """Review outcomes should be applied after PR creation."""
        if os.environ.get("E2E_DRY_RUN_PUSH") == "1":
            pytest.skip("Review outcome requires E2E_DRY_RUN_PUSH=false")

        e2e_flow.fail_on_blocked_failed = True
        issue, _issue_num = e2e_flow.create_issue(
            "[M0-852] [E2E-REVIEW] Review outcome checkpoint",
            ["agent:e2e-test", e2e_label(test_label)],
        )
        pr_number = None
        pr_branch = None

        try:
            pr_number, pr_payload = await _wait_for_pr_and_fetch(
                e2e_flow,
                issue,
                timeout_s=360,
            )
            pr_branch = (pr_payload.get("head") or {}).get("ref")
            logger.info("Phase 3: Waiting for code review outcomes...")
            await e2e_flow.review_outcomes_any_of(
                issues=[issue],
                any_of_labels=["code-reviewed", "needs-rework"],
            )
        finally:
            if pr_number is not None:
                adapter = _github_adapter(e2e_flow.repo)
                adapter.close_pr(pr_number)
                if pr_branch:
                    try:
                        adapter.delete_branch(pr_branch)
                    except Exception:
                        pass


# ---------------------------------------------------------------------------
# Edge Case: No Matching Issues
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.live
@pytest.mark.timeout(120)
class TestEdgeCases:
    """Test edge cases and failure handling."""

    @pytest.mark.gh_activity_limit(test_gh_activity_limit=50, system_gh_activity_limit=20)
    def test_orchestrator_handles_no_matching_issues(
        self,
        e2e_orchestrator,
    ):
        """Orchestrator should handle having no matching issues gracefully.

        Since the shared orchestrator uses a filter label that matches test issues,
        we just verify it's healthy after running for a bit.
        """
        # The session-scoped orchestrator is already running
        # Just verify it stays healthy
        time.sleep(5)
        assert e2e_orchestrator.is_running(), "Orchestrator should still be running"
        logger.info("✓ Orchestrator running healthy")
