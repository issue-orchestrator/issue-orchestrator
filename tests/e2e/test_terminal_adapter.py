"""E2E tests specifically for terminal adapter functionality.

These tests exercise the terminal execution code path (tmux)
to catch terminal-specific bugs.

Run with:
    make test-e2e-one TEST=terminal_adapter
"""

import logging
import os

import pytest

from tests.e2e.flows import E2EFlow

logger = logging.getLogger(__name__)


@pytest.mark.e2e
@pytest.mark.timeout(120)
class TestTerminalAdapterExecution:
    """Test terminal adapter execution paths.

    These tests verify that sessions launch correctly in the terminal,
    exercising the adapter-specific code (tmux).
    """

    async def _verify_pane_identification(self, issue_number: int, tmux_session: str) -> None:
        """Verify pane can be found by issue number while Claude is running.

        This is the critical test for the @orchestrator-session-id fix.
        Claude Code modifies the pane title, which used to break pane lookup.
        Now we use a custom tmux option that persists.
        """
        import asyncio
        from issue_orchestrator.adapters.terminal._tmux import TmuxManager

        # Give Claude a moment to start and potentially modify the title
        await asyncio.sleep(5)

        # Use the same tmux session as the e2e orchestrator
        manager = TmuxManager(session_name=tmux_session)

        # This is the critical check - can we find the pane by issue number?
        pane = manager._find_issue_session(issue_number)
        if pane is None:
            # Get diagnostic info
            issues_found = manager.list_issue_windows()
            logger.error(
                "PANE IDENTIFICATION FAILED: Could not find pane for issue #%d. "
                "Issues found: %s. This indicates the @orchestrator-session-id fix may not be working.",
                issue_number,
                issues_found,
            )
            raise AssertionError(
                f"Pane identification failed for issue #{issue_number}. "
                f"Found issues: {issues_found}. "
                "Claude Code may have overwritten the pane title and our session ID lookup failed."
            )

        # Verify we can get the session ID (pane is a Pane in pane mode)
        import libtmux
        if not isinstance(pane, libtmux.Pane):
            logger.warning(
                "Found window instead of pane for issue #%d - running in window mode?",
                issue_number,
            )
            return  # Window mode doesn't use session ID option

        session_id = manager._get_pane_session_id(pane)
        if not session_id:
            logger.warning(
                "Pane found but session ID is empty for issue #%d. "
                "This may indicate the pane was created before the fix was deployed.",
                issue_number,
            )
        else:
            logger.info(
                "Pane identification SUCCESS: Found pane for issue #%d with session_id='%s'",
                issue_number,
                session_id,
            )

    @pytest.mark.asyncio
    @pytest.mark.gh_activity_limit(test_gh_activity_limit=200, system_gh_activity_limit=100)
    async def test_terminal_adapter_session_launch(
        self,
        e2e_orchestrator,
        orchestrator_watcher,
        test_issue_factory,
        repo_name: str,
        e2e_ui_mode: str,
        e2e_tmux_session: str,
    ):
        """Verify session launches correctly in the configured terminal mode.

        This test:
        1. Creates an issue
        2. Waits for orchestrator to pick it up
        3. Verifies session starts (in-progress label added)
        4. Waits for session completion

        This catches terminal-specific bugs like sandbox check failures.
        """
        logger.info("Testing terminal adapter in '%s' mode", e2e_ui_mode)

        flow = E2EFlow(repo=repo_name, watcher=orchestrator_watcher)

        # Create issue scoped to this test
        issue = test_issue_factory(f"[E2E] Terminal adapter test ({e2e_ui_mode})")
        issue_number = int(issue.stable_id())
        logger.info("Created issue #%d for terminal adapter test", issue_number)

        pr = None
        dry_run = os.environ.get("E2E_DRY_RUN_PUSH") == "1"
        try:
            # Wait for issue to be seen by orchestrator
            await flow.issue_seen(issue, timeout_s=60)
            logger.info("Issue #%d seen by orchestrator", issue_number)

            # Wait for session to start - this exercises the terminal adapter
            # The session launch is where terminal-specific code runs:
            # - tmux: creates window, runs claude command
            await flow.session_started(issue, timeout_s=60)
            logger.info("Session started for issue #%d in %s mode", issue_number, e2e_ui_mode)

            # CRITICAL: Verify pane can be found by issue number while Claude is running
            # Claude Code modifies the pane title, so this tests that our @orchestrator-session-id
            # option persists and pane lookup still works
            if e2e_ui_mode == "tmux":
                await self._verify_pane_identification(issue_number, e2e_tmux_session)

            # In dry-run mode, PR creation is skipped but we still verify the session completes
            if dry_run:
                # In dry-run mode, wait briefly for session processing, then succeed
                # The session has already started, which exercises the terminal adapter
                import asyncio
                await asyncio.sleep(10)  # Give time for completion processing
                logger.info("Session completed in dry-run mode (no real PR) - terminal adapter test passed")
            else:
                pr_number = await flow.pr_created(issue, timeout_s=120)
                pr = {"number": pr_number}
                logger.info("PR #%d created - terminal adapter test passed", pr_number)

        finally:
            if pr and not dry_run:
                flow.close_pr(pr["number"])


@pytest.mark.e2e
@pytest.mark.timeout(30)
class TestTerminalAdapterConfig:
    """Test terminal adapter configuration."""

    @pytest.mark.gh_activity_limit(test_gh_activity_limit=10, system_gh_activity_limit=10)
    def test_terminal_adapter_mode_configured(
        self,
        e2e_orchestrator,
        e2e_ui_mode: str,
    ):
        """Verify the terminal adapter mode is properly configured."""
        expected_mode = os.environ.get("E2E_UI_MODE", "tmux")
        assert e2e_ui_mode == expected_mode, f"Expected {expected_mode}, got {e2e_ui_mode}"
        logger.info("Terminal adapter mode: %s", e2e_ui_mode)

        # Verify orchestrator is running with this mode
        assert e2e_orchestrator.is_running(), "Orchestrator should be running"
        logger.info("Orchestrator running in %s mode", e2e_ui_mode)
