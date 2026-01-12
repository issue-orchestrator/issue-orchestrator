"""E2E tests for subprocess terminal adapter."""

import logging
import os

import pytest

from tests.e2e.flows import E2EFlow
from tests.e2e.fixtures.logging_utils import find_worktree_for_issue

logger = logging.getLogger(__name__)


@pytest.mark.e2e
class TestTerminalAdapterSubprocess:
    """Subprocess-specific terminal adapter checks."""

    def _require_subprocess(self) -> None:
        if os.environ.get("E2E_TERMINAL_ADAPTER") != "subprocess":
            pytest.skip("subprocess-only check")

    @pytest.mark.asyncio
    @pytest.mark.timeout(180)
    @pytest.mark.gh_activity_limit(test_gh_activity_limit=150, system_gh_activity_limit=100)
    async def test_terminal_adapter_subprocess_session_starts(
        self,
        e2e_orchestrator,
        orchestrator_watcher,
        test_issue_factory,
        repo_name: str,
        e2e_ui_mode: str,
    ):
        """Verify subprocess session launches."""
        self._require_subprocess()
        logger.info("Testing terminal adapter in '%s' mode", e2e_ui_mode)

        flow = E2EFlow(repo=repo_name, watcher=orchestrator_watcher)
        issue = test_issue_factory(f"[E2E] Terminal adapter test ({e2e_ui_mode})")
        issue_number = int(issue.stable_id())
        logger.info("Created issue #%d for terminal adapter test", issue_number)

        await flow.issue_seen(issue, timeout_s=60)
        await flow.session_started(issue, timeout_s=60)
        logger.info("Session started for issue #%d in %s mode", issue_number, e2e_ui_mode)

    @pytest.mark.asyncio
    @pytest.mark.timeout(300)
    @pytest.mark.gh_activity_limit(test_gh_activity_limit=200, system_gh_activity_limit=100)
    async def test_terminal_adapter_subprocess_completion_pipeline(
        self,
        e2e_orchestrator,
        orchestrator_watcher,
        test_issue_factory,
        repo_name: str,
        e2e_ui_mode: str,
        e2e_worktree_base,
    ):
        """Verify completion pipeline for subprocess sessions."""
        self._require_subprocess()
        logger.info("Testing terminal adapter completion in '%s' mode", e2e_ui_mode)

        flow = E2EFlow(repo=repo_name, watcher=orchestrator_watcher)
        issue = test_issue_factory(f"[E2E] Terminal adapter completion ({e2e_ui_mode})")
        issue_number = int(issue.stable_id())
        logger.info("Created issue #%d for terminal adapter completion test", issue_number)

        await flow.issue_seen(issue, timeout_s=60)

        from tests.e2e.fixtures.wait_helpers import wait_for_session_completed
        completion_event = await wait_for_session_completed(
            orchestrator_watcher, str(issue_number), timeout_s=120
        )
        logger.info(
            "Session completed for issue #%d - event type: %s",
            issue_number,
            completion_event.get("type"),
        )

        payload = completion_event.get("payload", {})
        pr_url = payload.get("pr_url")
        assert pr_url, (
            "Session completed but no PR URL in event. "
            "This means completion.json was found but PR creation failed. "
            f"Payload: {payload}"
        )
        logger.info("PR created: %s - cd fix verified!", pr_url)

        await flow.issue_has_label(issue, "pr-pending", timeout_s=30)
        logger.info("pr-pending label added - full pipeline verified for issue #%d", issue_number)

        worktree = find_worktree_for_issue(issue_number, worktree_base=e2e_worktree_base)
        assert worktree is not None, f"Could not locate worktree for issue #{issue_number}"
        session_dir = worktree / ".issue-orchestrator" / "sessions" / f"issue-{issue_number}"
        assert session_dir.exists(), f"Session dir missing: {session_dir}"
        log_path = session_dir / "session.log"
        assert log_path.exists(), f"Session log missing: {log_path}"
        log_content = log_path.read_text(errors="ignore")
        assert "Starting e2e completion script" in log_content, (
            "Expected subprocess session log to include script output"
        )
