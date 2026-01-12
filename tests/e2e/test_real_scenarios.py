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
from libtmux import Server
from libtmux.exc import LibTmuxException
from libtmux._internal.query_list import ObjectDoesNotExist

from tests.e2e.conftest import (
    OrchestratorProcess,
    e2e_label,
    _github_adapter,
)
from issue_orchestrator.testing.support.test_data import close_issue, cleanup_issues_by_label
from issue_orchestrator.domain.issue_key import IssueKey
from tests.e2e.flows import (
    E2EFlow,
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

def cleanup_stale_orchestrators(config_path: Path, tmux_session: str = "orchestrator") -> None:
    """Kill any stale orchestrator processes from previous test runs.

    Args:
        config_path: Path to the config file.
        tmux_session: Name of the tmux session to kill. Defaults to "orchestrator".
    """
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
    # Kill tmux session using libtmux
    try:
        server = Server()
        session = server.sessions.get(session_name=tmux_session)
        if session:
            session.kill()
    except (LibTmuxException, ObjectDoesNotExist):
        # Session doesn't exist or server not running - that's fine
        pass


def start_orchestrator_with_config(
    config_path: Path,
    max_issues: int = 1,
    tmux_session: str = "orchestrator",
) -> subprocess.Popen:
    """Start orchestrator with a specific config file.

    Args:
        config_path: Path to the config file.
        max_issues: Maximum number of issues to process.
        tmux_session: Name of the tmux session. Defaults to "orchestrator".
    """
    cleanup_stale_orchestrators(config_path, tmux_session=tmux_session)
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
            sys.executable, "-m", "issue_orchestrator.entrypoints.cli",
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

    # Set environment with tmux session name for isolation
    env = os.environ.copy()
    env["ORCHESTRATOR_TMUX_SESSION"] = tmux_session

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env=env,
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
) -> tuple[IssueKey, int]:
    """Create a single test issue (with labels ensured).

    Returns:
        Tuple of (IssueKey, issue_number)
    """
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

    async def _create_issue_and_wait_for_pr(
        self,
        flow: E2EFlow,
        issue_title: str,
        e2e_timing_stats,
    ) -> tuple[IssueKey, int]:
        with e2e_timing_stats.phase("Create issue"):
            issue, issue_number = flow.create_issue(
                issue_title,
                ["agent:e2e-test", e2e_label("code_review_test")],
            )

        with e2e_timing_stats.phase("Wait for PR creation"):
            pr_number = await flow.pr_created(issue, timeout_s=TIMEOUT_SESSION_COMPLETE)

        return issue, pr_number

    @pytest.mark.asyncio
    @pytest.mark.timeout(300)
    @pytest.mark.gh_activity_limit(test_gh_activity_limit=250, system_gh_activity_limit=100)
    async def test_code_review_pr_created(
        self,
        e2e_orchestrator,
        orchestrator_watcher,
        repo_name: str,
        filter_label: str,
        e2e_timing_stats,
    ):
        """Verify that the dev agent completes and creates a PR."""
        dry_run = os.environ.get("E2E_DRY_RUN_PUSH") == "1"
        if dry_run:
            pytest.skip("Code review test requires real PRs (not dry-run mode)")

        logger.info("=" * 60)
        logger.info("CODE REVIEW TEST: Verify PR Creation")
        logger.info("=" * 60)

        flow = E2EFlow(repo=repo_name, watcher=orchestrator_watcher, filter_label=filter_label)

        issue = None
        pr_number = None

        try:
            issue, pr_number = await self._create_issue_and_wait_for_pr(
                flow,
                "[M0-701] [E2E-REVIEW] PR creation checkpoint",
                e2e_timing_stats,
            )
            logger.info("  ✓ PR #%s created", pr_number)

        finally:
            with e2e_timing_stats.phase("Cleanup"):
                if pr_number:
                    flow.close_pr(pr_number)
                if issue:
                    close_issue(repo_name, int(issue.stable_id()), "E2E code review test completed")

    @pytest.mark.asyncio
    @pytest.mark.timeout(420)
    @pytest.mark.gh_activity_limit(test_gh_activity_limit=320, system_gh_activity_limit=120)
    async def test_code_review_outcome_label(
        self,
        e2e_orchestrator,
        orchestrator_watcher,
        repo_name: str,
        filter_label: str,
        e2e_timing_stats,
    ):
        """Verify that code review runs and applies an outcome label."""
        dry_run = os.environ.get("E2E_DRY_RUN_PUSH") == "1"
        if dry_run:
            pytest.skip("Code review test requires real PRs (not dry-run mode)")

        logger.info("=" * 60)
        logger.info("CODE REVIEW TEST: Verify Review Outcome Label")
        logger.info("=" * 60)

        flow = E2EFlow(repo=repo_name, watcher=orchestrator_watcher, filter_label=filter_label)
        issue = None
        pr_number = None

        try:
            issue, pr_number = await self._create_issue_and_wait_for_pr(
                flow,
                "[M0-702] [E2E-REVIEW] Review outcome checkpoint",
                e2e_timing_stats,
            )

            with e2e_timing_stats.phase("Wait for code review"):
                await flow.pr_has_any_label(
                    issue,
                    labels=["code-reviewed", "needs-rework"],
                    timeout_s=TIMEOUT_CODE_REVIEW_COMPLETE,
                )

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
                if issue:
                    close_issue(repo_name, int(issue.stable_id()), "E2E code review test completed")


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
        """Test that triage review is triggered after code_reviewed PRs reach threshold.

        Note: Requires real PRs and code reviews - skipped in dry-run mode.
        """
        dry_run = os.environ.get("E2E_DRY_RUN_PUSH") == "1"
        if dry_run:
            pytest.skip("Triage test requires real PRs (not dry-run mode)")

        logger.info("=" * 60)
        logger.info("TRIAGE TEST: Verify Triage Triggered After Batch Threshold")
        logger.info("=" * 60)

        NUM_ISSUES = 2
        issues = []
        issue_numbers: list[int] = []
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
                issue, issue_num = flow.create_issue(
                    f"[M0-{710+i:03d}] [E2E-TRIAGE-{i+1}] Test triage trigger issue {i+1}",
                    ["agent:e2e-test", e2e_label(f"triage_{i}")],
                )
                issues.append(issue)
                issue_numbers.append(issue_num)
                logger.info("  Created issue #%d (%s)", issue_num, issue.stable_id())
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
            for issue_num in issue_numbers:
                close_issue(repo_name, issue_num, "E2E triage test completed")


# ---------------------------------------------------------------------------
# Rework Cycles Test (needs special config)
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.live
@pytest.mark.timeout(1800)  # 30 minutes
class TestReworkCyclesAndEscalation:
    """Test the rework cycle flow and escalation to needs-human.

    Uses shared orchestrator with review-decider behavior.
    """

    @pytest.mark.asyncio
    @pytest.mark.timeout(420)
    @pytest.mark.gh_activity_limit(test_gh_activity_limit=220, system_gh_activity_limit=100)
    async def test_rework_cycle_label_emitted(
        self,
        repo_name: str,
        e2e_orchestrator,
        orchestrator_watcher,
    ):
        """Test that at least one rework-cycle label appears."""
        dry_run = os.environ.get("E2E_DRY_RUN_PUSH") == "1"
        if dry_run:
            pytest.skip("Rework test requires real PRs (not dry-run mode)")

        logger.info("=" * 60)
        logger.info("REWORK TEST: Rework Cycle Label Emitted")
        logger.info("=" * 60)

        issue_number = None
        pr_number = None
        flow = E2EFlow(
            repo=repo_name,
            watcher=orchestrator_watcher,
        )

        try:
            logger.info("Creating test issue...")
            issue_key, issue_number = create_single_issue(
                repo_name,
                "[M0-720] [E2E-REWORK] Test rework cycle label",
                ["agent:script-completes", "io-e2e-test-data", e2e_label("rework_cycles")],
                watcher=orchestrator_watcher,
            )
            logger.info("  Created issue #%d", issue_number)

            logger.info("Waiting for PR creation...")
            pr_number = await flow.pr_created(issue_key, timeout_s=TIMEOUT_SESSION_COMPLETE)
            logger.info("  ✓ PR #%s created", pr_number)

            logger.info("Waiting for first rework cycle label...")
            _escalated, rework_labels_seen = await flow.rework_progress(
                issue_key,
                timeout_s=300,
            )

            logger.info("Rework cycle labels seen: %s", sorted(list(rework_labels_seen)))
            assert len(rework_labels_seen) >= 1, "Expected at least one rework-cycle label"

        finally:
            if pr_number:
                flow.close_pr(pr_number)
            if issue_number:
                close_issue(repo_name, issue_number, "E2E rework cycle label test completed")

    @pytest.mark.asyncio
    @pytest.mark.timeout(720)
    @pytest.mark.gh_activity_limit(test_gh_activity_limit=260, system_gh_activity_limit=100)
    async def test_rework_cycles_escalate(
        self,
        repo_name: str,
        e2e_orchestrator,
        orchestrator_watcher,
    ):
        """Test that rework cycles lead to escalation after max cycles."""
        dry_run = os.environ.get("E2E_DRY_RUN_PUSH") == "1"
        if dry_run:
            pytest.skip("Rework test requires real PRs (not dry-run mode)")

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
            logger.info("Creating test issue...")
            issue_key, issue_number = create_single_issue(
                repo_name,
                "[M0-721] [E2E-REWORK] Test rework escalation",
                ["agent:script-completes", "io-e2e-test-data", e2e_label("rework_escalation")],
                watcher=orchestrator_watcher,
            )
            logger.info("  Created issue #%d", issue_number)

            logger.info("Waiting for PR creation...")
            pr_number = await flow.pr_created(issue_key, timeout_s=TIMEOUT_SESSION_COMPLETE)
            logger.info("  ✓ PR #%s created", pr_number)

            logger.info("Waiting for escalation (this may take several minutes)...")
            escalated, rework_labels_seen = await flow.rework_progress(
                issue_key,
                timeout_s=900,
            )

            logger.info("Rework cycle labels seen: %s", sorted(list(rework_labels_seen)))
            assert escalated, "Expected escalation to blocked-needs-human"

        finally:
            if pr_number:
                flow.close_pr(pr_number)
            if issue_number:
                close_issue(repo_name, issue_number, "E2E rework escalation test completed")


if __name__ == "__main__":
    pytest.main([__file__, "-v", "-s"])
