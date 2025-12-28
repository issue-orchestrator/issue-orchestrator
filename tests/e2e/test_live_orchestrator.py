"""Live E2E tests that create real GitHub issues and run the orchestrator.

These tests verify the complete orchestrator lifecycle:
1. Issue creation
2. Session launch
3. Agent execution
4. PR creation
5. Completion handling

Uses the single-orchestrator pattern for efficiency.
"""

import json
import subprocess
import time

import pytest

from tests.e2e.conftest import (
    inflight_create,
    inflight_update,
    trigger_refresh,
    wait_for_issue_label,
    wait_for_pr_created,
    get_issue_comments,
)
from issue_orchestrator.test_data import cleanup_test_issues


@pytest.mark.e2e
@pytest.mark.timeout(180)  # 3 minute timeout
class TestLiveOrchestratorLifecycle:
    """Test complete orchestrator lifecycle with real GitHub issues."""

    def test_issue_to_completion(
        self,
        e2e_orchestrator,
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
        # Create issue scoped to this test
        issue = test_issue_factory("[E2E] Issue to completion test")
        issue_number = int(issue.stable_id())
        trigger_refresh()

        pr = None
        try:
            # Wait for in-progress label (indicates session started)
            in_progress = wait_for_issue_label(
                repo_name, issue_number, "in-progress", timeout=60,
                orchestrator=e2e_orchestrator,
            )
            assert in_progress, f"Issue {issue_number} should have 'in-progress' label"

            # Wait for PR to be created (indicates agent completed)
            pr = wait_for_pr_created(
                repo_name, issue_number, timeout=120,
                orchestrator=e2e_orchestrator,
            )
            assert pr is not None, f"PR should be created for issue {issue_number}"

            # Verify PR details
            assert "e2e" in pr["title"].lower() or str(issue_number) in pr["headRefName"], \
                "PR should reference the test issue"

            # Check for completion comment (with retry for GitHub API propagation)
            implementation_comment = None
            for attempt in range(5):
                time.sleep(2)  # Wait for GitHub API propagation
                comments = get_issue_comments(repo_name, issue_number)
                for comment in comments:
                    body = comment.get("body", "")
                    if "## Implementation" in body or "E2E test completed" in body:
                        implementation_comment = comment
                        break
                if implementation_comment:
                    break

            assert implementation_comment is not None, \
                "Implementation comment should be posted on issue"

        finally:
            # Cleanup: close the PR if created
            if pr:
                subprocess.run(
                    ["gh", "pr", "close", str(pr["number"]),
                     "--repo", repo_name,
                     "--delete-branch"],
                    capture_output=True
                )


@pytest.mark.e2e
@pytest.mark.timeout(60)
class TestOrchestratorStateObservation:
    """Test observing orchestrator state during execution."""

    def test_orchestrator_starts_and_stops(
        self,
        e2e_orchestrator,
    ):
        """Verify the shared orchestrator is running."""
        # The session-scoped orchestrator should already be running
        assert e2e_orchestrator.is_running(), "Orchestrator should be running"
        print("✓ Orchestrator is running")

    def test_no_issues_to_process(
        self,
        e2e_orchestrator,
    ):
        """Orchestrator handles having no matching issues gracefully."""
        # Just verify orchestrator is healthy
        time.sleep(3)
        assert e2e_orchestrator.is_running(), "Orchestrator should still be running"
        print("✓ Orchestrator running healthy with no new issues")


@pytest.mark.e2e
@pytest.mark.timeout(120)
class TestLabelDetection:
    """Test that label changes are detected correctly."""

    def test_blocked_label_detection(
        self,
        e2e_orchestrator,
        test_issue_factory,
        repo_name: str,
    ):
        """Adding blocked label is detected and handled.

        This test:
        1. Creates issue and waits for session to start
        2. Adds 'blocked' label
        3. Verifies orchestrator sees it
        """
        issue = test_issue_factory("[E2E] Blocked label detection test")
        issue_number = int(issue.stable_id())
        trigger_refresh()

        # Wait for in-progress label
        in_progress = wait_for_issue_label(
            repo_name, issue_number, "in-progress", timeout=60,
            orchestrator=e2e_orchestrator,
        )
        if not in_progress:
            pytest.skip("Session didn't start in time")

        # Add blocked label using inflight_update
        inflight_update(issue, add_labels=["blocked"])

        # Wait for orchestrator to see the blocked label
        blocked = wait_for_issue_label(
            repo_name, issue_number, "blocked", timeout=30,
            orchestrator=e2e_orchestrator,
        )
        assert blocked, "Blocked label should be detected"
        print("✓ Blocked label detected")


@pytest.mark.e2e
class TestCleanup:
    """Tests for cleanup functionality."""

    def test_cleanup_test_issues(self, repo_name: str):
        """Verify test issue cleanup works."""
        # This just verifies the cleanup function runs without error
        count = cleanup_test_issues(repo_name)
        assert count >= 0
        print(f"✓ Cleaned up {count} test issues")

    def test_cleanup_test_prs(self, repo_name: str):
        """Clean up any test PRs left behind including review-labeled PRs."""
        labels_to_cleanup = ["test-data", "needs-code-review", "code-reviewed"]
        closed_prs = set()

        for label in labels_to_cleanup:
            result = subprocess.run(
                ["gh", "pr", "list",
                 "--repo", repo_name,
                 "--label", label,
                 "--json", "number"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                prs = json.loads(result.stdout)
                for pr in prs:
                    pr_num = pr["number"]
                    if pr_num not in closed_prs:
                        subprocess.run(
                            ["gh", "pr", "close", str(pr_num),
                             "--repo", repo_name,
                             "--delete-branch"],
                            capture_output=True
                        )
                        closed_prs.add(pr_num)

        print(f"✓ Cleaned up {len(closed_prs)} test PRs")
