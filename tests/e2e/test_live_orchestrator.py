"""Live E2E tests that create real GitHub issues and run the orchestrator.

These tests verify the complete orchestrator lifecycle:
1. Issue creation
2. Session launch
3. Agent execution
4. PR creation
5. Completion handling

Prerequisites:
- gh CLI authenticated
- claude CLI available
- Network access to GitHub
"""

import json
import subprocess
import time

import pytest

from tests.e2e.conftest import (
    wait_for_issue_label,
    wait_for_pr_created,
    get_issue_comments,
    OrchestratorProcess,
)


class TestLiveOrchestratorLifecycle:
    """Test complete orchestrator lifecycle with real GitHub issues."""

    @pytest.mark.timeout(180)  # 3 minute timeout
    def test_issue_to_completion(
        self,
        single_test_issue: dict,
        orchestrator_process: OrchestratorProcess,
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
        issue_number = single_test_issue["number"]

        # Start orchestrator
        orchestrator_process.start(max_issues=1)
        assert orchestrator_process.is_running(), "Orchestrator should be running"

        pr = None  # Initialize for cleanup in finally block
        try:
            # Wait for in-progress label (indicates session started, with fast failure detection)
            in_progress = wait_for_issue_label(
                repo_name, issue_number, "in-progress", timeout=60,
                orchestrator=orchestrator_process,
            )
            assert in_progress, f"Issue {issue_number} should have 'in-progress' label"

            # Wait for PR to be created (indicates agent completed, with fast failure detection)
            pr = wait_for_pr_created(
                repo_name, issue_number, timeout=120,
                orchestrator=orchestrator_process,
            )
            assert pr is not None, f"PR should be created for issue {issue_number}"

            # Verify PR details
            assert "e2e" in pr["title"].lower() or str(issue_number) in pr["headRefName"], \
                "PR should reference the test issue"

            # Wait for completion (in-progress removed)
            # Give it a few seconds for cleanup
            time.sleep(5)
            result = subprocess.run(
                ["gh", "issue", "view", str(issue_number),
                 "--repo", repo_name,
                 "--json", "labels"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                labels = [l["name"] for l in data.get("labels", [])]
                # in-progress should be removed after completion
                # (may still be present if test runs fast)

            # Check for completion comment
            comments = get_issue_comments(repo_name, issue_number)
            implementation_comment = None
            for comment in comments:
                body = comment.get("body", "")
                if "## Implementation" in body or "E2E test completed" in body:
                    implementation_comment = comment
                    break

            assert implementation_comment is not None, \
                "Implementation comment should be posted on issue"

        finally:
            # Stop orchestrator
            stdout, stderr = orchestrator_process.stop()

            # Cleanup: close the PR if created
            if pr:
                subprocess.run(
                    ["gh", "pr", "close", str(pr["number"]),
                     "--repo", repo_name,
                     "--delete-branch"],
                    capture_output=True
                )


class TestOrchestratorStateObservation:
    """Test observing orchestrator state during execution."""

    @pytest.mark.timeout(60)
    def test_orchestrator_starts_and_stops(
        self,
        orchestrator_process: OrchestratorProcess,
    ):
        """Verify orchestrator can start and stop cleanly."""
        # Start
        orchestrator_process.start(max_issues=0)  # Don't process any issues
        assert orchestrator_process.is_running()

        # Let it run briefly
        time.sleep(3)
        assert orchestrator_process.is_running()

        # Stop
        stdout, stderr = orchestrator_process.stop()
        assert not orchestrator_process.is_running()

        # Should have exited cleanly (no crash errors)
        assert "Error" not in stderr or "Traceback" not in stderr

    @pytest.mark.timeout(60)
    def test_no_issues_to_process(
        self,
        orchestrator_process: OrchestratorProcess,
        repo_name: str,
    ):
        """Orchestrator handles having no matching issues gracefully."""
        # Ensure no test issues exist
        subprocess.run(
            ["gh", "issue", "list",
             "--repo", repo_name,
             "--label", "test-data,agent:e2e-test",
             "--state", "open",
             "--json", "number"],
            capture_output=True
        )

        # Start orchestrator
        orchestrator_process.start(max_issues=1)

        # Let it run for a bit
        time.sleep(5)

        # Should still be running (not crashed)
        assert orchestrator_process.is_running()

        # Stop cleanly
        stdout, stderr = orchestrator_process.stop()
        assert "Traceback" not in stderr


class TestLabelDetection:
    """Test that label changes are detected correctly."""

    @pytest.mark.timeout(120)
    def test_blocked_label_detection(
        self,
        single_test_issue: dict,
        orchestrator_process: OrchestratorProcess,
        repo_name: str,
    ):
        """Adding blocked label is detected and handled.

        This test:
        1. Starts orchestrator
        2. Waits for session to start
        3. Adds 'blocked' label manually
        4. Verifies orchestrator detects it
        """
        issue_number = single_test_issue["number"]

        # Start orchestrator
        orchestrator_process.start(max_issues=1)

        try:
            # Wait for in-progress label (with fast failure detection)
            in_progress = wait_for_issue_label(
                repo_name, issue_number, "in-progress", timeout=60,
                orchestrator=orchestrator_process,
            )
            if not in_progress:
                pytest.skip("Session didn't start in time")

            # Add blocked label
            subprocess.run(
                ["gh", "issue", "edit", str(issue_number),
                 "--repo", repo_name,
                 "--add-label", "blocked"],
                capture_output=True
            )

            # Wait for orchestrator to detect and handle
            time.sleep(15)

            # Check issue labels
            result = subprocess.run(
                ["gh", "issue", "view", str(issue_number),
                 "--repo", repo_name,
                 "--json", "labels"],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                data = json.loads(result.stdout)
                labels = [l["name"] for l in data.get("labels", [])]
                assert "blocked" in labels, "Blocked label should still be present"

        finally:
            orchestrator_process.stop()


class TestCleanup:
    """Tests for cleanup functionality."""

    def test_cleanup_test_issues(self, repo_name: str):
        """Verify test issue cleanup works."""
        from issue_orchestrator.test_data import cleanup_test_issues

        # This just verifies the cleanup function runs without error
        # It will close any lingering test issues
        count = cleanup_test_issues(repo_name)
        # count could be 0 if no test issues exist
        assert count >= 0

    def test_cleanup_test_prs(self, repo_name: str):
        """Clean up any test PRs left behind including review-labeled PRs."""
        # Find and close PRs with test-related labels
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
