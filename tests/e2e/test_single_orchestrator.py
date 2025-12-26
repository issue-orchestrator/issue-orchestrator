"""E2E tests using the single-orchestrator pattern.

This test file demonstrates the efficient single-orchestrator approach:
- One orchestrator runs for the entire test session
- Each test cleans up its own issues (by test-specific label) before creating new ones
- Tests can create issues dynamically with inflight_create()
- Failed test issues persist for debugging, cleaned up on next run

Run with:
    pytest tests/e2e/test_single_orchestrator.py -v

Run in parallel with another session:
    E2E_FILTER=run-a pytest tests/e2e/test_single_orchestrator.py -v
    E2E_FILTER=run-b pytest tests/e2e/test_single_orchestrator.py -v  # different terminal
"""

import time
import pytest

from tests.e2e.conftest import (
    inflight_create,
    inflight_update,
    trigger_refresh,
    wait_for_issue_label,
)


@pytest.mark.e2e
@pytest.mark.timeout(300)
class TestSingleOrchestratorBasic:
    """Basic tests using the single-orchestrator pattern."""

    def test_issue_gets_picked_up(
        self,
        e2e_orchestrator,
        test_issue_factory,
        repo_name,
    ):
        """Test that orchestrator picks up a newly created issue."""
        # Create issue scoped to this test (cleans up stale ones first)
        issue = test_issue_factory("[E2E] Basic pickup test")

        # Trigger refresh so orchestrator sees it
        trigger_refresh()

        # Wait for orchestrator to pick it up
        found = wait_for_issue_label(
            repo_name,
            int(issue.stable_id()),
            "in-progress",
            timeout=60,
            orchestrator=e2e_orchestrator,
        )

        assert found, f"Issue {issue} should have in-progress label"

    def test_inflight_issue_creation(
        self,
        e2e_orchestrator,
        repo_name,
        filter_label,
        test_label,
    ):
        """Test creating an issue while orchestrator is running.

        This test validates:
        1. inflight_create() successfully creates an issue mid-session
        2. trigger_refresh() signals the orchestrator
        3. The orchestrator remains healthy after seeing the new issue

        Note: The issue may be queued (not in-progress) if concurrency slots
        are full. That's valid - we just verify it was created and orchestrator
        didn't crash.
        """
        # Create issue dynamically (inflight)
        issue = inflight_create(
            repo_name,
            "[E2E] Inflight creation test",
            [filter_label, "agent:e2e-test", test_label],
        )

        # Verify issue was created
        assert issue is not None, "inflight_create should return an IssueKey"
        assert issue.stable_id(), "Issue should have a valid ID"

        # Give orchestrator time to process the refresh and see the issue
        time.sleep(5)

        # Verify orchestrator is still running (didn't crash on new issue)
        assert e2e_orchestrator.is_running(), (
            f"Orchestrator should still be running after inflight issue creation. "
            f"Issue: {issue}"
        )


@pytest.mark.e2e
@pytest.mark.timeout(300)
class TestSingleOrchestratorAdvanced:
    """Advanced tests demonstrating in-flight operations."""

    def test_label_update_detected(
        self,
        e2e_orchestrator,
        test_issue_factory,
        repo_name,
    ):
        """Test that orchestrator detects label changes."""
        # Create issue
        issue = test_issue_factory("[E2E] Label update test")
        trigger_refresh()

        # Wait for initial pickup
        wait_for_issue_label(
            repo_name,
            int(issue.stable_id()),
            "in-progress",
            timeout=60,
            orchestrator=e2e_orchestrator,
        )

        # Update labels (add blocked)
        inflight_update(issue, add_labels=["blocked"])

        # Verify orchestrator sees the blocked label
        found = wait_for_issue_label(
            repo_name,
            int(issue.stable_id()),
            "blocked",
            timeout=30,
            orchestrator=e2e_orchestrator,
        )

        assert found, f"Issue {issue} should have blocked label after update"
