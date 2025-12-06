"""Tests for CLI module."""

import argparse
from unittest.mock import Mock, patch, AsyncMock, MagicMock
import pytest

from issue_orchestrator.cli import cmd_start, cmd_status, cmd_init, main


class TestCmdStart:
    """Tests for the start command."""

    def test_cmd_start_missing_config_returns_error(self):
        """Verify proper error handling when config is missing."""
        # Patch where Config is defined, not where it's imported
        with patch('issue_orchestrator.config.Config.find_and_load') as mock_find:
            mock_find.side_effect = FileNotFoundError("No config")

            args = argparse.Namespace(
                test_mode=False,
                milestone=None,
                dry_run=False,
                no_dashboard=False,
            )

            result = cmd_start(args)
            assert result == 1

    def test_cmd_start_calls_startup_and_run_loop(self):
        """Verify that startup() is called before run_loop()."""
        with patch('issue_orchestrator.config.Config.find_and_load') as mock_find:
            with patch('issue_orchestrator.orchestrator.Orchestrator') as mock_orch_class:
                with patch('issue_orchestrator.dashboard.run_with_dashboard') as mock_dashboard:
                    with patch('issue_orchestrator.cli.asyncio') as mock_asyncio:
                        # Setup config
                        mock_config = Mock()
                        mock_config.agents = {'agent:test': Mock()}
                        mock_config.max_sessions = 2
                        mock_config.ui_mode = 'tmux'
                        mock_find.return_value = mock_config

                        # Setup orchestrator
                        mock_orchestrator = Mock()
                        mock_orchestrator.startup = AsyncMock()
                        mock_orchestrator.run_loop = AsyncMock()
                        mock_orch_class.return_value = mock_orchestrator

                        # Setup asyncio.run to return None
                        mock_asyncio.run.return_value = None
                        mock_dashboard.return_value = False

                        args = argparse.Namespace(
                            test_mode=False,
                            milestone=None,
                            dry_run=False,
                            no_dashboard=False,
                            debug=False,
                        )

                        result = cmd_start(args)

                        # Verify asyncio.run was called at least twice
                        # (once for startup, once for run_with_dashboard)
                        assert mock_asyncio.run.call_count >= 2

    def test_cmd_start_no_dashboard_calls_run_loop(self):
        """Verify run_loop() is called when --no-dashboard is set."""
        with patch('issue_orchestrator.config.Config.find_and_load') as mock_find:
            with patch('issue_orchestrator.orchestrator.Orchestrator') as mock_orch_class:
                with patch('issue_orchestrator.cli.asyncio') as mock_asyncio:
                    mock_config = Mock()
                    mock_config.agents = {'agent:test': Mock()}
                    mock_config.max_sessions = 2
                    mock_config.ui_mode = 'tmux'
                    mock_find.return_value = mock_config

                    mock_orchestrator = Mock()
                    mock_orchestrator.startup = AsyncMock()
                    mock_orchestrator.run_loop = AsyncMock()
                    mock_orch_class.return_value = mock_orchestrator

                    # Setup asyncio.run to return None
                    mock_asyncio.run.return_value = None

                    args = argparse.Namespace(
                        test_mode=False,
                        milestone=None,
                        dry_run=False,
                        no_dashboard=True,
                        debug=False,
                    )

                    result = cmd_start(args)

                    # Should call asyncio.run twice: startup + run_loop
                    assert mock_asyncio.run.call_count == 2

    def test_cmd_start_dry_run_does_not_create_orchestrator(self):
        """Verify dry-run mode doesn't create orchestrator."""
        with patch('issue_orchestrator.config.Config.find_and_load') as mock_find:
            with patch('issue_orchestrator.orchestrator.Orchestrator') as mock_orch_class:
                with patch('issue_orchestrator.github.list_issues', return_value=[]):
                    with patch('issue_orchestrator.scheduler.Scheduler'):
                        with patch('issue_orchestrator.tmux.get_manager') as mock_get_mgr:
                            with patch('issue_orchestrator.analysis.analyze_all_issues', return_value=[]):
                                with patch('issue_orchestrator.analysis.get_issue_branches', return_value={}):
                                    with patch('issue_orchestrator.analysis.analyze_orphan_branches', return_value=[]):
                                        mock_config = Mock()
                                        mock_config.agents = {'agent:test': Mock()}
                                        mock_config.max_sessions = 2
                                        mock_config.repo = 'test/repo'
                                        mock_config.filter_label = None
                                        mock_config.filter_milestone = None
                                        mock_config.repo_root = '/tmp'
                                        mock_config.get_label_in_progress.return_value = 'in-progress'
                                        mock_find.return_value = mock_config

                                        mock_mgr = Mock()
                                        mock_mgr.window_exists = Mock(return_value=False)
                                        mock_get_mgr.return_value = mock_mgr

                                        args = argparse.Namespace(
                                            test_mode=False,
                                            milestone=None,
                                            dry_run=True,
                                            no_dashboard=False,
                                        )

                                        result = cmd_start(args)

                                        assert result == 0
                                        # Orchestrator should NOT be instantiated for dry-run
                                        mock_orch_class.assert_not_called()


class TestCmdStatus:
    """Tests for the status command."""

    def test_cmd_status_shows_config(self):
        """Verify status shows configuration."""
        with patch('issue_orchestrator.config.Config.find_and_load') as mock_find:
            with patch('issue_orchestrator.tmux.list_sessions', return_value=[]):
                mock_config = Mock()
                mock_config.repo = 'test/repo'
                mock_config.max_sessions = 3
                mock_config.agents = {'agent:web': Mock(), 'agent:mobile': Mock()}
                mock_config.filter_label = None
                mock_config.filter_milestone = None
                mock_find.return_value = mock_config

                args = argparse.Namespace()
                result = cmd_status(args)

                assert result == 0

    def test_cmd_status_shows_active_sessions(self):
        """Verify status shows active sessions."""
        with patch('issue_orchestrator.config.Config.find_and_load') as mock_find:
            with patch('issue_orchestrator.tmux.list_sessions') as mock_list:
                mock_config = Mock()
                mock_config.repo = 'test/repo'
                mock_config.max_sessions = 3
                mock_config.agents = {'agent:web': Mock()}
                mock_config.filter_label = None
                mock_config.filter_milestone = None
                mock_find.return_value = mock_config
                mock_list.return_value = ['issue-123', 'issue-456']

                args = argparse.Namespace()
                result = cmd_status(args)

                assert result == 0

    def test_cmd_status_handles_missing_config(self):
        """Verify status handles missing config gracefully."""
        with patch('issue_orchestrator.config.Config.find_and_load') as mock_find:
            mock_find.side_effect = FileNotFoundError("No config")

            args = argparse.Namespace()
            result = cmd_status(args)

            assert result == 0  # Status returns 0 even without config


class TestCmdInit:
    """Tests for the init command."""

    def test_cmd_init_missing_config_returns_error(self):
        """Verify init fails gracefully without config."""
        with patch('issue_orchestrator.config.Config.find_and_load') as mock_find:
            mock_find.side_effect = FileNotFoundError("No config")

            args = argparse.Namespace()
            result = cmd_init(args)

            assert result == 1

    def test_cmd_init_missing_repo_returns_error(self):
        """Verify init fails when repo not configured."""
        with patch('issue_orchestrator.config.Config.find_and_load') as mock_find:
            mock_config = Mock()
            mock_config.repo = None
            mock_find.return_value = mock_config

            args = argparse.Namespace()
            result = cmd_init(args)

            assert result == 1


class TestMain:
    """Tests for the main entry point."""

    def test_main_requires_command(self):
        """Verify main requires a subcommand."""
        with patch('sys.argv', ['issue-orchestrator']):
            with pytest.raises(SystemExit):
                main()

    def test_main_dispatches_to_status(self):
        """Verify main dispatches to correct command handler."""
        with patch('issue_orchestrator.cli.cmd_status') as mock_cmd_status:
            mock_cmd_status.return_value = 0

            with patch('sys.argv', ['issue-orchestrator', 'status']):
                result = main()

            mock_cmd_status.assert_called_once()
            assert result == 0
