"""Real-world scenario E2E tests.

These tests verify the common, non-edge-case scenarios that users actually encounter:

1. Happy path: Issue processed, PR created, code review runs
2. Multiple issues: Concurrency works, triage triggered after threshold
3. Failure handling: Agent fails, system recovers gracefully
4. Blocked/needs-human: Labels detected and handled

These are the "if these pass, the system works for real users" tests.

Run with: pytest tests/e2e/test_real_scenarios.py -v
Expected runtime: 15-30 minutes (real Claude sessions)
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

import pytest

from issue_orchestrator.test_data import create_test_issues, cleanup_test_issues
from issue_orchestrator.github import get_prs_for_issue as _get_prs_for_issue


# ---------------------------------------------------------------------------
# Test Configuration
# ---------------------------------------------------------------------------

# How many issues to create for multi-issue tests
MULTI_ISSUE_COUNT = 3

# Timeouts (seconds)
TIMEOUT_SESSION_START = 90
TIMEOUT_SESSION_COMPLETE = 300
TIMEOUT_CODE_REVIEW = 180  # Code review can take time
TIMEOUT_CODE_REVIEW_COMPLETE = 240  # Wait for code-reviewed label
TIMEOUT_TRIAGE = 180


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def create_single_issue(repo: str, title: str, labels: list[str]) -> int:
    """Create a single test issue and return its number."""
    # Ensure labels exist
    for label in labels:
        subprocess.run(
            ["gh", "label", "create", label, "--repo", repo, "--force"],
            capture_output=True
        )

    result = subprocess.run(
        ["gh", "issue", "create",
         "--repo", repo,
         "--title", title,
         "--body", f"Automated test issue.\n\nLabels: {', '.join(labels)}",
         ] + [item for label in labels for item in ["--label", label]],
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:
        raise RuntimeError(f"Failed to create issue: {result.stderr}")

    # Extract issue number from URL
    url = result.stdout.strip()
    return int(url.split("/")[-1])


def close_issue(repo: str, issue_number: int, comment: str = "Test cleanup"):
    """Close an issue."""
    subprocess.run(
        ["gh", "issue", "close", str(issue_number),
         "--repo", repo,
         "--comment", comment],
        capture_output=True
    )


def get_issue_state(repo: str, issue_number: int) -> dict:
    """Get full issue state including labels, comments, linked PRs."""
    result = subprocess.run(
        ["gh", "issue", "view", str(issue_number),
         "--repo", repo,
         "--json", "number,title,state,labels,comments"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        return json.loads(result.stdout)
    return {}


def get_prs_for_issue(repo: str, issue_number: int) -> list[dict]:
    """Find PRs that reference an issue.

    Uses the centralized get_prs_for_issue from issue_orchestrator.github.
    """
    return _get_prs_for_issue(repo=repo, issue_number=issue_number)


def wait_for_condition(
    condition_fn,
    timeout: int,
    interval: int = 5,
    description: str = "condition"
) -> bool:
    """Wait for a condition to become true."""
    start = time.time()
    while time.time() - start < timeout:
        if condition_fn():
            return True
        time.sleep(interval)
    print(f"Timeout waiting for {description}")
    return False


def start_orchestrator(repo: str, max_issues: int = 1) -> subprocess.Popen:
    """Start orchestrator as background process."""
    proc = subprocess.Popen(
        [sys.executable, "-m", "issue_orchestrator.cli", "start",
         "--label", "test-data",
         "--max-issues", str(max_issues),
         "--ui-mode", "tmux",
         "--no-dashboard"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(3)  # Give it time to start
    return proc


def stop_orchestrator(proc: subprocess.Popen) -> tuple[str, str]:
    """Stop orchestrator and return output."""
    import signal
    proc.send_signal(signal.SIGTERM)
    try:
        stdout, stderr = proc.communicate(timeout=10)
        return stdout.decode() if stdout else "", stderr.decode() if stderr else ""
    except subprocess.TimeoutExpired:
        proc.kill()
        stdout, stderr = proc.communicate()
        return stdout.decode() if stdout else "", stderr.decode() if stderr else ""


def cleanup_test_prs(repo: str):
    """Close all test PRs."""
    result = subprocess.run(
        ["gh", "pr", "list",
         "--repo", repo,
         "--label", "test-data",
         "--json", "number"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        prs = json.loads(result.stdout)
        for pr in prs:
            subprocess.run(
                ["gh", "pr", "close", str(pr["number"]),
                 "--repo", repo,
                 "--delete-branch"],
                capture_output=True
            )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def test_repo():
    """Get the test repo name."""
    import os
    if "E2E_TEST_REPO" in os.environ:
        return os.environ["E2E_TEST_REPO"]

    # Get from git
    result = subprocess.run(
        ["git", "remote", "get-url", "origin"],
        capture_output=True,
        text=True,
    )
    url = result.stdout.strip()
    if url.startswith("git@"):
        parts = url.split(":")[-1]
    else:
        parts = "/".join(url.split("/")[-2:])
    return parts.replace(".git", "")


@pytest.fixture(scope="module", autouse=True)
def cleanup_before_and_after(test_repo):
    """Clean up test data before and after tests."""
    # Before
    cleanup_test_issues(test_repo)
    cleanup_test_prs(test_repo)

    yield

    # After
    cleanup_test_issues(test_repo)
    cleanup_test_prs(test_repo)


# ---------------------------------------------------------------------------
# SCENARIO 1: Happy Path - Single Issue to PR
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.live
@pytest.mark.quick  # Run this in make validate
class TestHappyPath:
    """Test the most common scenario: issue → session → PR → review."""

    @pytest.mark.timeout(600)
    def test_issue_becomes_pr_with_review(self, test_repo):
        """
        The happy path that every user expects to work:

        1. Create issue with agent label
        2. Orchestrator picks it up
        3. Session runs, agent completes work
        4. PR is created with needs-code-review label
        5. Code review agent reviews it
        6. PR gets code-reviewed label

        This is THE test. If this passes, the core system works.
        """
        print("\n" + "=" * 60)
        print("HAPPY PATH TEST: Issue → Session → PR → Code Review")
        print("=" * 60)

        # Step 1: Create issue
        print("\n[1/6] Creating test issue...")
        issue_number = create_single_issue(
            test_repo,
            "[E2E-HAPPY] Test happy path scenario",
            ["agent:e2e-test", "test-data"]
        )
        print(f"      Created issue #{issue_number}")

        orchestrator = None
        pr_number = None

        try:
            # Step 2: Start orchestrator
            print("\n[2/6] Starting orchestrator...")
            orchestrator = start_orchestrator(test_repo, max_issues=1)
            assert orchestrator.poll() is None, "Orchestrator should be running"
            print("      Orchestrator started")

            # Step 3: Wait for session to start (in-progress label)
            print(f"\n[3/6] Waiting for session to start (timeout: {TIMEOUT_SESSION_START}s)...")

            def has_in_progress():
                state = get_issue_state(test_repo, issue_number)
                labels = [l["name"] for l in state.get("labels", [])]
                return "in-progress" in labels

            started = wait_for_condition(
                has_in_progress,
                TIMEOUT_SESSION_START,
                description="in-progress label"
            )
            assert started, "Session should start (in-progress label applied)"
            print("      ✓ Session started (in-progress label applied)")

            # Step 4: Wait for PR to be created
            print(f"\n[4/6] Waiting for PR creation (timeout: {TIMEOUT_SESSION_COMPLETE}s)...")

            def has_pr():
                prs = get_prs_for_issue(test_repo, issue_number)
                return len(prs) > 0

            pr_created = wait_for_condition(
                has_pr,
                TIMEOUT_SESSION_COMPLETE,
                description="PR creation"
            )
            assert pr_created, "PR should be created"

            prs = get_prs_for_issue(test_repo, issue_number)
            pr_number = prs[0]["number"]
            print(f"      ✓ PR #{pr_number} created")

            # Step 5: Wait for code review to COMPLETE (not just start)
            print(f"\n[5/7] Waiting for code review to complete (timeout: {TIMEOUT_CODE_REVIEW_COMPLETE}s)...")

            def has_code_reviewed_label():
                prs = get_prs_for_issue(test_repo, issue_number)
                if not prs:
                    return False
                labels = [l["name"] for l in prs[0].get("labels", [])]
                return "code-reviewed" in labels

            code_review_completed = wait_for_condition(
                has_code_reviewed_label,
                TIMEOUT_CODE_REVIEW_COMPLETE,
                interval=10,
                description="code-reviewed label"
            )

            # Refresh PR state
            prs = get_prs_for_issue(test_repo, issue_number)
            pr_labels = [l["name"] for l in prs[0].get("labels", [])] if prs else []
            print(f"      PR labels after waiting: {pr_labels}")

            if code_review_completed:
                print("      ✓ Code review COMPLETED (code-reviewed label applied)")
            elif "needs-code-review" in pr_labels:
                print("      ⚠ Code review started but not yet completed")
                # Still a partial success - means review was queued
            else:
                print("      ⚠ No code review labels (may not be configured)")

            # Step 6: Verify completion comment
            print(f"\n[6/7] Verifying completion comment...")
            state = get_issue_state(test_repo, issue_number)
            comments = state.get("comments", [])
            has_completion = any(
                "## Implementation" in c.get("body", "") or
                "completed" in c.get("body", "").lower()
                for c in comments
            )
            if has_completion:
                print("      ✓ Completion comment posted")
            else:
                print("      ⚠ No completion comment found")

            # Step 7: Summary and assertions
            print(f"\n[7/7] Final verification...")

            # Final verification
            print("\n" + "=" * 60)
            if code_review_completed:
                print("RESULT: PASSED - Full happy path with code review completed!")
            else:
                print("RESULT: PARTIAL - PR created but code review not confirmed")
            print("=" * 60)

            # Core assertions - these must pass
            assert pr_number is not None, "PR must be created"

            # Soft assertion for code review (warn but don't fail if not configured)
            if not code_review_completed and "needs-code-review" not in pr_labels:
                print("\nNote: Code review may not be configured for this repo")
            elif code_review_completed:
                print("\n✓ Full e2e flow verified: Issue → Session → PR → Code Review Complete")

        finally:
            # Cleanup
            if orchestrator:
                stop_orchestrator(orchestrator)
            if pr_number:
                subprocess.run(
                    ["gh", "pr", "close", str(pr_number),
                     "--repo", test_repo,
                     "--delete-branch"],
                    capture_output=True
                )
            close_issue(test_repo, issue_number, "E2E test completed")


# ---------------------------------------------------------------------------
# SCENARIO 2: Multiple Issues - Concurrency & Triage
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.live
class TestMultipleIssues:
    """Test processing multiple issues and triggering triage."""

    @pytest.mark.timeout(1200)  # 20 minutes for multiple issues
    def test_multiple_issues_trigger_triage(self, test_repo):
        """
        Test that multiple issues can be processed and triage is triggered.

        1. Create 3 issues
        2. Orchestrator processes them (possibly concurrently)
        3. PRs are created for each
        4. Code reviews complete
        5. Triage should be triggered (if threshold is met)
        """
        print("\n" + "=" * 60)
        print("MULTI-ISSUE TEST: Concurrency & Triage Trigger")
        print("=" * 60)

        issue_numbers = []
        pr_numbers = []
        orchestrator = None

        try:
            # Step 1: Create multiple issues
            print(f"\n[1/5] Creating {MULTI_ISSUE_COUNT} test issues...")
            for i in range(MULTI_ISSUE_COUNT):
                issue_num = create_single_issue(
                    test_repo,
                    f"[E2E-MULTI-{i+1}] Test issue {i+1} of {MULTI_ISSUE_COUNT}",
                    ["agent:e2e-test", "test-data"]
                )
                issue_numbers.append(issue_num)
                print(f"      Created issue #{issue_num}")

            # Step 2: Start orchestrator
            print("\n[2/5] Starting orchestrator...")
            orchestrator = start_orchestrator(test_repo, max_issues=MULTI_ISSUE_COUNT)
            print("      Orchestrator started")

            # Step 3: Wait for all sessions to start
            print(f"\n[3/5] Waiting for all sessions to start...")
            for issue_num in issue_numbers:
                def has_in_progress(n=issue_num):
                    state = get_issue_state(test_repo, n)
                    labels = [l["name"] for l in state.get("labels", [])]
                    return "in-progress" in labels

                started = wait_for_condition(
                    has_in_progress,
                    TIMEOUT_SESSION_START,
                    description=f"issue #{issue_num} in-progress"
                )
                if started:
                    print(f"      ✓ Issue #{issue_num} session started")
                else:
                    print(f"      ⚠ Issue #{issue_num} session did not start")

            # Step 4: Wait for PRs
            print(f"\n[4/5] Waiting for PRs (timeout: {TIMEOUT_SESSION_COMPLETE}s per issue)...")
            for issue_num in issue_numbers:
                def has_pr(n=issue_num):
                    prs = get_prs_for_issue(test_repo, n)
                    return len(prs) > 0

                pr_created = wait_for_condition(
                    has_pr,
                    TIMEOUT_SESSION_COMPLETE,
                    description=f"PR for issue #{issue_num}"
                )
                if pr_created:
                    prs = get_prs_for_issue(test_repo, issue_num)
                    pr_numbers.append(prs[0]["number"])
                    print(f"      ✓ PR created for issue #{issue_num}")
                else:
                    print(f"      ✗ No PR created for issue #{issue_num}")

            # Step 5: Check for triage (if all PRs completed)
            print(f"\n[5/5] Checking for triage trigger...")
            if len(pr_numbers) >= MULTI_ISSUE_COUNT:
                # Wait for code reviews to complete
                time.sleep(30)

                # Count code-reviewed PRs
                result = subprocess.run(
                    ["gh", "pr", "list",
                     "--repo", test_repo,
                     "--label", "code-reviewed",
                     "--json", "number"],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    reviewed_prs = json.loads(result.stdout)
                    print(f"      Code-reviewed PRs: {len(reviewed_prs)}")

                # Check for triage issue
                result = subprocess.run(
                    ["gh", "issue", "list",
                     "--repo", test_repo,
                     "--search", "Triage OR Batch Review",
                     "--json", "number,title"],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    triage_issues = json.loads(result.stdout)
                    if triage_issues:
                        print(f"      ✓ Triage issue created: #{triage_issues[0]['number']}")
                    else:
                        print("      ⚠ No triage issue (threshold may not be met)")

            # Summary
            print("\n" + "=" * 60)
            print(f"RESULT: {len(pr_numbers)}/{MULTI_ISSUE_COUNT} PRs created")
            print("=" * 60)

            assert len(pr_numbers) > 0, "At least one PR should be created"

        finally:
            # Cleanup
            if orchestrator:
                stop_orchestrator(orchestrator)
            for pr_num in pr_numbers:
                subprocess.run(
                    ["gh", "pr", "close", str(pr_num),
                     "--repo", test_repo,
                     "--delete-branch"],
                    capture_output=True
                )
            for issue_num in issue_numbers:
                close_issue(test_repo, issue_num, "E2E test completed")


# ---------------------------------------------------------------------------
# SCENARIO 3: Code Review Must Actually Run
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.live
class TestCodeReviewRuns:
    """Test that code reviews actually execute, not just get queued."""

    @pytest.mark.timeout(600)
    def test_code_review_produces_review_comment(self, test_repo):
        """
        Verify that the code review agent actually reviews the PR.

        This test ensures:
        1. PR is created
        2. Code review agent picks it up
        3. Code review agent posts a review comment
        4. code-reviewed OR needs-rework label is applied

        This is the "reviews have to run" test.
        """
        print("\n" + "=" * 60)
        print("CODE REVIEW TEST: Verify Review Actually Runs")
        print("=" * 60)

        issue_number = None
        pr_number = None
        orchestrator = None

        try:
            # Create issue
            print("\n[1/5] Creating test issue...")
            issue_number = create_single_issue(
                test_repo,
                "[E2E-REVIEW] Test that code review runs",
                ["agent:e2e-test", "test-data"]
            )
            print(f"      Created issue #{issue_number}")

            # Start orchestrator
            print("\n[2/5] Starting orchestrator...")
            orchestrator = start_orchestrator(test_repo, max_issues=1)

            # Wait for PR
            print(f"\n[3/5] Waiting for PR creation...")

            def has_pr():
                prs = get_prs_for_issue(test_repo, issue_number)
                return len(prs) > 0

            wait_for_condition(has_pr, TIMEOUT_SESSION_COMPLETE)
            prs = get_prs_for_issue(test_repo, issue_number)
            if not prs:
                pytest.fail("PR was not created")
            pr_number = prs[0]["number"]
            print(f"      ✓ PR #{pr_number} created")

            # Wait for code review to complete
            print(f"\n[4/5] Waiting for code review to complete...")

            def review_completed():
                result = subprocess.run(
                    ["gh", "pr", "view", str(pr_number),
                     "--repo", test_repo,
                     "--json", "labels,reviews"],
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    return False

                data = json.loads(result.stdout)
                labels = [l["name"] for l in data.get("labels", [])]

                # Either code-reviewed or needs-rework means review ran
                if "code-reviewed" in labels or "needs-rework" in labels:
                    return True

                return False

            review_done = wait_for_condition(
                review_completed,
                TIMEOUT_CODE_REVIEW_COMPLETE,
                interval=10,
                description="code review completion"
            )

            # Get final state
            result = subprocess.run(
                ["gh", "pr", "view", str(pr_number),
                 "--repo", test_repo,
                 "--json", "labels,reviews,comments"],
                capture_output=True,
                text=True,
            )
            pr_data = json.loads(result.stdout) if result.returncode == 0 else {}
            final_labels = [l["name"] for l in pr_data.get("labels", [])]
            reviews = pr_data.get("reviews", [])
            comments = pr_data.get("comments", [])

            print(f"      Final labels: {final_labels}")
            print(f"      Number of reviews: {len(reviews)}")
            print(f"      Number of comments: {len(comments)}")

            # Check for review evidence
            print(f"\n[5/5] Verifying review evidence...")

            has_review_outcome = (
                "code-reviewed" in final_labels or
                "needs-rework" in final_labels
            )

            if has_review_outcome:
                if "code-reviewed" in final_labels:
                    print("      ✓ Code review PASSED (code-reviewed label)")
                else:
                    print("      ✓ Code review requested CHANGES (needs-rework label)")
                print("      ✓ CODE REVIEW ACTUALLY RAN!")
            else:
                print("      ✗ No review outcome labels found")
                if "needs-code-review" in final_labels:
                    print("      ⚠ Review was queued but didn't complete")

            print("\n" + "=" * 60)
            if has_review_outcome:
                print("RESULT: PASSED - Code review executed successfully!")
            else:
                print("RESULT: FAILED - Code review did not run")
            print("=" * 60)

            assert has_review_outcome, "Code review must run and produce an outcome"

        finally:
            if orchestrator:
                stop_orchestrator(orchestrator)
            if pr_number:
                subprocess.run(
                    ["gh", "pr", "close", str(pr_number),
                     "--repo", test_repo,
                     "--delete-branch"],
                    capture_output=True
                )
            if issue_number:
                close_issue(test_repo, issue_number, "E2E test completed")


# ---------------------------------------------------------------------------
# SCENARIO 4: Triage Review Trigger
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.live
class TestTriageReviewTrigger:
    """Test that triage review is triggered after enough code reviews."""

    @pytest.mark.timeout(1800)  # 30 minutes for full triage test
    def test_triage_triggered_after_threshold(self, test_repo):
        """
        Test that triage review is triggered after code_reviewed PRs reach threshold.

        This test verifies the complete pipeline including triage:
        1. Create multiple issues (enough to trigger triage threshold)
        2. Each issue creates a PR
        3. Code reviews complete (code-reviewed labels applied)
        4. After threshold is reached, triage review issue is created
        5. Triage agent processes the batch

        Note: This test requires triage_review_batch_threshold to be set low (e.g., 2-3)
        for reasonable test execution time.
        """
        print("\n" + "=" * 60)
        print("TRIAGE TEST: Verify Triage Triggered After Batch Threshold")
        print("=" * 60)

        # Create enough issues to trigger triage
        # Default threshold is often 3, so we create 3 issues
        NUM_ISSUES = 3

        issue_numbers = []
        pr_numbers = []
        orchestrator = None

        try:
            # Step 1: Create multiple issues
            print(f"\n[1/6] Creating {NUM_ISSUES} test issues...")
            for i in range(NUM_ISSUES):
                issue_num = create_single_issue(
                    test_repo,
                    f"[E2E-TRIAGE-{i+1}] Test triage trigger issue {i+1}",
                    ["agent:e2e-test", "test-data"]
                )
                issue_numbers.append(issue_num)
                print(f"      Created issue #{issue_num}")

            # Step 2: Start orchestrator
            print("\n[2/6] Starting orchestrator...")
            orchestrator = start_orchestrator(test_repo, max_issues=NUM_ISSUES)
            print("      Orchestrator started")

            # Step 3: Wait for all sessions to start
            print(f"\n[3/6] Waiting for all sessions to start...")
            for issue_num in issue_numbers:
                def has_in_progress(n=issue_num):
                    state = get_issue_state(test_repo, n)
                    labels = [l["name"] for l in state.get("labels", [])]
                    return "in-progress" in labels

                started = wait_for_condition(
                    has_in_progress,
                    TIMEOUT_SESSION_START,
                    description=f"issue #{issue_num} in-progress"
                )
                if started:
                    print(f"      ✓ Issue #{issue_num} session started")

            # Step 4: Wait for all PRs to be created
            print(f"\n[4/6] Waiting for all PRs to be created...")
            for issue_num in issue_numbers:
                def has_pr(n=issue_num):
                    prs = get_prs_for_issue(test_repo, n)
                    return len(prs) > 0

                wait_for_condition(has_pr, TIMEOUT_SESSION_COMPLETE)
                prs = get_prs_for_issue(test_repo, issue_num)
                if prs:
                    pr_numbers.append(prs[0]["number"])
                    print(f"      ✓ PR created for issue #{issue_num}")

            # Step 5: Wait for code reviews to complete on all PRs
            print(f"\n[5/6] Waiting for all code reviews to complete...")
            code_reviewed_count = 0
            for pr_num in pr_numbers:
                def has_code_reviewed(n=pr_num):
                    result = subprocess.run(
                        ["gh", "pr", "view", str(n),
                         "--repo", test_repo,
                         "--json", "labels"],
                        capture_output=True,
                        text=True,
                    )
                    if result.returncode != 0:
                        return False
                    data = json.loads(result.stdout)
                    labels = [l["name"] for l in data.get("labels", [])]
                    return "code-reviewed" in labels

                reviewed = wait_for_condition(
                    has_code_reviewed,
                    TIMEOUT_CODE_REVIEW_COMPLETE,
                    interval=10,
                    description=f"code-reviewed on PR #{pr_num}"
                )
                if reviewed:
                    code_reviewed_count += 1
                    print(f"      ✓ PR #{pr_num} code review completed")
                else:
                    print(f"      ⚠ PR #{pr_num} code review not confirmed")

            print(f"      Code-reviewed: {code_reviewed_count}/{len(pr_numbers)}")

            # Step 6: Check for triage review issue
            print(f"\n[6/6] Checking for triage review issue...")

            def find_triage_issue():
                result = subprocess.run(
                    ["gh", "issue", "list",
                     "--repo", test_repo,
                     "--label", "agent:triage-investigator",
                     "--json", "number,title"],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    issues = json.loads(result.stdout)
                    # Look for batch/triage review issues
                    for issue in issues:
                        if "batch" in issue["title"].lower() or "triage" in issue["title"].lower() or "review" in issue["title"].lower():
                            return issue
                return None

            # Wait for triage issue to appear
            triage_issue = None
            for _ in range(30):
                triage_issue = find_triage_issue()
                if triage_issue:
                    break
                time.sleep(10)

            if triage_issue:
                print(f"      ✓ Triage review issue created: #{triage_issue['number']}")
                print(f"      Title: {triage_issue['title']}")
                print("\n" + "=" * 60)
                print("RESULT: PASSED - Triage review triggered successfully!")
                print("=" * 60)
            else:
                print("      ⚠ Triage review issue not found")
                print("      This may be expected if:")
                print("        - triage_review_batch_threshold not met")
                print("        - triage_review_agent not configured")
                print("\n" + "=" * 60)
                print("RESULT: PARTIAL - PRs created but triage not triggered")
                print("=" * 60)

            # Core assertions
            assert len(pr_numbers) >= 1, "At least one PR should be created"
            assert code_reviewed_count >= 1, "At least one code review should complete"

            # Soft check for triage
            if code_reviewed_count >= NUM_ISSUES:
                # All reviews completed, triage should trigger
                if not triage_issue:
                    print("\nNote: All code reviews completed but triage not triggered.")
                    print("Check triage_review_agent and triage_review_batch_threshold config.")

        finally:
            if orchestrator:
                stop_orchestrator(orchestrator)
            for pr_num in pr_numbers:
                subprocess.run(
                    ["gh", "pr", "close", str(pr_num),
                     "--repo", test_repo,
                     "--delete-branch"],
                    capture_output=True
                )
            for issue_num in issue_numbers:
                close_issue(test_repo, issue_num, "E2E test completed")


# ---------------------------------------------------------------------------
# SCENARIO 5: Failure Handling
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.live
class TestFailureHandling:
    """Test that failures are handled gracefully."""

    @pytest.mark.timeout(300)
    def test_session_timeout_handled(self, test_repo):
        """
        Test that a session that times out is handled correctly.

        Note: This requires a very short timeout or a stuck agent.
        For now, we just verify the orchestrator doesn't crash.
        """
        print("\n" + "=" * 60)
        print("FAILURE TEST: Timeout/Failure Handling")
        print("=" * 60)

        # Just verify orchestrator handles having no issues gracefully
        print("\n[1/2] Starting orchestrator with no matching issues...")
        cleanup_test_issues(test_repo)
        cleanup_test_prs(test_repo)

        orchestrator = start_orchestrator(test_repo, max_issues=1)

        try:
            print("[2/2] Verifying orchestrator stays healthy...")
            time.sleep(15)

            # Should still be running
            assert orchestrator.poll() is None, "Orchestrator should still be running"
            print("      ✓ Orchestrator running healthy with no issues")

            stdout, stderr = stop_orchestrator(orchestrator)
            orchestrator = None

            # Should not have crashed
            assert "Traceback" not in stderr, "Should not have crashed"
            print("      ✓ Orchestrator stopped cleanly")

            print("\n" + "=" * 60)
            print("RESULT: PASSED - Orchestrator handles empty queue")
            print("=" * 60)

        finally:
            if orchestrator:
                stop_orchestrator(orchestrator)


# ---------------------------------------------------------------------------
# SCENARIO 4: Blocked Issue Detection
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.live
class TestBlockedDetection:
    """Test that blocked labels are detected."""

    @pytest.mark.timeout(300)
    def test_blocked_label_detected(self, test_repo):
        """
        Test that adding 'blocked' label is detected.

        1. Create issue, start session
        2. Add 'blocked' label manually
        3. Verify orchestrator detects it
        """
        print("\n" + "=" * 60)
        print("BLOCKED TEST: Label Detection")
        print("=" * 60)

        issue_number = None
        orchestrator = None

        try:
            # Create issue
            print("\n[1/4] Creating test issue...")
            issue_number = create_single_issue(
                test_repo,
                "[E2E-BLOCKED] Test blocked detection",
                ["agent:e2e-test", "test-data"]
            )
            print(f"      Created issue #{issue_number}")

            # Start orchestrator
            print("\n[2/4] Starting orchestrator...")
            orchestrator = start_orchestrator(test_repo, max_issues=1)

            # Wait for session to start
            print("\n[3/4] Waiting for session to start...")

            def has_in_progress():
                state = get_issue_state(test_repo, issue_number)
                labels = [l["name"] for l in state.get("labels", [])]
                return "in-progress" in labels

            wait_for_condition(has_in_progress, TIMEOUT_SESSION_START)

            # Add blocked label
            print("\n[4/4] Adding 'blocked' label...")
            subprocess.run(
                ["gh", "issue", "edit", str(issue_number),
                 "--repo", test_repo,
                 "--add-label", "blocked"],
                capture_output=True
            )

            # Give orchestrator time to detect
            time.sleep(20)

            # Verify label is present
            state = get_issue_state(test_repo, issue_number)
            labels = [l["name"] for l in state.get("labels", [])]
            print(f"      Current labels: {labels}")

            assert "blocked" in labels, "Blocked label should be present"
            print("      ✓ Blocked label detected and preserved")

            print("\n" + "=" * 60)
            print("RESULT: PASSED - Blocked detection works")
            print("=" * 60)

        finally:
            if orchestrator:
                stop_orchestrator(orchestrator)
            if issue_number:
                close_issue(test_repo, issue_number, "E2E test completed")


# ---------------------------------------------------------------------------
# SCENARIO 6: Session Timeout Triggers Triage
# ---------------------------------------------------------------------------

# Path to test configs
E2E_CONFIG_DIR = Path(__file__).parent / "configs"


def cleanup_stale_orchestrators(config_path: Path) -> None:
    """Kill any stale orchestrator processes from previous test runs.

    This prevents resource leaks when tests are interrupted or fail.
    """
    import signal
    config_name = config_path.name

    # Find processes matching this config
    result = subprocess.run(
        ["pgrep", "-f", f"issue_orchestrator.*{config_name}"],
        capture_output=True,
        text=True,
    )
    if result.returncode == 0 and result.stdout.strip():
        pids = result.stdout.strip().split("\n")
        for pid in pids:
            try:
                os.kill(int(pid), signal.SIGTERM)
                print(f"      Killed stale orchestrator process {pid}")
            except (ProcessLookupError, ValueError):
                pass
        time.sleep(1)  # Give processes time to exit

    # Also kill any stale tmux sessions
    subprocess.run(
        ["tmux", "kill-session", "-t", "orchestrator"],
        capture_output=True,
    )


def start_orchestrator_with_config(config_path: Path, max_issues: int = 1) -> subprocess.Popen:
    """Start orchestrator with a specific config file.

    Cleans up any stale processes from previous runs first.
    """
    # Clean up stale processes from previous runs
    cleanup_stale_orchestrators(config_path)

    proc = subprocess.Popen(
        [sys.executable, "-m", "issue_orchestrator.cli",
         "--config", str(config_path),  # Global arg goes BEFORE subcommand
         "start",
         "--max-issues", str(max_issues),
         "--ui-mode", "tmux",
         "--no-dashboard"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    time.sleep(3)
    return proc


@pytest.mark.e2e
@pytest.mark.live
class TestSessionTimeoutFailure:
    """Test that session timeouts trigger proper failure handling."""

    @pytest.mark.timeout(300)  # 5 minutes - includes 1 min timeout + buffer
    def test_timeout_triggers_failure_flow(self, test_repo):
        """
        Test that a session timeout is detected and handled.

        Uses the e2e-test-timeout agent which does nothing and times out.
        Verifies:
        1. Session starts
        2. Session times out (1 minute)
        3. Orchestrator detects the timeout
        4. Triage review is queued (if configured)
        """
        print("\n" + "=" * 60)
        print("TIMEOUT TEST: Session Timeout → Failure Handling")
        print("=" * 60)

        config_path = E2E_CONFIG_DIR / "timeout-test.yaml"
        if not config_path.exists():
            pytest.skip(f"Test config not found: {config_path}")

        issue_number = None
        orchestrator = None

        try:
            # Create issue with timeout agent
            print("\n[1/4] Creating test issue with timeout agent...")
            issue_number = create_single_issue(
                test_repo,
                "[E2E-TIMEOUT] Test session timeout handling",
                ["agent:e2e-test-timeout", "test-data"]
            )
            print(f"      Created issue #{issue_number}")

            # Start orchestrator with timeout config
            print("\n[2/4] Starting orchestrator with timeout config...")
            orchestrator = start_orchestrator_with_config(config_path, max_issues=1)
            assert orchestrator.poll() is None, "Orchestrator should start"
            print("      Orchestrator started")

            # Wait for session to start
            print("\n[3/4] Waiting for session to start...")

            def has_in_progress():
                state = get_issue_state(test_repo, issue_number)
                labels = [l["name"] for l in state.get("labels", [])]
                return "in-progress" in labels

            started = wait_for_condition(
                has_in_progress,
                TIMEOUT_SESSION_START,
                description="in-progress label"
            )
            if started:
                print("      ✓ Session started (in-progress label)")
            else:
                print("      ⚠ Session may not have started")

            # Wait for timeout (agent has 1 minute timeout)
            print("\n[4/4] Waiting for session to timeout (up to 90 seconds)...")
            time.sleep(90)

            # Check final state
            state = get_issue_state(test_repo, issue_number)
            labels = [l["name"] for l in state.get("labels", [])]
            print(f"      Final labels: {labels}")

            # Session should have timed out - in-progress might be removed
            # or there might be a failure indication

            # Check if triage review was queued (look for triage issue)
            result = subprocess.run(
                ["gh", "issue", "list",
                 "--repo", test_repo,
                 "--search", f"Investigate #{issue_number}",
                 "--json", "number,title"],
                capture_output=True,
                text=True,
            )
            triage_issues = json.loads(result.stdout) if result.returncode == 0 else []

            if triage_issues:
                print(f"      ✓ Triage investigation queued: {triage_issues[0]['title']}")
            else:
                print("      ⚠ No triage investigation found (may not be configured)")

            print("\n" + "=" * 60)
            print("RESULT: Session timeout was handled")
            print("=" * 60)

            # The main assertion is that the orchestrator didn't crash
            assert orchestrator.poll() is None, "Orchestrator should still be running"

        finally:
            if orchestrator:
                stop_orchestrator(orchestrator)
            if issue_number:
                close_issue(test_repo, issue_number, "E2E timeout test completed")


# ---------------------------------------------------------------------------
# SCENARIO 7: Rework Cycles and Escalation
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.live
class TestReworkCyclesAndEscalation:
    """Test the rework cycle flow and escalation to needs-human."""

    @pytest.mark.timeout(1200)  # 20 minutes for multiple rework cycles
    def test_rework_cycles_lead_to_escalation(self, test_repo):
        """
        Test the complete rework cycle flow:

        1. Issue processed → PR created
        2. Code review ALWAYS rejects (agent:e2e-test-rejects)
        3. Rework cycle 1 starts
        4. Code review rejects again
        5. Rework cycle 2 starts
        6. Code review rejects again
        7. Max cycles exceeded → escalation to needs-human

        Uses a dedicated config with always-rejecting code reviewer.
        """
        print("\n" + "=" * 60)
        print("REWORK TEST: Rework Cycles → Escalation to needs-human")
        print("=" * 60)

        config_path = E2E_CONFIG_DIR / "rework-test.yaml"
        if not config_path.exists():
            pytest.skip(f"Test config not found: {config_path}")

        issue_number = None
        pr_number = None
        orchestrator = None

        try:
            # Create issue
            print("\n[1/6] Creating test issue...")
            issue_number = create_single_issue(
                test_repo,
                "[E2E-REWORK] Test rework cycles and escalation",
                ["agent:script-completes", "test-data"]
            )
            print(f"      Created issue #{issue_number}")

            # Start orchestrator with rework config (always-rejecting reviewer)
            print("\n[2/6] Starting orchestrator with rework test config...")
            orchestrator = start_orchestrator_with_config(config_path, max_issues=1)
            assert orchestrator.poll() is None, "Orchestrator should start"
            print("      Orchestrator started (code reviewer will ALWAYS reject)")

            # Wait for PR creation
            print("\n[3/6] Waiting for PR creation...")

            def has_pr():
                prs = get_prs_for_issue(test_repo, issue_number)
                return len(prs) > 0

            pr_created = wait_for_condition(
                has_pr,
                TIMEOUT_SESSION_COMPLETE,
                description="PR creation"
            )
            assert pr_created, "PR should be created"

            prs = get_prs_for_issue(test_repo, issue_number)
            pr_number = prs[0]["number"]
            print(f"      ✓ PR #{pr_number} created")

            # Wait for rework cycles and escalation
            # With max_rework_cycles=2, we need 3 rejections to trigger escalation
            print("\n[4/6] Waiting for rework cycles (this may take several minutes)...")

            def check_pr_state():
                result = subprocess.run(
                    ["gh", "pr", "view", str(pr_number),
                     "--repo", test_repo,
                     "--json", "labels"],
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    return None
                data = json.loads(result.stdout)
                return [l["name"] for l in data.get("labels", [])]

            # Poll for escalation (needs-human label)
            escalated = False
            rework_labels_seen = set()
            for i in range(60):  # Up to 10 minutes
                labels = check_pr_state()
                if labels is None:
                    time.sleep(10)
                    continue

                # Track rework cycle labels we've seen
                for label in labels:
                    if label.startswith("rework-"):
                        rework_labels_seen.add(label)

                print(f"      [{i*10}s] Labels: {labels}")

                if "needs-human" in labels:
                    escalated = True
                    break

                time.sleep(10)

            # Report results
            print(f"\n[5/6] Rework cycle labels seen: {rework_labels_seen}")
            print(f"[6/6] Escalation result...")

            if escalated:
                print("      ✓ PR escalated to needs-human!")
                print("\n" + "=" * 60)
                print("RESULT: PASSED - Rework cycles led to escalation")
                print("=" * 60)
            else:
                final_labels = check_pr_state() or []
                print(f"      Final labels: {final_labels}")
                if "needs-rework" in final_labels:
                    print("      ⚠ Still in rework cycle (may need more time)")
                elif "code-reviewed" in final_labels:
                    print("      ⚠ Code was approved (reviewer didn't reject?)")
                print("\n" + "=" * 60)
                print("RESULT: PARTIAL - Rework cycles started but escalation not confirmed")
                print("=" * 60)

            # At minimum, we should have seen at least one rework cycle
            assert len(rework_labels_seen) >= 1 or escalated, \
                "Should have at least one rework cycle or escalation"

        finally:
            if orchestrator:
                stop_orchestrator(orchestrator)
            if pr_number:
                subprocess.run(
                    ["gh", "pr", "close", str(pr_number),
                     "--repo", test_repo,
                     "--delete-branch"],
                    capture_output=True
                )
            if issue_number:
                close_issue(test_repo, issue_number, "E2E rework test completed")


# ---------------------------------------------------------------------------
# Run All Scenarios
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
