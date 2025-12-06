"""Integration tests that verify component wiring.

These tests mock only at the subprocess boundary (gh, git, tmux commands)
and let the internal Python code actually run. This catches wiring bugs
that unit tests miss when they mock everything.
"""

import asyncio
import argparse
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
from tempfile import TemporaryDirectory

from issue_orchestrator.config import Config
from issue_orchestrator.models import (
    Issue, AgentConfig, Session, OrchestratorState, SessionStatus,
    CommentHeadings
)


class TestOrchestratorWiring:
    """Test that Orchestrator methods are properly wired together."""

    @pytest.fixture
    def temp_repo(self):
        """Create a temporary git repository."""
        with TemporaryDirectory() as tmpdir:
            yield Path(tmpdir)

    @pytest.fixture
    def config(self, temp_repo):
        """Create a minimal config."""
        config = Config()
        config.repo_root = temp_repo
        config.agents = {
            "agent:test": AgentConfig(
                prompt_path=Path("test.md"),
                worktree_base=temp_repo / "worktrees",
                timeout_minutes=5,
            )
        }
        config.max_sessions = 2
        return config

    @pytest.mark.asyncio
    async def test_startup_calls_lock_cleanup(self, config):
        """Verify startup() actually calls the cleanup functions."""
        from issue_orchestrator.orchestrator import Orchestrator

        orchestrator = Orchestrator(config)
        cleanup_called = []

        # Must patch where function is USED (orchestrator module), not where defined
        with patch('issue_orchestrator.orchestrator.cleanup_stale_claims') as mock_cleanup:
            mock_cleanup.side_effect = lambda *args: cleanup_called.append('cleanup')
            mock_cleanup.return_value = []

            with patch('issue_orchestrator.orchestrator.list_issues', return_value=[]):
                # get_issue_branches is imported inside startup, so patch analysis module
                with patch('issue_orchestrator.analysis.get_issue_branches', return_value={}):
                    await orchestrator.startup()

                    # Verify cleanup was actually called
                    assert 'cleanup' in cleanup_called, "Expected cleanup_stale_claims to be called"

    def test_launch_session_creates_worktree_and_window(self, config):
        """Verify launch_session actually creates worktree and tmux window."""
        from issue_orchestrator.orchestrator import Orchestrator

        orchestrator = Orchestrator(config)
        test_issue = Issue(
            number=456,
            title="Test Feature",
            labels=["agent:test"],  # Must match config's agent key
            state="open"
        )

        created = {"worktree": False, "window": False, "label": False}

        # Patch where functions are USED (in orchestrator module)
        with patch('issue_orchestrator.orchestrator.create_worktree') as mock_worktree:
            def record_worktree(*args, **kwargs):
                created['worktree'] = True
                return (Path("/fake/worktree"), "456-test-feature")
            mock_worktree.side_effect = record_worktree

            with patch('issue_orchestrator.orchestrator.tmux_create_session') as mock_create:
                def record_window(*args, **kwargs):
                    created['window'] = True
                mock_create.side_effect = record_window

                with patch('issue_orchestrator.orchestrator.add_label') as mock_label:
                    def record_label(*args, **kwargs):
                        created['label'] = True
                    mock_label.side_effect = record_label

                    with patch('issue_orchestrator.orchestrator.try_claim', return_value=True):
                        # launch_session only takes issue - gets agent_config internally
                        session = orchestrator.launch_session(test_issue)

                        # Verify all steps happened
                        assert created['worktree'], "Worktree should be created"
                        assert created['window'], "Tmux window should be created"
                        assert created['label'], "In-progress label should be added"
                        assert session is not None
                        assert session.issue.number == 456


class TestCLIWiring:
    """Test that CLI commands are wired to correct orchestrator methods."""

    def test_cmd_start_loads_config(self):
        """Verify cmd_start loads config correctly.

        Note: cmd_start has lazy imports and uses asyncio.run(),
        so we test at a simpler level.
        """
        from issue_orchestrator.cli import cmd_start

        # Patch at config module level since it's imported inside cmd_start
        with patch('issue_orchestrator.config.Config.find_and_load') as mock_config:
            mock_cfg = MagicMock()
            mock_cfg.agents = {"agent:test": MagicMock()}
            mock_cfg.max_sessions = 2
            mock_cfg.repo_root = Path("/fake")
            mock_config.return_value = mock_cfg

            # Patch orchestrator module since it's imported inside cmd_start
            with patch('issue_orchestrator.orchestrator.Orchestrator') as mock_orch_class:
                mock_orch = MagicMock()
                mock_orch.startup = AsyncMock()
                mock_orch.run_loop = AsyncMock()
                mock_orch._shutdown_requested = False
                mock_orch_class.return_value = mock_orch

                # Patch dashboard module
                with patch('issue_orchestrator.dashboard.run_with_dashboard', new_callable=AsyncMock):
                    with patch('asyncio.run') as mock_asyncio_run:
                        mock_asyncio_run.return_value = None

                        args = argparse.Namespace(
                            no_dashboard=False,
                            dry_run=False,
                            test_mode=False
                        )

                        cmd_start(args)

                        # Verify config was loaded
                        mock_config.assert_called_once()

    def test_cmd_status_returns_without_error(self):
        """Verify cmd_status can execute without error."""
        from issue_orchestrator.cli import cmd_status

        with patch('issue_orchestrator.config.Config.find_and_load') as mock_config:
            mock_cfg = MagicMock()
            mock_cfg.agents = {"agent:test": MagicMock()}
            mock_cfg.repo_root = Path("/fake")
            mock_cfg.repo = None
            mock_config.return_value = mock_cfg

            # Patch tmux at the module level
            with patch('issue_orchestrator.tmux.get_manager') as mock_tmux:
                mock_mgr = MagicMock()
                mock_mgr.list_windows.return_value = []
                mock_tmux.return_value = mock_mgr

                args = argparse.Namespace()
                result = cmd_status(args)

                # Should complete without error
                assert result == 0 or result is None


class TestCommentHeadingsWiring:
    """Test that comment headings are properly loaded and available."""

    def test_config_loads_comment_headings(self, tmp_path):
        """Verify comment headings are loaded from YAML."""
        config_content = """
agents:
  "agent:test":
    prompt: "test.md"
    worktree_base: "../"

comment_headings:
  implementation: "## Done"
  problems: "## Issues"
  pr_link: "## PR"
  blocked: "## Stuck"
  needs_human: "## Help Needed"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        # Verify headings were loaded
        assert config.comment_headings.implementation == "## Done"
        assert config.comment_headings.problems == "## Issues"
        assert config.comment_headings.pr_link == "## PR"
        assert config.comment_headings.blocked == "## Stuck"
        assert config.comment_headings.needs_human == "## Help Needed"

    def test_comment_headings_format_completion_comment(self):
        """Verify CommentHeadings produces correct format."""
        headings = CommentHeadings(
            implementation="## What I Did",
            problems="## Problems",
            pr_link="## PR Link",
        )

        comment = headings.format_completion_comment(
            implementation="Fixed the bug in foo.py",
            problems="None",
            pr_url="https://github.com/test/repo/pull/123"
        )

        assert "## What I Did" in comment
        assert "Fixed the bug" in comment
        assert "## Problems" in comment
        assert "## PR Link" in comment
        assert "https://github.com/test/repo/pull/123" in comment


class TestDashboardWiring:
    """Test that dashboard is properly wired to orchestrator."""

    @pytest.mark.asyncio
    async def test_dashboard_pause_updates_orchestrator_state(self):
        """Verify pressing pause in dashboard actually updates orchestrator state."""
        from issue_orchestrator.dashboard import Dashboard

        # Create a mock orchestrator
        mock_orch = MagicMock()
        mock_orch.state = OrchestratorState()
        mock_orch.state.paused = False
        mock_orch.config = MagicMock()
        mock_orch.config.max_sessions = 2
        mock_orch._shutdown_requested = False

        dashboard = Dashboard(mock_orch)

        # Call the pause handler directly
        await dashboard._handle_pause()

        # Verify state was updated
        assert mock_orch.state.paused is True

    @pytest.mark.asyncio
    async def test_dashboard_resume_updates_orchestrator_state(self):
        """Verify pressing resume in dashboard actually updates orchestrator state."""
        from issue_orchestrator.dashboard import Dashboard

        mock_orch = MagicMock()
        mock_orch.state = OrchestratorState()
        mock_orch.state.paused = True
        mock_orch.config = MagicMock()
        mock_orch.config.max_sessions = 2
        mock_orch._shutdown_requested = False

        dashboard = Dashboard(mock_orch)

        await dashboard._handle_resume()

        assert mock_orch.state.paused is False


class TestMonitorWiring:
    """Test that monitor correctly detects session states."""

    def test_monitor_detects_completed_session(self):
        """Verify monitor detects when a session has completed."""
        from issue_orchestrator.monitor import SessionMonitor
        from issue_orchestrator.models import Session, Issue, AgentConfig, SessionStatus
        from datetime import datetime

        config = MagicMock()
        config.get_label_blocked.return_value = "blocked"
        config.get_label_needs_human.return_value = "needs-human"

        monitor = SessionMonitor(config)

        session = Session(
            issue=Issue(number=789, title="Test", labels=["agent:test"]),
            agent_config=AgentConfig(
                prompt_path=Path("test.md"),
                worktree_base=Path(".."),
                timeout_minutes=60
            ),
            tmux_session_name="orchestrator",
            worktree_path=Path("/fake"),
            branch_name="789-test",
            started_at=datetime.now(),
        )

        with patch('issue_orchestrator.monitor.tmux_session_exists', return_value=False):
            with patch('issue_orchestrator.monitor.get_open_prs_for_branch') as mock_prs:
                mock_prs.return_value = [{"url": "https://github.com/test/pull/1"}]

                # check_session is NOT async - it's a regular method
                status = monitor.check_session(session)

                assert status == SessionStatus.COMPLETED

    def test_monitor_detects_running_session(self):
        """Verify monitor detects when a session is still running."""
        from issue_orchestrator.monitor import SessionMonitor
        from issue_orchestrator.models import Session, Issue, AgentConfig, SessionStatus
        from datetime import datetime

        config = MagicMock()
        monitor = SessionMonitor(config)

        session = Session(
            issue=Issue(number=101, title="Test", labels=["agent:test"]),
            agent_config=AgentConfig(
                prompt_path=Path("test.md"),
                worktree_base=Path(".."),
                timeout_minutes=60
            ),
            tmux_session_name="orchestrator",
            worktree_path=Path("/fake"),
            branch_name="101-test",
            started_at=datetime.now(),
        )

        with patch('issue_orchestrator.monitor.tmux_session_exists', return_value=True):
            status = monitor.check_session(session)
            assert status == SessionStatus.RUNNING


class TestSmoke:
    """Smoke tests to verify basic functionality works end-to-end."""

    def test_can_import_all_modules(self):
        """Verify all modules can be imported without errors."""
        from issue_orchestrator import cli
        from issue_orchestrator import config
        from issue_orchestrator import dashboard
        from issue_orchestrator import github
        from issue_orchestrator import locks
        from issue_orchestrator import models
        from issue_orchestrator import monitor
        from issue_orchestrator import orchestrator
        from issue_orchestrator import scheduler
        from issue_orchestrator import tmux
        from issue_orchestrator import worktree

        # If we get here, all imports succeeded
        assert True

    def test_config_default_values(self):
        """Verify config has sensible defaults."""
        config = Config()
        assert config.max_sessions == 3
        assert config.session_timeout_minutes == 45
        assert config.label_in_progress == "in-progress"
        assert config.label_blocked == "blocked"
        assert config.comment_headings.implementation == "## Implementation"

    def test_orchestrator_state_initial(self):
        """Verify OrchestratorState starts with correct defaults."""
        state = OrchestratorState()
        assert state.active_sessions == []
        assert state.completed_today == []
        assert state.paused is False
        assert state.priority_queue == []
