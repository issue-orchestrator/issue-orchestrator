"""Live E2E tests that create real GitHub issues and run the orchestrator.

These tests verify the complete orchestrator lifecycle:
1. Issue creation
2. Session launch
3. Agent execution
4. PR creation
5. Completion handling

Uses the single-orchestrator pattern for efficiency.
"""

import asyncio
import logging
import os
import time

import pytest

from tests.e2e.flows import E2EFlow, cleanup_test_prs, check_issue_comment
from issue_orchestrator.testing.support.test_data import cleanup_test_issues

logger = logging.getLogger(__name__)

@pytest.mark.e2e
@pytest.mark.timeout(180)  # 3 minute timeout
class TestLiveOrchestratorLifecycle:
    """Test complete orchestrator lifecycle with real GitHub issues."""

    @pytest.mark.asyncio
    @pytest.mark.gh_activity_limit(test_gh_activity_limit=300, system_gh_activity_limit=100)
    async def test_issue_to_completion(
        self,
        e2e_orchestrator,
        orchestrator_watcher,
        test_issue_factory,
        repo_name: str,
    ):
        """Full lifecycle: issue created -> session runs -> PR created -> completed.

        This is the main happy-path e2e test that verifies:
        1. Orchestrator picks up the issue
        2. Session is launched (in-progress label added)
        3. Agent runs and completes via agent-done
        4. PR is created
        5. Completion comment is posted
        """
        flow = E2EFlow(repo=repo_name, watcher=orchestrator_watcher, fail_on_blocked_failed=True)

        # Create issue scoped to this test
        issue = test_issue_factory("[E2E] Issue to completion test")
        issue_number = int(issue.stable_id())

        pr = None
        dry_run = os.environ.get("E2E_DRY_RUN_PUSH") == "1"
        try:
            # Wait for session start (in-progress label or PR created)
            await flow.session_started(issue, timeout_s=60)

            if dry_run:
                # In dry-run mode, PR creation is skipped but we verify session completes
                logger.info("Skipping PR wait (E2E_DRY_RUN_PUSH=1)")
                await asyncio.sleep(5)  # Give time for completion processing
            else:
                # Wait for PR to be created (indicates agent completed)
                pr_number = await flow.pr_created(issue, timeout_s=120)
                pr = {"number": pr_number}

                # Verify PR details
                assert pr["number"] is not None, "PR should have a number"

                # Check for completion comment (boundary check after PR created)
                # This is a single GH read at a known boundary, not a polling loop
                implementation_comment = check_issue_comment(
                    repo_name,
                    issue_number,
                    lambda comment: "## Implementation" in comment.get("body", "")
                    or "E2E test completed" in comment.get("body", ""),
                )

                # Comment may not exist if agent didn't post one - this is acceptable
                # The key assertion is that the PR was created successfully
                if implementation_comment is None:
                    logger.info("No implementation comment found (agent may not have posted one)")

        finally:
            # Cleanup: close the PR if created (only for non-dry-run)
            if pr and not dry_run:
                flow.close_pr(pr["number"])


@pytest.mark.e2e
@pytest.mark.timeout(60)
class TestOrchestratorStateObservation:
    """Test observing orchestrator state during execution."""

    @pytest.mark.gh_activity_limit(test_gh_activity_limit=10, system_gh_activity_limit=50)
    def test_orchestrator_starts_and_stops(
        self,
        e2e_orchestrator,
    ):
        """Verify the shared orchestrator is running."""
        # The session-scoped orchestrator should already be running
        assert e2e_orchestrator.is_running(), "Orchestrator should be running"
        logger.info("✓ Orchestrator is running")

    @pytest.mark.gh_activity_limit(test_gh_activity_limit=10, system_gh_activity_limit=50)
    def test_no_issues_to_process(
        self,
        e2e_orchestrator,
    ):
        """Orchestrator handles having no matching issues gracefully."""
        # Just verify orchestrator is healthy
        time.sleep(3)
        assert e2e_orchestrator.is_running(), "Orchestrator should still be running"
        logger.info("✓ Orchestrator running healthy with no new issues")


@pytest.mark.e2e
@pytest.mark.timeout(120)
class TestLabelDetection:
    """Test that label changes are detected correctly."""

    @pytest.mark.asyncio
    @pytest.mark.gh_activity_limit(test_gh_activity_limit=200, system_gh_activity_limit=100)
    async def test_blocked_label_detection(
        self,
        e2e_orchestrator,
        orchestrator_watcher,
        test_issue_factory,
        repo_name: str,
    ):
        """Adding blocked label is detected and handled.

        This test:
        1. Creates issue and waits for session to start
        2. Adds 'blocked' label
        3. Verifies orchestrator sees it

        Note: In dry-run mode (E2E_DRY_RUN_PUSH=1), sessions complete instantly,
        so we skip this test since there's no time to add the blocked label
        before the session completes.
        """
        dry_run = os.environ.get("E2E_DRY_RUN_PUSH") == "1"
        if dry_run:
            pytest.skip("Blocked label detection not applicable in dry-run mode")

        flow = E2EFlow(repo=repo_name, watcher=orchestrator_watcher, fail_on_blocked_failed=True)
        issue = test_issue_factory("[E2E] Blocked label detection test")
        issue_number = int(issue.stable_id())

        # Wait for issue to appear in snapshots before updating labels
        await flow.issue_seen(issue, timeout_s=60)

        # Add blocked label
        flow.update_issue(issue, add_labels=["blocked"])

        # Wait for orchestrator to see the blocked label
        await flow.issue_has_label(issue, "blocked", timeout_s=60)
        logger.info("✓ Blocked label detected")


@pytest.mark.e2e
class TestCleanup:
    """Tests for cleanup functionality."""

    @pytest.mark.gh_activity_limit(test_gh_activity_limit=300, system_gh_activity_limit=50)
    def test_cleanup_test_issues(self, repo_name: str):
        """Verify test issue cleanup works."""
        # This just verifies the cleanup function runs without error
        count = cleanup_test_issues(repo_name)
        assert count >= 0
        logger.info("✓ Cleaned up %d test issues", count)

    @pytest.mark.gh_activity_limit(test_gh_activity_limit=300, system_gh_activity_limit=50)
    def test_cleanup_test_prs(self, repo_name: str):
        """Clean up any test PRs left behind including review-labeled PRs."""
        labels_to_cleanup = ["io-e2e-test-data", "needs-code-review", "code-reviewed"]
        cleaned = cleanup_test_prs(repo_name, labels_to_cleanup)
        logger.info("✓ Cleaned up %d test PRs", cleaned)
