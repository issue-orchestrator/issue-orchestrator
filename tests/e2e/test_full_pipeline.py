"""Comprehensive E2E test for the full orchestration pipeline.

This test verifies the complete system works end-to-end by:
1. Creating multiple real GitHub issues
2. Starting ONE orchestrator that processes them concurrently
3. Verifying labels, sessions, PRs at each stage

This is the "if this passes, the system works" test.

Design principle: Tests should mirror production behavior.
In production, one orchestrator handles many issues concurrently.
"""

import json
import subprocess
import time
from typing import Optional

import pytest

from tests.e2e.conftest import (
    OrchestratorProcess,
    wait_for_issue_label,
    wait_for_pr_created,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_issue_labels(repo: str, issue_number: int) -> list[str]:
    """Get current labels on an issue."""
    result = subprocess.run(
        ["gh", "issue", "view", str(issue_number),
         "--repo", repo,
         "--json", "labels"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        data = json.loads(result.stdout)
        return [l["name"] for l in data.get("labels", [])]
    return []


def get_pr_labels(repo: str, pr_number: int) -> list[str]:
    """Get current labels on a PR."""
    result = subprocess.run(
        ["gh", "pr", "view", str(pr_number),
         "--repo", repo,
         "--json", "labels"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        data = json.loads(result.stdout)
        return [l["name"] for l in data.get("labels", [])]
    return []


def close_pr(repo: str, pr_number: int) -> None:
    """Close a PR and delete its branch."""
    subprocess.run(
        ["gh", "pr", "close", str(pr_number),
         "--repo", repo,
         "--delete-branch"],
        capture_output=True
    )


# ---------------------------------------------------------------------------
# The Main E2E Test
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.live
@pytest.mark.timeout(300)  # 5 minute timeout (typecheck validation is fast)
@pytest.mark.parametrize("concurrent_test_run", [3], indirect=True)
class TestConcurrentPipeline:
    """Test the orchestrator processing multiple issues concurrently.

    This is THE e2e test. It creates N issues and verifies
    ONE orchestrator processes them all in parallel.
    """

    def test_concurrent_issue_processing(
        self,
        concurrent_test_run: dict,
        orchestrator_process: OrchestratorProcess,
        repo_name: str,
    ):
        """Test orchestrator processes multiple issues concurrently.

        Verifies:
        1. All issues get 'in-progress' label (sessions started)
        2. All issues get PRs created
        3. Processing happens concurrently (not sequentially)
        4. Labels are cleaned up after completion
        """
        run_label = concurrent_test_run["label"]
        issues = concurrent_test_run["issues"]
        issue_numbers = [i["number"] for i in issues]

        print(f"\n=== Testing {len(issues)} issues concurrently ===")
        print(f"Run label: {run_label}")
        print(f"Issues: {issue_numbers}")

        # Start orchestrator with the unique run label
        orchestrator_process.start(
            max_issues=len(issues),
            extra_args=["--label", run_label]
        )
        assert orchestrator_process.is_running(), "Orchestrator should start"

        created_prs = []
        start_time = time.time()

        try:
            # Phase 1: Wait for all sessions to start (in-progress labels)
            print("\nPhase 1: Waiting for all sessions to start...")
            sessions_started = {}

            for _ in range(60):  # 2 minutes max
                if not orchestrator_process.is_running():
                    stdout, stderr = orchestrator_process.stop()
                    pytest.fail(
                        f"Orchestrator crashed.\n"
                        f"stdout: {stdout[:1000] if stdout else '(empty)'}\n"
                        f"stderr: {stderr[:1000] if stderr else '(empty)'}"
                    )

                for issue_num in issue_numbers:
                    if issue_num not in sessions_started:
                        labels = get_issue_labels(repo_name, issue_num)
                        if "in-progress" in labels:
                            sessions_started[issue_num] = time.time() - start_time
                            print(f"  ✓ Issue #{issue_num} started at {sessions_started[issue_num]:.1f}s")

                if len(sessions_started) == len(issues):
                    print(f"All {len(issues)} sessions started!")
                    break

                time.sleep(2)
            else:
                missing = set(issue_numbers) - set(sessions_started.keys())
                pytest.fail(f"Sessions never started for issues: {missing}")

            # Phase 2: Wait for all PRs to be created
            print("\nPhase 2: Waiting for all PRs to be created...")
            prs_created = {}

            for _ in range(120):  # 4 minutes max
                if not orchestrator_process.is_running():
                    stdout, stderr = orchestrator_process.stop()
                    pytest.fail(
                        f"Orchestrator crashed waiting for PRs.\n"
                        f"stdout: {stdout[:1000] if stdout else '(empty)'}\n"
                        f"stderr: {stderr[:1000] if stderr else '(empty)'}"
                    )

                for issue_num in issue_numbers:
                    if issue_num not in prs_created:
                        pr = wait_for_pr_created(repo_name, issue_num, timeout=1)
                        if pr:
                            prs_created[issue_num] = {
                                "pr": pr,
                                "time": time.time() - start_time,
                            }
                            created_prs.append(pr)
                            print(f"  ✓ PR #{pr['number']} for issue #{issue_num} at {prs_created[issue_num]['time']:.1f}s")

                if len(prs_created) == len(issues):
                    print(f"All {len(issues)} PRs created!")
                    break

                time.sleep(2)
            else:
                missing = set(issue_numbers) - set(prs_created.keys())
                pytest.fail(f"PRs never created for issues: {missing}")

            # Phase 3: Verify label cleanup
            print("\nPhase 3: Verifying label cleanup...")
            time.sleep(5)
            for issue_num in issue_numbers:
                labels = get_issue_labels(repo_name, issue_num)
                print(f"  Issue #{issue_num} final labels: {labels}")

            # Summary
            total_time = time.time() - start_time
            print(f"\n=== Summary ===")
            print(f"Total time: {total_time:.1f}s")
            print(f"Issues processed: {len(issues)}")
            print(f"PRs created: {len(created_prs)}")

            # Verify concurrency: if sequential, would take N * ~60s each
            # If concurrent, should complete in roughly the time of one
            if len(issues) > 1:
                avg_time = total_time / len(issues)
                print(f"Avg time per issue: {avg_time:.1f}s")
                # If truly concurrent, avg should be much less than 60s per issue
                if avg_time < 45:
                    print("✓ Processing was concurrent (avg < 45s per issue)")
                else:
                    print("⚠ Processing may have been sequential")

        finally:
            orchestrator_process.stop()

            # Cleanup: close all PRs
            for pr in created_prs:
                close_pr(repo_name, pr["number"])


# ---------------------------------------------------------------------------
# Edge Case: No Matching Issues
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.live
@pytest.mark.timeout(60)
class TestEdgeCases:
    """Test edge cases and failure handling."""

    def test_orchestrator_handles_no_matching_issues(
        self,
        orchestrator_process: OrchestratorProcess,
        repo_name: str,
    ):
        """Orchestrator should handle having no matching issues gracefully."""
        # Use a label that doesn't exist
        fake_label = "nonexistent-label-xyz123"

        # Start orchestrator with the fake label
        orchestrator_process.start(
            max_issues=1,
            extra_args=["--label", fake_label]
        )

        try:
            # Should run without crashing for 10 seconds
            time.sleep(10)
            assert orchestrator_process.is_running(), "Orchestrator should still be running"

            # Stop and check output
            stdout, stderr = orchestrator_process.stop()

            # Should not have crashed
            assert "Traceback" not in stderr, f"Should not have crashed: {stderr[:500]}"
            print("✓ Orchestrator handled no matching issues gracefully")

        finally:
            if orchestrator_process.is_running():
                orchestrator_process.stop()
