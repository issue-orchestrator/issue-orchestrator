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
from tests.e2e.fixtures.logging_utils import find_worktree_for_issue

logger = logging.getLogger(__name__)


@pytest.mark.e2e
@pytest.mark.timeout(300)  # Longer timeout for real PR creation
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
        from issue_orchestrator.adapters.terminal._tmux import TmuxManager

        # Use the same tmux session as the e2e orchestrator
        manager = TmuxManager(session_name=tmux_session)

        # Wait for pane to exist (uses adapter's built-in polling)
        # The pane is created after the in-progress label, so we need to wait
        pane = manager.wait_for_issue_session(issue_number, timeout_s=30.0)

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
        e2e_terminal_adapter: str | None,
        e2e_worktree_base,
    ):
        """Verify session launches correctly in the configured terminal mode.

        This test:
        1. Creates an issue
        2. Waits for orchestrator to pick it up
        3. Verifies session starts (in-progress label added)
        4. Waits for session completion

        This catches terminal-specific bugs like sandbox check failures.
        """
        adapter_name = e2e_terminal_adapter or "tmux"
        logger.info("Testing terminal adapter in '%s' mode", adapter_name)

        flow = E2EFlow(repo=repo_name, watcher=orchestrator_watcher)

        # Create issue scoped to this test
        issue = test_issue_factory(f"[E2E] Terminal adapter test ({adapter_name})")
        issue_number = int(issue.stable_id())
        logger.info("Created issue #%d for terminal adapter test", issue_number)

        # Wait for issue to be seen by orchestrator
        await flow.issue_seen(issue, timeout_s=60)
        logger.info("Issue #%d seen by orchestrator", issue_number)

        # Wait for session to start - this exercises the terminal adapter
        # The session launch is where terminal-specific code runs:
        # - tmux: creates window, runs claude command
        await flow.session_started(issue, timeout_s=60)
        logger.info("Session started for issue #%d in %s mode", issue_number, adapter_name)

        # CRITICAL: Verify pane can be found by issue number while Claude is running
        # Claude Code modifies the pane title, so this tests that our @orchestrator-session-id
        # option persists and pane lookup still works
        if adapter_name == "tmux":
            await self._verify_pane_identification(issue_number, e2e_tmux_session)

        # Wait for session to complete - this verifies the ENTIRE pipeline:
        # 1. Claude ran in the correct directory (worktree, not main repo)
        # 2. agent-done wrote completion.json to the worktree
        # 3. Orchestrator detected and processed completion.json
        # 4. Orchestrator pushed code and created PR (even if fake in dry-run mode)
        #
        # If the cd fix is broken, completion.json goes to wrong place,
        # orchestrator never finds it, and session.completed never fires.
        from tests.e2e.fixtures.wait_helpers import wait_for_session_completed
        completion_event = await wait_for_session_completed(
            orchestrator_watcher, str(issue_number), timeout_s=120
        )
        logger.info(
            "Session completed for issue #%d - event type: %s",
            issue_number,
            completion_event.get("type"),
        )

        # Verify the session completed with a PR (proves full pipeline worked)
        payload = completion_event.get("payload", {})
        pr_url = payload.get("pr_url")
        assert pr_url, (
            f"Session completed but no PR URL in event. "
            f"This means completion.json was found but PR creation failed. "
            f"Payload: {payload}"
        )
        logger.info("PR created: %s - cd fix verified!", pr_url)

        # Verify pr-pending label was added (proves post-completion actions ran)
        await flow.issue_has_label(issue, "pr-pending", timeout_s=30)
        logger.info("pr-pending label added - full pipeline verified for issue #%d", issue_number)

        if e2e_terminal_adapter == "subprocess":
            worktree = find_worktree_for_issue(issue_number, worktree_base=e2e_worktree_base)
            assert worktree is not None, f"Could not locate worktree for issue #{issue_number}"
            session_dir = worktree / ".issue-orchestrator"
            assert session_dir.exists(), f"Session dir missing: {session_dir}"
            log_path = session_dir / "session.log"
            assert log_path.exists(), f"Session log missing: {log_path}"
            log_content = log_path.read_text(errors="ignore")
            assert "Starting e2e completion script" in log_content, (
                "Expected subprocess session log to include script output"
            )


@pytest.mark.e2e
@pytest.mark.timeout(30)
class TestTerminalAdapterConfig:
    """Test terminal adapter configuration."""

    @pytest.mark.gh_activity_limit(test_gh_activity_limit=10, system_gh_activity_limit=10)
    def test_terminal_adapter_mode_configured(
        self,
        e2e_orchestrator,
        e2e_ui_mode: str,
        e2e_terminal_adapter: str | None,
    ):
        """Verify the terminal adapter mode is properly configured."""
        expected_mode = os.environ.get("E2E_UI_MODE")
        if not expected_mode and os.environ.get("E2E_TERMINAL_ADAPTER") == "subprocess":
            expected_mode = "web"
        if not expected_mode:
            expected_mode = "tmux"
        assert e2e_ui_mode == expected_mode, f"Expected {expected_mode}, got {e2e_ui_mode}"
        logger.info("Terminal adapter mode: %s", e2e_ui_mode)

        expected_adapter = os.environ.get("E2E_TERMINAL_ADAPTER")
        if expected_adapter:
            assert e2e_terminal_adapter == expected_adapter

        # Verify orchestrator is running with this mode
        assert e2e_orchestrator.is_running(), "Orchestrator should be running"
        logger.info("Orchestrator running in %s mode", e2e_ui_mode)
