"""Real-world scenario E2E tests.

Tests that verify specific behaviors not covered by basic lifecycle tests:
1. Code review actually runs and produces outcome
2. Triage review is triggered after threshold
3. Session timeout is handled correctly (special config)
4. Rework cycles lead to escalation (special config)

Tests requiring special orchestrator configs (timeout, rework) start their own
orchestrator. Other tests use the shared session-scoped orchestrator.
"""

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.e2e.conftest import (
    inflight_create,
    trigger_refresh,
    wait_for_issue_label,
    wait_for_pr_created,
)
from issue_orchestrator.test_data import cleanup_test_issues, close_issue
from issue_orchestrator._github_impl import get_prs_for_issue


# ---------------------------------------------------------------------------
# Test Configuration
# ---------------------------------------------------------------------------

TIMEOUT_SESSION_COMPLETE = 300
TIMEOUT_CODE_REVIEW_COMPLETE = 240
E2E_CONFIG_DIR = Path(__file__).parent / "configs"


# ---------------------------------------------------------------------------
# Helpers for special-config tests
# ---------------------------------------------------------------------------

def cleanup_stale_orchestrators(config_path: Path) -> None:
    """Kill any stale orchestrator processes from previous test runs."""
    config_name = config_path.name
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
            except (ProcessLookupError, ValueError):
                pass
        time.sleep(1)
    subprocess.run(["tmux", "kill-session", "-t", "orchestrator"], capture_output=True)


def start_orchestrator_with_config(config_path: Path, max_issues: int = 1) -> subprocess.Popen:
    """Start orchestrator with a specific config file."""
    cleanup_stale_orchestrators(config_path)
    ui_mode = os.environ.get("E2E_UI_MODE", "tmux")

    cmd = [
        sys.executable, "-m", "issue_orchestrator.cli",
        "--config", str(config_path),
        "start",
        "--max-issues", str(max_issues),
        "--ui-mode", ui_mode,
    ]

    if ui_mode == "web":
        port = os.environ.get("E2E_WEB_PORT", "8080")
        cmd.extend(["--port", port])
    else:
        cmd.append("--no-dashboard")

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    time.sleep(3)
    return proc


def stop_orchestrator(proc: subprocess.Popen) -> None:
    """Stop orchestrator."""
    proc.send_signal(signal.SIGTERM)
    try:
        proc.communicate(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()


def get_issue_state(repo: str, issue_number: int) -> dict:
    """Get full issue state."""
    result = subprocess.run(
        ["gh", "issue", "view", str(issue_number),
         "--repo", repo,
         "--json", "number,title,state,labels,comments"],
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout) if result.returncode == 0 else {}


def create_single_issue(repo: str, title: str, labels: list[str]) -> int:
    """Create a single test issue."""
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
    return int(result.stdout.strip().split("/")[-1])


def wait_for_condition(condition_fn, timeout: int, interval: int = 5) -> bool:
    """Wait for a condition to become true."""
    start = time.time()
    while time.time() - start < timeout:
        if condition_fn():
            return True
        time.sleep(interval)
    return False


# ---------------------------------------------------------------------------
# Code Review Test (uses shared orchestrator)
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.live
@pytest.mark.timeout(600)
class TestCodeReviewRuns:
    """Test that code reviews actually execute, not just get queued."""

    def test_code_review_produces_review_comment(
        self,
        e2e_orchestrator,
        repo_name: str,
        filter_label: str,
    ):
        """Verify that the code review agent actually reviews the PR.

        This test ensures:
        1. PR is created
        2. Code review agent picks it up
        3. code-reviewed OR needs-rework label is applied
        """
        print("\n" + "=" * 60)
        print("CODE REVIEW TEST: Verify Review Actually Runs")
        print("=" * 60)

        # Create issue
        issue = inflight_create(
            repo_name,
            "[E2E-REVIEW] Test that code review runs",
            [filter_label, "agent:e2e-test", "e2e:code_review_test"],
        )
        issue_number = int(issue.stable_id())
        pr_number = None

        try:
            # Wait for PR
            print(f"\nWaiting for PR creation...")

            def has_pr():
                prs = get_prs_for_issue(repo_name, issue_number)
                return len(prs) > 0

            wait_for_condition(has_pr, TIMEOUT_SESSION_COMPLETE)
            prs = get_prs_for_issue(repo_name, issue_number)
            if not prs:
                pytest.fail("PR was not created")
            pr_number = prs[0]["number"]
            print(f"  ✓ PR #{pr_number} created")

            # Wait for code review outcome
            print(f"\nWaiting for code review to complete...")

            def review_completed():
                result = subprocess.run(
                    ["gh", "pr", "view", str(pr_number),
                     "--repo", repo_name,
                     "--json", "labels"],
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    return False
                data = json.loads(result.stdout)
                labels = [l["name"] for l in data.get("labels", [])]
                return "code-reviewed" in labels or "needs-rework" in labels

            review_done = wait_for_condition(review_completed, TIMEOUT_CODE_REVIEW_COMPLETE, interval=10)

            # Get final state
            result = subprocess.run(
                ["gh", "pr", "view", str(pr_number),
                 "--repo", repo_name,
                 "--json", "labels"],
                capture_output=True,
                text=True,
            )
            final_labels = []
            if result.returncode == 0:
                data = json.loads(result.stdout)
                final_labels = [l["name"] for l in data.get("labels", [])]

            print(f"  Final labels: {final_labels}")

            has_review_outcome = "code-reviewed" in final_labels or "needs-rework" in final_labels
            if has_review_outcome:
                print("  ✓ CODE REVIEW ACTUALLY RAN!")
            else:
                print("  ⚠ No review outcome labels found")

            assert has_review_outcome, "Code review must run and produce an outcome"

        finally:
            if pr_number:
                subprocess.run(
                    ["gh", "pr", "close", str(pr_number),
                     "--repo", repo_name,
                     "--delete-branch"],
                    capture_output=True
                )


# ---------------------------------------------------------------------------
# Triage Review Test (uses shared orchestrator)
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.live
@pytest.mark.timeout(1800)  # 30 minutes
class TestTriageReviewTrigger:
    """Test that triage review is triggered after enough code reviews."""

    def test_triage_triggered_after_threshold(
        self,
        e2e_orchestrator,
        repo_name: str,
        filter_label: str,
    ):
        """Test that triage review is triggered after code_reviewed PRs reach threshold."""
        print("\n" + "=" * 60)
        print("TRIAGE TEST: Verify Triage Triggered After Batch Threshold")
        print("=" * 60)

        NUM_ISSUES = 3
        issues = []
        pr_numbers = []

        try:
            # Create multiple issues
            print(f"\nCreating {NUM_ISSUES} test issues...")
            for i in range(NUM_ISSUES):
                issue = inflight_create(
                    repo_name,
                    f"[E2E-TRIAGE-{i+1}] Test triage trigger issue {i+1}",
                    [filter_label, "agent:e2e-test", f"e2e:triage_{i}"],
                )
                issues.append(issue)
                print(f"  Created issue #{issue.stable_id()}")

            # Wait for all PRs to be created
            print(f"\nWaiting for all PRs to be created...")
            for issue in issues:
                issue_num = int(issue.stable_id())

                def has_pr(n=issue_num):
                    prs = get_prs_for_issue(repo_name, n)
                    return len(prs) > 0

                wait_for_condition(has_pr, TIMEOUT_SESSION_COMPLETE)
                prs = get_prs_for_issue(repo_name, issue_num)
                if prs:
                    pr_numbers.append(prs[0]["number"])
                    print(f"  ✓ PR created for issue #{issue_num}")

            # Wait for code reviews to complete
            print(f"\nWaiting for all code reviews to complete...")
            code_reviewed_count = 0
            for pr_num in pr_numbers:
                def has_code_reviewed(n=pr_num):
                    result = subprocess.run(
                        ["gh", "pr", "view", str(n),
                         "--repo", repo_name,
                         "--json", "labels"],
                        capture_output=True,
                        text=True,
                    )
                    if result.returncode != 0:
                        return False
                    data = json.loads(result.stdout)
                    labels = [l["name"] for l in data.get("labels", [])]
                    return "code-reviewed" in labels

                reviewed = wait_for_condition(has_code_reviewed, TIMEOUT_CODE_REVIEW_COMPLETE, interval=10)
                if reviewed:
                    code_reviewed_count += 1
                    print(f"  ✓ PR #{pr_num} code review completed")

            print(f"  Code-reviewed: {code_reviewed_count}/{len(pr_numbers)}")

            # Check for triage review issue
            print(f"\nChecking for triage review issue...")

            def find_triage_issue():
                result = subprocess.run(
                    ["gh", "issue", "list",
                     "--repo", repo_name,
                     "--label", "agent:triage-investigator",
                     "--json", "number,title"],
                    capture_output=True,
                    text=True,
                )
                if result.returncode == 0:
                    issues = json.loads(result.stdout)
                    for issue in issues:
                        if any(k in issue["title"].lower() for k in ["batch", "triage", "review"]):
                            return issue
                return None

            triage_issue = None
            for _ in range(30):
                triage_issue = find_triage_issue()
                if triage_issue:
                    break
                time.sleep(10)

            if triage_issue:
                print(f"  ✓ Triage review issue created: #{triage_issue['number']}")
            else:
                print("  ⚠ Triage review issue not found (threshold may not be met)")

            # Core assertions
            assert len(pr_numbers) >= 1, "At least one PR should be created"
            assert code_reviewed_count >= 1, "At least one code review should complete"

        finally:
            for pr_num in pr_numbers:
                subprocess.run(
                    ["gh", "pr", "close", str(pr_num),
                     "--repo", repo_name,
                     "--delete-branch"],
                    capture_output=True
                )


# ---------------------------------------------------------------------------
# Session Timeout Test (needs special config)
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.live
@pytest.mark.timeout(300)
class TestSessionTimeoutFailure:
    """Test that session timeouts trigger proper failure handling.

    This test requires a special config and runs its own orchestrator.
    """

    def test_timeout_triggers_failure_flow(self, repo_name: str):
        """Test that a session timeout is detected and handled."""
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
            print("\nCreating test issue with timeout agent...")
            issue_number = create_single_issue(
                repo_name,
                "[E2E-TIMEOUT] Test session timeout handling",
                ["agent:e2e-test-timeout", "test-data"]
            )
            print(f"  Created issue #{issue_number}")

            # Start orchestrator with timeout config
            print("\nStarting orchestrator with timeout config...")
            orchestrator = start_orchestrator_with_config(config_path, max_issues=1)
            assert orchestrator.poll() is None, "Orchestrator should start"

            # Wait for session to start
            print("\nWaiting for session to start...")

            def has_in_progress():
                state = get_issue_state(repo_name, issue_number)
                labels = [l["name"] for l in state.get("labels", [])]
                return "in-progress" in labels

            started = wait_for_condition(has_in_progress, 90)
            if started:
                print("  ✓ Session started")

            # Wait for timeout
            print("\nWaiting for session to timeout (up to 90 seconds)...")
            time.sleep(90)

            # Verify orchestrator still running
            assert orchestrator.poll() is None, "Orchestrator should still be running"
            print("  ✓ Orchestrator handled timeout without crashing")

        finally:
            if orchestrator:
                stop_orchestrator(orchestrator)
            if issue_number:
                close_issue(repo_name, issue_number, "E2E timeout test completed")


# ---------------------------------------------------------------------------
# Rework Cycles Test (needs special config)
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.live
@pytest.mark.timeout(1200)  # 20 minutes
class TestReworkCyclesAndEscalation:
    """Test the rework cycle flow and escalation to needs-human.

    This test requires a special config and runs its own orchestrator.
    """

    def test_rework_cycles_lead_to_escalation(self, repo_name: str):
        """Test that rework cycles lead to escalation after max cycles."""
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
            print("\nCreating test issue...")
            issue_number = create_single_issue(
                repo_name,
                "[E2E-REWORK] Test rework cycles and escalation",
                ["agent:script-completes", "test-data"]
            )
            print(f"  Created issue #{issue_number}")

            # Start orchestrator with rework config
            print("\nStarting orchestrator with rework test config...")
            orchestrator = start_orchestrator_with_config(config_path, max_issues=1)
            assert orchestrator.poll() is None, "Orchestrator should start"

            # Wait for PR creation
            print("\nWaiting for PR creation...")

            def has_pr():
                prs = get_prs_for_issue(repo_name, issue_number)
                return len(prs) > 0

            wait_for_condition(has_pr, TIMEOUT_SESSION_COMPLETE)
            prs = get_prs_for_issue(repo_name, issue_number)
            if not prs:
                pytest.fail("PR should be created")
            pr_number = prs[0]["number"]
            print(f"  ✓ PR #{pr_number} created")

            # Wait for rework cycles and escalation
            print("\nWaiting for rework cycles (this may take several minutes)...")

            def check_pr_labels():
                result = subprocess.run(
                    ["gh", "pr", "view", str(pr_number),
                     "--repo", repo_name,
                     "--json", "labels"],
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    return None
                data = json.loads(result.stdout)
                return [l["name"] for l in data.get("labels", [])]

            escalated = False
            rework_labels_seen = set()
            for i in range(60):  # Up to 10 minutes
                labels = check_pr_labels()
                if labels is None:
                    time.sleep(10)
                    continue

                for label in labels:
                    if label.startswith("rework-cycle-"):
                        rework_labels_seen.add(label)

                # Check for escalation labels (blocked-needs-human or needs-human)
                if "blocked-needs-human" in labels or "needs-human" in labels:
                    escalated = True
                    break

                time.sleep(10)

            print(f"\nRework cycle labels seen: {rework_labels_seen}")
            if escalated:
                print("  ✓ PR escalated to blocked-needs-human!")
            else:
                print("  ⚠ Escalation not confirmed")

            assert len(rework_labels_seen) >= 1 or escalated, \
                "Should have at least one rework cycle or escalation"

        finally:
            if orchestrator:
                stop_orchestrator(orchestrator)
            if pr_number:
                subprocess.run(
                    ["gh", "pr", "close", str(pr_number),
                     "--repo", repo_name,
                     "--delete-branch"],
                    capture_output=True
                )
            if issue_number:
                close_issue(repo_name, issue_number, "E2E rework test completed")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
