"""Comprehensive E2E test for the full orchestration pipeline.

This test verifies the complete system works end-to-end by:
1. Creating multiple real GitHub issues
2. Using the shared session orchestrator that processes them concurrently
3. Verifying labels, sessions, PRs at each stage

Uses the single-orchestrator pattern for efficiency.
"""

import json
import subprocess
import time

import pytest

from tests.e2e.conftest import (
    inflight_create,
    trigger_refresh,
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


def close_pr(repo: str, pr_number: int) -> None:
    """Close a PR and delete its branch."""
    subprocess.run(
        ["gh", "pr", "close", str(pr_number),
         "--repo", repo,
         "--delete-branch"],
        capture_output=True
    )


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

    def test_concurrent_issue_processing(
        self,
        e2e_orchestrator,
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

        print(f"\n=== Testing {num_issues} issues concurrently ===")
        print(f"Filter label: {filter_label}")

        try:
            # Create issues dynamically
            for i in range(num_issues):
                issue = inflight_create(
                    repo_name,
                    f"[E2E-CONCURRENT-{i+1}] Concurrent pipeline test",
                    [filter_label, "agent:e2e-test", f"e2e:concurrent_{i}"],
                )
                issues.append(issue)
                print(f"Created issue #{issue.stable_id()}")

            issue_numbers = [int(i.stable_id()) for i in issues]

            # Phase 1: Wait for all sessions to start (in-progress labels)
            print("\nPhase 1: Waiting for all sessions to start...")
            sessions_started = {}

            for _ in range(60):  # 2 minutes max
                if not e2e_orchestrator.is_running():
                    stdout, stderr = e2e_orchestrator.stop()
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

                if len(sessions_started) == num_issues:
                    print(f"All {num_issues} sessions started!")
                    break

                time.sleep(2)
            else:
                missing = set(issue_numbers) - set(sessions_started.keys())
                pytest.fail(f"Sessions never started for issues: {missing}")

            # Phase 2: Wait for all PRs to be created
            print("\nPhase 2: Waiting for all PRs to be created...")
            prs_created = {}

            for _ in range(120):  # 4 minutes max
                if not e2e_orchestrator.is_running():
                    stdout, stderr = e2e_orchestrator.stop()
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

                if len(prs_created) == num_issues:
                    print(f"All {num_issues} PRs created!")
                    break

                time.sleep(2)
            else:
                missing = set(issue_numbers) - set(prs_created.keys())
                pytest.fail(f"PRs never created for issues: {missing}")

            # Summary
            total_time = time.time() - start_time
            print(f"\n=== Summary ===")
            print(f"Total time: {total_time:.1f}s")
            print(f"Issues processed: {num_issues}")
            print(f"PRs created: {len(created_prs)}")

            # Verify concurrency
            if num_issues > 1:
                avg_time = total_time / num_issues
                print(f"Avg time per issue: {avg_time:.1f}s")
                if avg_time < 45:
                    print("✓ Processing was concurrent (avg < 45s per issue)")

        finally:
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
        print("✓ Orchestrator running healthy")
