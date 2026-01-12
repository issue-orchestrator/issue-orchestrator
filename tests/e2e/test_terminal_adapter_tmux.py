"""E2E tests for tmux terminal adapter."""

import logging

import pytest

from tests.e2e.flows import E2EFlow

logger = logging.getLogger(__name__)


@pytest.mark.e2e
class TestTerminalAdapterTmux:
    """Tmux-specific terminal adapter checks."""

    async def _verify_pane_identification(self, issue_number: int, tmux_session: str) -> None:
        """Verify pane can be found by issue number while Claude is running."""
        from issue_orchestrator.adapters.terminal._tmux import TmuxManager

        manager = TmuxManager(session_name=tmux_session)
        pane = manager.wait_for_issue_session(issue_number, timeout_s=30.0)

        if pane is None:
            issues_found = manager.list_issue_windows()
            logger.error(
                "PANE IDENTIFICATION FAILED: Could not find pane for issue #%d. "
                "Issues found: %s.",
                issue_number,
                issues_found,
            )
            raise AssertionError(
                f"Pane identification failed for issue #{issue_number}. "
                f"Found issues: {issues_found}."
            )

        import libtmux
        if not isinstance(pane, libtmux.Pane):
            logger.warning(
                "Found window instead of pane for issue #%d - running in window mode?",
                issue_number,
            )
            return

        session_id = manager._get_pane_session_id(pane)
        if not session_id:
            logger.warning(
                "Pane found but session ID is empty for issue #%d.",
                issue_number,
            )
        else:
            logger.info(
                "Pane identification SUCCESS: Found pane for issue #%d with session_id='%s'",
                issue_number,
                session_id,
            )

    @pytest.mark.asyncio
    @pytest.mark.timeout(180)
    @pytest.mark.gh_activity_limit(test_gh_activity_limit=150, system_gh_activity_limit=100)
    async def test_terminal_adapter_tmux_session_starts(
        self,
        e2e_orchestrator,
        orchestrator_watcher,
        test_issue_factory,
        repo_name: str,
        e2e_ui_mode: str,
        e2e_tmux_session: str,
    ):
        """Verify tmux session launches and pane identification."""
        if e2e_ui_mode != "tmux":
            pytest.skip("tmux-only check")
        logger.info("Testing terminal adapter in '%s' mode", e2e_ui_mode)

        flow = E2EFlow(repo=repo_name, watcher=orchestrator_watcher)
        issue = test_issue_factory(f"[E2E] Terminal adapter test ({e2e_ui_mode})")
        issue_number = int(issue.stable_id())
        logger.info("Created issue #%d for terminal adapter test", issue_number)

        await flow.issue_seen(issue, timeout_s=60)
        await flow.session_started(issue, timeout_s=60)
        logger.info("Session started for issue #%d in %s mode", issue_number, e2e_ui_mode)

        await self._verify_pane_identification(issue_number, e2e_tmux_session)

    @pytest.mark.asyncio
    @pytest.mark.timeout(300)
    @pytest.mark.gh_activity_limit(test_gh_activity_limit=200, system_gh_activity_limit=100)
    async def test_terminal_adapter_tmux_completion_pipeline(
        self,
        e2e_orchestrator,
        orchestrator_watcher,
        test_issue_factory,
        repo_name: str,
        e2e_ui_mode: str,
    ):
        """Verify completion pipeline for tmux sessions."""
        if e2e_ui_mode != "tmux":
            pytest.skip("tmux-only check")
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
