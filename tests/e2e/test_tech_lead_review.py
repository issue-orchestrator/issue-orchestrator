"""E2E tests for tech_lead review triggering.

Separated from test_real_scenarios.py because this test starts its own
orchestrator process, which acquires the repo-root lock and kills any
shared session-scoped orchestrator. Running in a separate file ensures
the shared orchestrator has already been torn down before this test's
session starts.
"""

import asyncio
import copy
import logging
import os
import time
from pathlib import Path

import pytest

from tests.e2e.conftest import (
    OrchestratorProcess,
    e2e_label,
    _github_adapter,
)
from issue_orchestrator.testing.support.test_data import close_issue, cleanup_issues_by_label
from tests.e2e.flows import (
    E2EFlow,
    start_orchestrator_runtime,
)

logger = logging.getLogger(__name__)

TIMEOUT_SESSION_COMPLETE = 300
TIMEOUT_CODE_REVIEW_COMPLETE = 240


async def _wait_for_issue_with_labels(
    watcher,
    *,
    labels: tuple[str, ...],
    timeout_s: float,
) -> str:
    deadline = time.monotonic() + timeout_s
    required = set(labels)

    while time.monotonic() < deadline:
        for issue_key, issue_view in watcher.view.issues.items():
            if required.issubset(set(issue_view.labels)):
                return issue_key
        try:
            await asyncio.wait_for(watcher._notify.wait(), timeout=1.0)  # noqa: SLF001
        except asyncio.TimeoutError:
            pass
        watcher._notify.clear()  # noqa: SLF001

    raise TimeoutError(f"Timed out waiting for issue with labels {sorted(required)}")


@pytest.mark.e2e
@pytest.mark.live
@pytest.mark.timeout(1800)  # 30 minutes
class TestTechLeadReviewTrigger:
    """Test that tech_lead review is triggered after enough code reviews."""

    @pytest.mark.asyncio
    @pytest.mark.gh_activity_limit(test_gh_activity_limit=390, system_gh_activity_limit=100)
    async def test_tech_lead_triggered_after_threshold(
        self,
        repo_name: str,
        e2e_project_root: Path,
        e2e_session_config,
    ):
        """Test that tech_lead review is triggered after code_reviewed PRs reach threshold.

        Note: Requires real PRs and code reviews - skipped in dry-run mode.
        """
        dry_run = os.environ.get("E2E_DRY_RUN_PUSH") == "1"
        if dry_run:
            pytest.skip("Tech Lead test requires real PRs (not dry-run mode)")

        logger.info("=" * 60)
        logger.info("TECH_LEAD TEST: Verify Tech Lead Triggered After Batch Threshold")
        logger.info("=" * 60)

        NUM_ISSUES = 2
        issues = []
        issue_numbers: list[int] = []
        pr_numbers = []
        runtime = None
        flow: E2EFlow | None = None
        run_label: str | None = None

        try:
            tech_lead_config = copy.deepcopy(e2e_session_config)
            tech_lead_config.tech_lead_review_agent = "agent:tech-lead-investigator"
            tech_lead_config.tech_lead_review_label = None
            tech_lead_config.tech_lead_reviewed_label = "tech-lead-reviewed"
            tech_lead_config.tech_lead_review_threshold = 2
            tech_lead_config.tech_lead_review_on_failure = False
            tech_lead_config.control_api_port = 19081
            run_id = int(time.time())
            run_label = e2e_label(f"tech_lead_run_{run_id}")
            review_label = e2e_label(f"tech_lead_review_{run_id}")
            reviewed_label = e2e_label(f"tech_lead_reviewed_{run_id}")
            tech_lead_config.filtering.label = run_label
            tech_lead_config.e2e_pr_labels = [run_label]
            tech_lead_config.code_review_label = review_label
            tech_lead_config.code_reviewed_label = reviewed_label
            flow = E2EFlow(repo=repo_name, watcher=None, filter_label=run_label)
            flow.ensure_labels([review_label, reviewed_label])

            logger.info("Starting orchestrator with tech_lead config...")
            orchestrator = OrchestratorProcess(tech_lead_config, e2e_project_root)
            runtime = await start_orchestrator_runtime(
                orchestrator,
                tech_lead_config.control_api_port,
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
                    f"[M0-{710+i:03d}] [E2E-TRIAGE-{i+1}] Test tech_lead trigger issue {i+1}",
                    ["agent:e2e-test", e2e_label(f"tech_lead_{i}")],
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

            # Check for tech_lead review issue
            logger.info("Checking for tech_lead review issue...")
            tech_lead_issue_key = await _wait_for_issue_with_labels(
                runtime.watcher,
                labels=("agent:tech-lead-investigator", run_label),
                timeout_s=600,
            )
            logger.info("  ✓ Tech Lead review issue created: %s", tech_lead_issue_key)

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
            if run_label:
                cleanup_issues_by_label(repo_name, run_label)
