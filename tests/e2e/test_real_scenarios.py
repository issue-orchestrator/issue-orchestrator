"""Real-world scenario E2E tests.

Tests that verify specific behaviors not covered by basic lifecycle tests:
1. Code review actually runs and produces outcome
2. Rework cycles lead to escalation

All tests use the shared session-scoped orchestrator.

Note: Tech Lead review tests are in test_tech_lead_review.py because they
start their own orchestrator (which steals the repo-root lock from the
shared orchestrator).
"""

import logging
import os

import pytest

from tests.e2e.conftest import e2e_label
from issue_orchestrator.testing.support.test_data import close_issue
from issue_orchestrator.domain.issue_key import IssueKey
from tests.e2e.flows import E2EFlow

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Test Configuration
# ---------------------------------------------------------------------------

TIMEOUT_SESSION_COMPLETE = 300
TIMEOUT_CODE_REVIEW_COMPLETE = 240


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

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
    ) -> tuple[IssueKey, int, int]:
        with e2e_timing_stats.phase("Create issue"):
            issue, issue_number = flow.create_issue(
                issue_title,
                ["agent:e2e-test", e2e_label("code_review_test")],
            )

        with e2e_timing_stats.phase("Wait for PR creation"):
            pr_number = await flow.pr_created(issue, timeout_s=TIMEOUT_SESSION_COMPLETE)

        return issue, issue_number, pr_number

    @pytest.mark.asyncio
    @pytest.mark.timeout(300)
    @pytest.mark.gh_activity_limit(test_gh_activity_limit=500, system_gh_activity_limit=100)
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
        issue_number = None
        pr_number = None

        try:
            issue, issue_number, pr_number = await self._create_issue_and_wait_for_pr(
                flow,
                "[M0-701] [E2E-REVIEW] PR creation checkpoint",
                e2e_timing_stats,
            )
            logger.info("  ✓ PR #%s created", pr_number)

        finally:
            with e2e_timing_stats.phase("Cleanup"):
                if pr_number:
                    flow.close_pr(pr_number)
                if issue and issue_number is not None:
                    close_issue(repo_name, issue_number, "E2E code review test completed")

    @pytest.mark.asyncio
    @pytest.mark.timeout(420)
    @pytest.mark.gh_activity_limit(test_gh_activity_limit=600, system_gh_activity_limit=120)
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
        issue_number = None
        pr_number = None

        try:
            issue, issue_number, pr_number = await self._create_issue_and_wait_for_pr(
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
                if issue and issue_number is not None:
                    close_issue(repo_name, issue_number, "E2E code review test completed")


# ---------------------------------------------------------------------------
# Rework Cycles Test (uses shared orchestrator)
# ---------------------------------------------------------------------------

@pytest.mark.e2e
@pytest.mark.live
@pytest.mark.timeout(1800)  # 30 minutes
class TestReworkCyclesAndEscalation:
    """Test the rework cycle flow and escalation to needs-human.

    Uses shared orchestrator with review-decider behavior.
    """

    @pytest.mark.asyncio
    @pytest.mark.timeout(720)
    @pytest.mark.gh_activity_limit(test_gh_activity_limit=700, system_gh_activity_limit=100)
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
                pr_number=pr_number,
            )

            logger.info("Rework cycle labels seen: %s", sorted(list(rework_labels_seen)))
            assert len(rework_labels_seen) >= 1, "Expected at least one rework-cycle label"

        finally:
            if pr_number:
                flow.close_pr(pr_number)
            if issue_number:
                close_issue(repo_name, issue_number, "E2E rework cycle label test completed")

    @pytest.mark.asyncio
    @pytest.mark.timeout(900)
    @pytest.mark.gh_activity_limit(test_gh_activity_limit=900, system_gh_activity_limit=150)
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
                wait_for_escalation=True,
                pr_number=pr_number,
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
