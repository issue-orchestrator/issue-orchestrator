"""Integration tests that verify component wiring.

These tests mock only at the subprocess boundary (gh, git, tmux commands)
and let the internal Python code actually run. This catches wiring bugs
that unit tests miss when they mock everything.

Note: We still inject mock adapters (like MockGitHubAdapter) to avoid real
API calls, but this tests the actual wiring between components.
"""

import asyncio
import argparse
import pytest
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock
from tempfile import TemporaryDirectory

from issue_orchestrator.config import Config, DangerousConfig
from issue_orchestrator.models import (
    Issue, AgentConfig, Session, OrchestratorState, SessionStatus,
    CommentHeadings
)
# Import MockGitHubAdapter from conftest (it's available as fixture)


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
        config.ui_mode = "tmux"  # Use tmux so tests can patch create_session
        config.agents = {
            "agent:test": AgentConfig(
                prompt_path=Path("test.md"),
                worktree_base=temp_repo / "worktrees",
                timeout_minutes=5,
            )
        }
        config.max_concurrent_sessions = 2
        # Use temp directory for state file to isolate tests
        config.state_file = temp_repo / ".issue-orchestrator" / "state.json"
        # Skip hook verification in tests
        config.dangerous = DangerousConfig(skip_verification=True, allow_unsupported_agents=True)
        return config

    @pytest.mark.asyncio
    async def test_startup_queries_in_progress_issues(self, config, mock_repository_host):
        """Verify startup() queries for in-progress issues."""
        from issue_orchestrator.orchestrator import Orchestrator
        from issue_orchestrator.execution.worktree_adapter import GitWorktreeManager
        from issue_orchestrator.execution.git_working_copy import GitWorkingCopy

        orchestrator = Orchestrator(
            config,
            _repository_host=mock_repository_host,
            worktree_manager=GitWorktreeManager(),
            working_copy=GitWorkingCopy(),
        )

        with patch('issue_orchestrator.analysis.get_issue_branches', return_value={}):
            await orchestrator.startup()

            # Verify list_issues was called via the adapter
            assert len(mock_repository_host.list_issues_calls) > 0

    def test_launch_session_creates_worktree_and_window(self, config, patch_plugin_manager, mock_repository_host):
        """Verify launch_session actually creates worktree and tmux window."""
        from issue_orchestrator.orchestrator import Orchestrator
        from issue_orchestrator.ports.worktree_manager import WorktreeInfo
        from issue_orchestrator.execution.git_working_copy import GitWorkingCopy

        # Configure mock plugin to allow session creation
        patch_plugin_manager.plugin.session_exists_override = False

        # Create a mock WorktreeManager
        mock_worktree_manager = MagicMock()
        mock_worktree_manager.create.return_value = WorktreeInfo(
            path=Path("/fake/worktree"),
            branch_name="456-test-feature",
        )

        orchestrator = Orchestrator(
            config,
            _repository_host=mock_repository_host,
            worktree_manager=mock_worktree_manager,
            working_copy=GitWorkingCopy(),
        )
        test_issue = Issue(
            number=456,
            title="Test Feature",
            labels=["agent:test"],  # Must match config's agent key
            state="open"
        )

        # launch_session only takes issue - gets agent_config internally
        session = orchestrator.launch_session(test_issue)

        # Verify all steps happened
        assert mock_worktree_manager.create.called, "Worktree should be created"
        # Verify window created via plugin manager
        assert len(patch_plugin_manager.plugin.create_session_calls) == 1, "Tmux window should be created"
        # Verify label added via the mock adapter
        assert (456, "in-progress") in mock_repository_host.add_label_calls, "In-progress label should be added"
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
            mock_cfg.max_concurrent_sessions = 2
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
            with patch('issue_orchestrator._tmux_impl.get_manager') as mock_tmux:
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
        mock_orch.config.max_concurrent_sessions = 2
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
        mock_orch.config.max_concurrent_sessions = 2
        mock_orch._shutdown_requested = False

        dashboard = Dashboard(mock_orch)

        await dashboard._handle_resume()

        assert mock_orch.state.paused is False


class TestObserverWiring:
    """Test that observer correctly detects session states."""

    def test_observer_detects_completed_session(self):
        """Verify observer detects when a session has completed."""
        from issue_orchestrator.observation import SessionObserver
        from issue_orchestrator.models import Session, Issue, AgentConfig, SessionStatus
        from datetime import datetime

        config = MagicMock()
        config.get_label_blocked.return_value = "blocked"
        config.get_label_needs_human.return_value = "needs-human"

        # Mock the session runner to report session not exists
        mock_runner = MagicMock()
        mock_runner.session_exists.return_value = False

        # Mock repository host for PR lookup
        mock_repo_host = MagicMock()
        mock_repo_host.get_open_prs_for_branch.return_value = [
            MagicMock(url="https://github.com/test/pull/1")
        ]

        observer = SessionObserver(
            config,
            session_runner=mock_runner,
            repository_host=mock_repo_host,
        )

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

        # check_session is NOT async - it's a regular method
        status = observer.check_session(session)

        assert status == SessionStatus.COMPLETED

    def test_observer_detects_running_session(self):
        """Verify observer detects when a session is still running."""
        from issue_orchestrator.observation import SessionObserver
        from issue_orchestrator.models import Session, Issue, AgentConfig, SessionStatus
        from datetime import datetime

        config = MagicMock()

        # Mock the session runner to report session exists
        mock_runner = MagicMock()
        mock_runner.session_exists.return_value = True

        observer = SessionObserver(config, session_runner=mock_runner)

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

        status = observer.check_session(session)
        assert status == SessionStatus.RUNNING


class TestSmoke:
    """Smoke tests to verify basic functionality works end-to-end."""

    def test_can_import_all_modules(self):
        """Verify all modules can be imported without errors."""
        from issue_orchestrator import cli
        from issue_orchestrator import config
        from issue_orchestrator import dashboard
        from issue_orchestrator import _github_impl as github
        from issue_orchestrator import models
        from issue_orchestrator.observation import observer
        from issue_orchestrator import orchestrator
        from issue_orchestrator.control import scheduler
        from issue_orchestrator import _tmux_impl as tmux
        from issue_orchestrator import _worktree_impl as worktree
        from issue_orchestrator import execution

        # If we get here, all imports succeeded
        assert True

    def test_config_default_values(self):
        """Verify config has sensible defaults."""
        config = Config()
        assert config.max_concurrent_sessions == 3
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
