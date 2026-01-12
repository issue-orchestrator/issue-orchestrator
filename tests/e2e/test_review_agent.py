"""E2E tests for the review agent pipeline.

Tests the full flow: dev agent -> PR created -> review agent -> approved.

Run with:
    make test-real-claude-review

NOTE: These tests require real PR creation (not dry-run mode) because the
review agent needs an actual PR to review.
"""

import logging
import os

# Enable real PR creation BEFORE any pytest fixtures run.
# Review agent tests require actual PRs to exist on GitHub because:
# 1. The review agent needs to fetch and review real PR content
# 2. Labels are added to the real PR, not a fake one
# 3. Dry-run mode creates fake PR numbers (90000-99999) that don't exist
os.environ["E2E_DRY_RUN_PUSH"] = "false"

import pytest

from tests.e2e.flows import E2EFlow
from tests.e2e.fixtures import poll_issue_label

logger = logging.getLogger(__name__)


@pytest.mark.e2e
@pytest.mark.timeout(600)  # 10 minutes - dev agent + review agent
class TestReviewAgentExecution:
    """Test the dev + review agent pipeline in smaller checkpoints."""

    async def _create_issue_and_wait_for_pr(
        self,
        flow: E2EFlow,
        orchestrator_watcher,
        issue_title: str,
    ) -> tuple[int, int]:
        issue = flow.create_issue(issue_title, ["agent:e2e-test"])[0]
        issue_number = int(issue.stable_id())
        logger.info("Created issue #%d for review agent test", issue_number)

        await flow.issue_seen(issue, timeout_s=60)
        await flow.session_started(issue, timeout_s=60)
        logger.info("Dev agent session started for issue #%d", issue_number)

        from tests.e2e.fixtures.wait_helpers import wait_for_session_completed
        completion_event = await wait_for_session_completed(
            orchestrator_watcher,
            str(issue_number),
            timeout_s=180,
            fail_on_blocked_failed=True,
        )
        pr_url = completion_event.get("payload", {}).get("pr_url")
        assert pr_url, f"Dev agent completed but no PR created. Event: {completion_event}"
        pr_number = int(pr_url.rstrip("/").split("/")[-1])
        logger.info("Dev agent created PR: %s", pr_url)
        return issue_number, pr_number

    @pytest.mark.asyncio
    @pytest.mark.timeout(300)
    @pytest.mark.gh_activity_limit(test_gh_activity_limit=200, system_gh_activity_limit=150)
    async def test_dev_agent_creates_pr(
        self,
        e2e_orchestrator,
        orchestrator_watcher,
        e2e_flow,
        e2e_ui_mode: str,
    ):
        """Verify dev agent creates a PR and sets pr-pending."""
        logger.info("Testing dev agent PR creation")
        flow = e2e_flow
        flow.fail_on_blocked_failed = True

        issue_number, pr_number = await self._create_issue_and_wait_for_pr(
            flow,
            orchestrator_watcher,
            "[E2E] Review agent test - dev PR created",
        )

        await poll_issue_label(flow.repo, issue_number, "pr-pending", backoff=(1, 2, 4, 8))
        logger.info("pr-pending label verified on issue #%d (PR #%d)", issue_number, pr_number)

    @pytest.mark.asyncio
    @pytest.mark.timeout(360)
    @pytest.mark.gh_activity_limit(test_gh_activity_limit=250, system_gh_activity_limit=150)
    async def test_review_agent_approves_pr(
        self,
        e2e_orchestrator,
        orchestrator_watcher,
        e2e_flow,
        e2e_ui_mode: str,
    ):
        """Verify review agent applies the approval label to the PR."""
        logger.info("Testing review agent approval")
        flow = e2e_flow
        flow.fail_on_blocked_failed = True

        issue_number, pr_number = await self._create_issue_and_wait_for_pr(
            flow,
            orchestrator_watcher,
            "[E2E] Review agent test - review approval",
        )

        await poll_issue_label(
            flow.repo,
            pr_number,  # Poll the PR, not the issue
            "code-reviewed",
            backoff=(2, 4, 8, 16, 32, 64, 64, 64),
        )
        logger.info("Review agent approved PR #%d for issue #%d", pr_number, issue_number)
