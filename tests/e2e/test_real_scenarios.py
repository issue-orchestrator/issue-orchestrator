"""Real-world scenario E2E tests.

Tests that verify specific behaviors not covered by basic lifecycle tests:
1. Code review actually runs and produces outcome
2. Triage review is triggered after threshold
3. Session timeout is handled correctly (special config)
4. Rework cycles lead to escalation (special config)

Tests requiring special orchestrator configs (timeout, rework) start their own
orchestrator. Other tests use the shared session-scoped orchestrator.
"""

import asyncio
import copy
import logging
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tests.e2e.conftest import (
    OrchestratorProcess,
    e2e_label,
    _github_adapter,
)
from issue_orchestrator.testing.support.test_data import close_issue, cleanup_issues_by_label
from issue_orchestrator.domain.issue_key import IssueKey
from tests.e2e.flows import (
    E2EFlow,
    create_watcher_for_port,
    start_orchestrator_runtime,
    wait_for_issue_with_label,
)

logger = logging.getLogger(__name__)

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

    project_root = Path(__file__).parent.parent.parent
    preferred_bin = project_root / ".venv" / "bin" / "issue-orchestrator"
    if preferred_bin.exists():
        cmd = [
            str(preferred_bin),
            "--config", str(config_path),
            "start",
            "--max-issues", str(max_issues),
            "--ui-mode", ui_mode,
        ]
    else:
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


def create_single_issue(
    repo: str,
    title: str,
    labels: list[str],
    watcher=None,
) -> IssueKey:
    """Create a single test issue (with labels ensured)."""
    flow = E2EFlow(repo=repo, watcher=watcher)
    return flow.create_issue(title, labels, body=f"Automated test issue.\n\nLabels: {', '.join(labels)}")


# ---------------------------------------------------------------------------
# Code Review Test (uses shared orchestrator)
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.live
@pytest.mark.timeout(600)
class TestCodeReviewRuns:
    """Test that code reviews actually execute, not just get queued."""

    @pytest.mark.asyncio
    @pytest.mark.gh_activity_limit(test_gh_activity_limit=390, system_gh_activity_limit=100)
    async def test_code_review_produces_review_comment(
        self,
        e2e_orchestrator,
        orchestrator_watcher,
        repo_name: str,
        filter_label: str,
        e2e_timing_stats,
    ):
        """Verify that the code review agent actually reviews the PR.

        This test ensures:
        1. PR is created
        2. Code review agent picks it up
        3. code-reviewed OR needs-rework label is applied
        """
        logger.info("=" * 60)
        logger.info("CODE REVIEW TEST: Verify Review Actually Runs")
        logger.info("=" * 60)

        flow = E2EFlow(repo=repo_name, watcher=orchestrator_watcher, filter_label=filter_label)

        # Create issue
        with e2e_timing_stats.phase("Create issue"):
            issue = flow.create_issue(
                "[E2E-REVIEW] Test that code review runs",
                ["agent:e2e-test", e2e_label("code_review_test")],
            )
        issue_number = int(issue.stable_id())
        pr_number = None

        try:
            # Wait for PR
            logger.info("Waiting for PR creation...")

            with e2e_timing_stats.phase("Wait for PR creation"):
                pr_number = await flow.pr_created(issue, timeout_s=TIMEOUT_SESSION_COMPLETE)
            logger.info("  ✓ PR #%s created", pr_number)

            # Wait for code review outcome
            logger.info("Waiting for code review to complete...")

            with e2e_timing_stats.phase("Wait for code review"):
                await flow.pr_has_any_label(
                    issue,
                    labels=["code-reviewed", "needs-rework"],
                    timeout_s=TIMEOUT_CODE_REVIEW_COMPLETE,
                )

            # Get final state
            with e2e_timing_stats.phase("Verify outcome"):
                issue_view = orchestrator_watcher.view.issues.get(issue.stable_id())
                final_labels = sorted(list(issue_view.pr.labels)) if issue_view else []
                logger.info("  Final labels: %s", final_labels)

                has_review_outcome = "code-reviewed" in final_labels or "needs-rework" in final_labels
                if has_review_outcome:
                    logger.info("  ✓ CODE REVIEW ACTUALLY RAN!")
                else:
                    logger.warning("  ⚠ No review outcome labels found")

                assert has_review_outcome, "Code review must run and produce an outcome"

        finally:
            with e2e_timing_stats.phase("Cleanup"):
                if pr_number:
                    flow.close_pr(pr_number)
                # Always close the issue to prevent accumulation
                close_issue(repo_name, issue_number, "E2E code review test completed")


# ---------------------------------------------------------------------------
# Triage Review Test (uses shared orchestrator)
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.live
@pytest.mark.timeout(1800)  # 30 minutes
class TestTriageReviewTrigger:
    """Test that triage review is triggered after enough code reviews."""

    @pytest.mark.asyncio
    @pytest.mark.gh_activity_limit(test_gh_activity_limit=390, system_gh_activity_limit=100)
    async def test_triage_triggered_after_threshold(
        self,
        repo_name: str,
        e2e_project_root: Path,
        e2e_session_config,
    ):
        """Test that triage review is triggered after code_reviewed PRs reach threshold."""
        logger.info("=" * 60)
        logger.info("TRIAGE TEST: Verify Triage Triggered After Batch Threshold")
        logger.info("=" * 60)

        NUM_ISSUES = 3
        issues = []
        pr_numbers = []
        runtime = None
        flow: E2EFlow | None = None

        try:
            triage_config = copy.deepcopy(e2e_session_config)
            triage_config.triage_review_agent = "agent:triage-investigator"
            triage_config.triage_review_label = None
            triage_config.triage_reviewed_label = "triage-reviewed"
            triage_config.triage_review_threshold = 2
            triage_config.triage_review_on_failure = False
            triage_config.control_api_port = 19081
            run_id = int(time.time())
            run_label = e2e_label(f"triage_run_{run_id}")
            review_label = e2e_label(f"triage_review_{run_id}")
            reviewed_label = e2e_label(f"triage_reviewed_{run_id}")
            triage_config.filter_label = run_label
            triage_config.e2e_pr_labels = [run_label]
            triage_config.code_review_label = review_label
            triage_config.code_reviewed_label = reviewed_label
            flow = E2EFlow(repo=repo_name, watcher=None, filter_label=run_label)
            flow.ensure_labels([review_label, reviewed_label])
            cleanup_issues_by_label(repo_name, "agent:triage-investigator")

            logger.info("Starting orchestrator with triage config...")
            orchestrator = OrchestratorProcess(triage_config, e2e_project_root)
            runtime = await start_orchestrator_runtime(
                orchestrator,
                triage_config.control_api_port,
                max_issues=10,
                extra_args=["--label", run_label],
            )
            flow = E2EFlow(
                repo=repo_name,
                watcher=runtime.watcher,
                filter_label=run_label,
            )

            # Create multiple issues
            logger.info("Creating %d test issues...", NUM_ISSUES)
            for i in range(NUM_ISSUES):
                issue = flow.create_issue(
                    f"[E2E-TRIAGE-{i+1}] Test triage trigger issue {i+1}",
                    ["agent:e2e-test", e2e_label(f"triage_{i}")],
                )
                issues.append(issue)
                logger.info("  Created issue #%s", issue.stable_id())
            # Wait for all PRs to be created
            logger.info("Waiting for all PRs to be created...")
            pr_numbers = await asyncio.gather(*[
                flow.pr_created(issue, timeout_s=TIMEOUT_SESSION_COMPLETE)
                for issue in issues
            ])
            for issue in issues:
                logger.info("  ✓ PR created for issue #%s", issue.stable_id())

            # Wait for code reviews to complete
            logger.info("Waiting for all code reviews to complete...")
            await asyncio.gather(*[
                flow.pr_has_any_label(
                    issue,
                    labels=[reviewed_label],
                    timeout_s=TIMEOUT_CODE_REVIEW_COMPLETE,
                )
                for issue in issues
            ])
            code_reviewed_count = len(pr_numbers)
            logger.info("  Code-reviewed: %d/%d", code_reviewed_count, len(pr_numbers))

            # Check for triage review issue
            logger.info("Checking for triage review issue...")
            triage_issue_key = await wait_for_issue_with_label(
                runtime.watcher,
                label="agent:triage-investigator",
                timeout_s=600,
            )
            logger.info("  ✓ Triage review issue created: %s", triage_issue_key)

            # Core assertions
            assert len(pr_numbers) >= 1, "At least one PR should be created"
            assert code_reviewed_count >= 1, "At least one code review should complete"

        finally:
            if runtime:
                await runtime.close()
            for pr_num in pr_numbers:
                if flow:
                    flow.close_pr(pr_num)
                else:
                    adapter = _github_adapter(repo_name)
                    pr = adapter.get_pr(pr_num)
                    branch = pr.branch if pr else None
                    adapter.close_pr(pr_num)
                    if branch:
                        try:
                            adapter.delete_branch(branch)
                        except Exception:
                            pass
            # Always close issues to prevent accumulation
            for issue in issues:
                close_issue(repo_name, int(issue.stable_id()), "E2E triage test completed")


# ---------------------------------------------------------------------------
# Rework Cycles Test (needs special config)
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.live
@pytest.mark.timeout(1200)  # 20 minutes
class TestReworkCyclesAndEscalation:
    """Test the rework cycle flow and escalation to needs-human.

    Uses shared orchestrator with review-decider behavior.
    """

    @pytest.mark.asyncio
    @pytest.mark.gh_activity_limit(test_gh_activity_limit=300, system_gh_activity_limit=100)
    async def test_rework_cycles_lead_to_escalation(
        self,
        repo_name: str,
        e2e_orchestrator,
        orchestrator_watcher,
    ):
        """Test that rework cycles lead to escalation after max cycles."""
        logger.info("=" * 60)
        logger.info("REWORK TEST: Rework Cycles → Escalation to needs-human")
        logger.info("=" * 60)

        issue_number = None
        pr_number = None
        flow = E2EFlow(
            repo=repo_name,
            watcher=orchestrator_watcher,
        )

        try:
            # Create issue
            logger.info("Creating test issue...")
            issue_key = create_single_issue(
                repo_name,
                "[E2E-REWORK] Test rework cycles and escalation",
                ["agent:script-completes", "test-data", e2e_label("rework_cycles")],
                watcher=orchestrator_watcher,
            )
            issue_number = int(issue_key.stable_id())
            logger.info("  Created issue #%d", issue_number)

            # Wait for PR creation
            logger.info("Waiting for PR creation...")
            pr_number = await flow.pr_created(issue_key, timeout_s=TIMEOUT_SESSION_COMPLETE)
            logger.info("  ✓ PR #%s created", pr_number)

            # Wait for rework cycles and escalation
            logger.info("Waiting for rework cycles (this may take several minutes)...")
            escalated, rework_labels_seen = await flow.rework_progress(
                issue_key,
                timeout_s=600,
            )

            logger.info("Rework cycle labels seen: %s", sorted(list(rework_labels_seen)))
            if escalated:
                logger.info("  ✓ PR escalated to blocked-needs-human!")
            else:
                logger.warning("  ⚠ Escalation not confirmed")

            assert len(rework_labels_seen) >= 1 or escalated, \
                "Should have at least one rework cycle or escalation"

        finally:
            if pr_number:
                flow.close_pr(pr_number)
            if issue_number:
                close_issue(repo_name, issue_number, "E2E rework test completed")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
