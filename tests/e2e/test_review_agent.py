"""E2E tests for the review agent pipeline.

Tests the full flow: dev agent -> PR created -> review agent -> approved.

Run with:
    make test-real-claude-review
"""

import logging

import pytest

from tests.e2e.flows import E2EFlow
from tests.e2e.fixtures import poll_issue_label

logger = logging.getLogger(__name__)


@pytest.mark.e2e
@pytest.mark.timeout(600)  # 10 minutes - dev agent + review agent
class TestReviewAgentExecution:
    """Test the full dev + review agent pipeline.

    This test verifies:
    1. Dev agent completes and creates a PR
    2. Review agent is triggered
    3. Review agent approves (adds code-reviewed label)
    """

    @pytest.mark.asyncio
    @pytest.mark.gh_activity_limit(test_gh_activity_limit=300, system_gh_activity_limit=150)
    async def test_review_agent_approves_pr(
        self,
        e2e_orchestrator,
        orchestrator_watcher,
        test_issue_factory,
        repo_name: str,
        e2e_ui_mode: str,
    ):
        """Verify dev agent creates PR and review agent approves it.

        This is the happy path test for the full pipeline:
        1. Create issue with agent:backend label
        2. Wait for dev agent to complete with PR
        3. Wait for review agent to run and approve
        """
        logger.info("Testing full pipeline: dev agent -> review agent")

        flow = E2EFlow(repo=repo_name, watcher=orchestrator_watcher)

        # Create issue - test_issue_factory adds agent:backend label
        issue = test_issue_factory("[E2E] Review agent test - happy path")
        issue_number = int(issue.stable_id())
        logger.info("Created issue #%d for review agent test", issue_number)

        # Phase 1: Wait for dev agent to complete
        logger.info("Phase 1: Waiting for dev agent to complete...")
        await flow.issue_seen(issue, timeout_s=60)
        await flow.session_started(issue, timeout_s=60)
        logger.info("Dev agent session started for issue #%d", issue_number)

        # Wait for session completion with PR
        from tests.e2e.fixtures.wait_helpers import wait_for_session_completed
        completion_event = await wait_for_session_completed(
            orchestrator_watcher, str(issue_number), timeout_s=180
        )
        pr_url = completion_event.get("payload", {}).get("pr_url")
        assert pr_url, f"Dev agent completed but no PR created. Event: {completion_event}"
        logger.info("Phase 1 complete: Dev agent created PR: %s", pr_url)

        # Extract PR number from URL (e.g., https://github.com/owner/repo/pull/123 -> 123)
        pr_number = int(pr_url.rstrip("/").split("/")[-1])

        # Verify pr-pending label (dev agent finished)
        await poll_issue_label(repo_name, issue_number, "pr-pending", backoff=(1, 2, 4, 8))
        logger.info("pr-pending label verified on issue #%d", issue_number)

        # Phase 2: Wait for review agent to complete
        logger.info("Phase 2: Waiting for review agent on PR #%d...", pr_number)

        # The review agent adds code-reviewed to the PR (not the issue)
        # Use direct polling - more reliable than SSE for cross-agent transitions
        await poll_issue_label(
            repo_name,
            pr_number,  # Poll the PR, not the issue
            "code-reviewed",
            backoff=(2, 4, 8, 16, 32, 64, 64, 64),  # Up to ~4 min with extra retries
        )
        logger.info("Phase 2 complete: Review agent approved PR #%d for issue #%d", pr_number, issue_number)

        logger.info(
            "SUCCESS: Full pipeline verified for issue #%d - dev agent -> PR -> review agent -> approved",
            issue_number,
        )
