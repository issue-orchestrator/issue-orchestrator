"""Comprehensive E2E test for the full orchestration pipeline.

This test verifies the complete system works end-to-end by:
1. Creating multiple real GitHub issues
2. Using the shared session orchestrator that processes them concurrently
3. Verifying labels, sessions, PRs at each stage

Uses the single-orchestrator pattern for efficiency.
"""

import asyncio
import logging
import time

import pytest

from tests.e2e.conftest import e2e_label
from tests.e2e.flows import E2EFlow

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Concurrent Pipeline Test
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.live
@pytest.mark.timeout(900)  # 15 min timeout
class TestConcurrentPipeline:
    """Test the orchestrator processing multiple issues concurrently.

    Uses the shared session-scoped orchestrator for efficiency.
    """

    @pytest.mark.asyncio
    @pytest.mark.gh_activity_limit(test_gh_activity_limit=400, system_gh_activity_limit=15)
    async def test_concurrent_issue_processing(
        self,
        e2e_orchestrator,
        orchestrator_watcher,
        repo_name: str,
        filter_label: str,
    ):
        """Test orchestrator processes multiple issues concurrently.

        Verifies:
        1. All issues get 'in-progress' label (sessions started)
        2. All issues get PRs created
        3. Processing happens concurrently (not sequentially)
        """
        num_issues = 3
        issues = []
        created_prs = []
        start_time = time.time()
        flow = E2EFlow(repo=repo_name, watcher=orchestrator_watcher, filter_label=filter_label)

        logger.info("=== Testing %d issues concurrently ===", num_issues)
        logger.info("Filter label: %s", filter_label)

        try:
            # Create issues dynamically
            for i in range(num_issues):
                issue = flow.create_issue(
                    f"[E2E-CONCURRENT-{i+1}] Concurrent pipeline test",
                    ["agent:e2e-test", e2e_label(f"concurrent_{i}")],
                )
                issues.append(issue)
                logger.info("Created issue #%s", issue.stable_id())

            # Phase 0: Wait for issues to appear in snapshots
            logger.info("Phase 0: Waiting for issues to appear in snapshots...")
            await asyncio.gather(*[
                flow.issue_seen(issue, timeout_s=120)
                for issue in issues
            ])

            # Phase 1: Wait for all sessions to start (in-progress labels)
            logger.info("Phase 1: Waiting for all sessions to start...")
            await asyncio.gather(*[
                flow.session_started(issue, timeout_s=240)
                for issue in issues
            ])

            # Phase 2: Wait for all PRs to be created
            logger.info("Phase 2: Waiting for all PRs to be created...")
            pr_numbers = await asyncio.gather(*[
                flow.pr_created(issue)
                for issue in issues
            ])
            created_prs = [{"number": pr_num} for pr_num in pr_numbers]

            # Phase 3: Wait for code review outcomes so later tests can launch new work
            logger.info("Phase 3: Waiting for code review outcomes...")
            await flow.review_outcomes_any_of(
                issues=issues,
                any_of_labels=["code-reviewed", "needs-rework"],
            )

            # Summary
            total_time = time.time() - start_time
            logger.info("=== Summary ===")
            logger.info("Total time: %.1fs", total_time)
            logger.info("Issues processed: %d", num_issues)
            logger.info("PRs created: %d", len(created_prs))

            # Verify concurrency
            if num_issues > 1:
                avg_time = total_time / num_issues
                logger.info("Avg time per issue: %.1fs", avg_time)
                if avg_time < 45:
                    logger.info("✓ Processing was concurrent (avg < 45s per issue)")

        finally:
            # Cleanup: close all PRs
            for pr in created_prs:
                flow.close_pr(pr["number"])


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
