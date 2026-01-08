"""E2E tests using the single-orchestrator pattern.

This test file demonstrates the efficient single-orchestrator approach:
- One orchestrator runs for the entire test session
- Each test cleans up its own issues (by test-specific label) before creating new ones
- Tests can create issues dynamically while orchestrator runs
- Failed test issues persist for debugging, cleaned up on next run

Run with:
    pytest tests/e2e/test_single_orchestrator.py -v

Run in parallel with another session:
    E2E_FILTER=run-a pytest tests/e2e/test_single_orchestrator.py -v
    E2E_FILTER=run-b pytest tests/e2e/test_single_orchestrator.py -v  # different terminal
"""

import os

import pytest

from tests.e2e.conftest import e2e_label
from tests.e2e.flows import E2EFlow

@pytest.mark.e2e
@pytest.mark.timeout(300)
class TestSingleOrchestratorBasic:
    """Basic tests using the single-orchestrator pattern."""

    @pytest.mark.asyncio
    @pytest.mark.gh_activity_limit(test_gh_activity_limit=100, system_gh_activity_limit=50)
    async def test_issue_gets_picked_up(
        self,
        e2e_orchestrator,
        orchestrator_watcher,
        test_issue_factory,
        repo_name,
    ):
        """Test that orchestrator picks up a newly created issue."""
        flow = E2EFlow(repo=repo_name, watcher=orchestrator_watcher)

        # Create issue scoped to this test (cleans up stale ones first)
        issue = test_issue_factory("[E2E] Basic pickup test")

        # Wait for orchestrator to pick it up
        await flow.session_started(issue, timeout_s=60)

    @pytest.mark.asyncio
    # Note: Limit is higher because orchestrator's periodic list_issues polling
    # (~6 calls during 13s test) isn't tagged as "periodic" scope, so it counts
    # against test activity. TODO: Fix scope tagging in gh_audit to properly
    # separate orchestrator polling from test-specific activity.
    @pytest.mark.gh_activity_limit(test_gh_activity_limit=30, system_gh_activity_limit=10)
    async def test_inflight_issue_creation(
        self,
        e2e_orchestrator,
        orchestrator_watcher,
        repo_name,
        filter_label,
        test_label,
    ):
        """Test creating an issue while orchestrator is running.

        This test validates:
        1. Issue creation succeeds mid-session
        2. Orchestrator observes the new issue
        3. The orchestrator remains healthy after seeing the new issue

        Note: The issue may be queued (not in-progress) if concurrency slots
        are full. That's valid - we just verify it was created and orchestrator
        didn't crash.
        """
        # Create issue dynamically (inflight)
        flow = E2EFlow(repo=repo_name, watcher=orchestrator_watcher, filter_label=filter_label)
        issue, _issue_num = flow.create_issue(
            "[M0-700] [E2E] Inflight creation test",
            ["agent:e2e-test", e2e_label(test_label)],
        )

        # Verify issue was created
        assert issue is not None, "create_issue should return an IssueKey"
        assert issue.stable_id(), "Issue should have a valid ID"

        # Give orchestrator time to process the refresh and see the issue
        await flow.issue_seen(issue, timeout_s=60)

        # Verify orchestrator is still running (didn't crash on new issue)
        assert e2e_orchestrator.is_running(), (
            f"Orchestrator should still be running after inflight issue creation. "
            f"Issue: {issue}"
        )


@pytest.mark.e2e
@pytest.mark.timeout(300)
class TestSingleOrchestratorAdvanced:
    """Advanced tests demonstrating in-flight operations."""

    @pytest.mark.asyncio
    @pytest.mark.gh_activity_limit(test_gh_activity_limit=150, system_gh_activity_limit=50)
    async def test_label_update_detected(
        self,
        e2e_orchestrator,
        orchestrator_watcher,
        test_issue_factory,
        repo_name,
    ):
        """Test that orchestrator detects label changes.

        Note: In dry-run mode (E2E_DRY_RUN_PUSH=1), sessions complete instantly,
        so there's no time window to add labels before the session completes.
        """
        dry_run = os.environ.get("E2E_DRY_RUN_PUSH") == "1"
        if dry_run:
            pytest.skip("Label detection not applicable in dry-run mode (sessions complete instantly)")

        flow = E2EFlow(repo=repo_name, watcher=orchestrator_watcher)
        # Create issue
        issue = test_issue_factory("[E2E] Label update test")
        # Wait for initial pickup
        await flow.issue_seen(issue, timeout_s=60)

        # Update labels (add blocked)
        flow.update_issue(issue, add_labels=["blocked"])

        # Verify orchestrator sees the blocked label
        await flow.issue_has_label(issue, "blocked", timeout_s=60)
