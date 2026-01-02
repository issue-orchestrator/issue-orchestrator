"""Tests for CLI module."""

import argparse
import inspect
from unittest.mock import Mock, patch, AsyncMock, MagicMock
import pytest

from issue_orchestrator.cli import (
    cmd_start, cmd_status, cmd_init, cmd_attach, cmd_switch,
    cmd_dashboard, cmd_output, cmd_pause, cmd_resume, cmd_next,
    cmd_test_reset, main, setup_logging, _run_test_setup, _load_config
)


def _run_and_close(coro):
    if inspect.iscoroutine(coro):
        coro.close()
    return None


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
                debug=False,
            )

            result = cmd_start(args)
            assert result == 1

    def test_cmd_start_calls_startup_and_run_loop(self):
        """Verify that startup() is called before run_loop()."""
        with patch('issue_orchestrator.config.Config.find_and_load') as mock_find:
            with patch('issue_orchestrator.bootstrap.build_orchestrator') as mock_build:
                with patch('issue_orchestrator.dashboard.run_with_dashboard') as mock_dashboard:
                    with patch('issue_orchestrator.cli.asyncio') as mock_asyncio:
                        # Setup config
                        mock_config = Mock()
                        mock_config.agents = {'agent:test': Mock()}
                        mock_config.max_concurrent_sessions = 2
                        mock_config.ui_mode = 'tmux'
                        mock_config.validate.return_value = []  # Pass validation
                        mock_find.return_value = mock_config

                        # Setup orchestrator
                        mock_orchestrator = Mock()
                        mock_orchestrator.startup = AsyncMock()
                        mock_orchestrator.run_loop = AsyncMock()
                        mock_build.return_value = mock_orchestrator

                        # Mock asyncio.run to return None immediately without executing
                        mock_asyncio.run.side_effect = _run_and_close

                        args = argparse.Namespace(
                            test_mode=False,
                            milestone=None,
                            dry_run=False,
                            no_dashboard=False,
                            debug=False,
                        )

                        result = cmd_start(args)

                        # Verify asyncio.run was called once
                        # (startup + run_with_dashboard combined in single event loop)
                        assert mock_asyncio.run.call_count >= 1

    def test_cmd_start_no_dashboard_calls_run_loop(self):
        """Verify run_loop() is called when --no-dashboard is set."""
        with patch('issue_orchestrator.config.Config.find_and_load') as mock_find:
            with patch('issue_orchestrator.bootstrap.build_orchestrator') as mock_build:
                with patch('issue_orchestrator.cli.asyncio') as mock_asyncio:
                    mock_config = Mock()
                    mock_config.agents = {'agent:test': Mock()}
                    mock_config.max_concurrent_sessions = 2
                    mock_config.ui_mode = 'tmux'
                    mock_config.validate.return_value = []  # Pass validation
                    mock_find.return_value = mock_config

                    mock_orchestrator = Mock()
                    mock_orchestrator.startup = AsyncMock()
                    mock_orchestrator.run_loop = AsyncMock()
                    mock_build.return_value = mock_orchestrator

                    # Mock asyncio.run to return None immediately without executing
                    mock_asyncio.run.side_effect = _run_and_close

                    args = argparse.Namespace(
                        test_mode=False,
                        milestone=None,
                        dry_run=False,
                        no_dashboard=True,
                        debug=False,
                    )

                    result = cmd_start(args)

                    # Should call asyncio.run once (startup + run_loop combined in single event loop)
                    assert mock_asyncio.run.call_count == 1

    def test_cmd_start_dry_run_does_not_create_orchestrator(self):
        """Verify dry-run mode doesn't create orchestrator."""
        with patch('issue_orchestrator.config.Config.find_and_load') as mock_find:
            with patch('issue_orchestrator.bootstrap.build_orchestrator') as mock_build:
                with patch('issue_orchestrator.adapters.github.github_adapter.GitHubAdapter.list_issues', return_value=[]):
                    with patch('issue_orchestrator.control.scheduler.Scheduler'):
                        with patch('issue_orchestrator.adapters.terminal._tmux.get_manager') as mock_get_mgr:
                            with patch('issue_orchestrator.infra.analysis.analyze_all_issues', return_value=[]):
                                with patch('issue_orchestrator.execution.git_working_copy.GitWorkingCopy.list_remote_branches', return_value=[]):
                                    with patch('issue_orchestrator.infra.analysis.analyze_orphan_branches', return_value=[]):
                                        mock_config = Mock()
                                        mock_config.agents = {'agent:test': Mock()}
                                        mock_config.max_concurrent_sessions = 2
                                        mock_config.repo = 'test/repo'
                                        mock_config.filter_label = None
                                        mock_config.filter_milestone = None
                                        mock_config.filter_milestones = []
                                        mock_config.get_filter_milestones.return_value = []
                                        mock_config.repo_root = '/tmp'
                                        mock_config.get_label_in_progress.return_value = 'in-progress'
                                        mock_config.github_api_url = 'https://api.github.com'
                                        mock_config.github_http_timeout_seconds = 20.0
                                        mock_config.queue_refresh_seconds = 0
                                        mock_config.validate.return_value = []  # Pass validation
                                        mock_find.return_value = mock_config

                                        mock_mgr = Mock()
                                        mock_mgr.window_exists = Mock(return_value=False)
                                        mock_get_mgr.return_value = mock_mgr

                                        args = argparse.Namespace(
                                            test_mode=False,
                                            milestone=None,
                                            dry_run=True,
                                            no_dashboard=False,
                                            debug=False,
                                        )

                                        result = cmd_start(args)

                                        assert result == 0
                                        # Orchestrator should NOT be instantiated for dry-run
                                        mock_build.assert_not_called()


class TestCmdStatus:
    """Tests for the status command."""

    def test_cmd_status_shows_config(self):
        """Verify status shows configuration."""
        with patch('issue_orchestrator.config.Config.find_and_load') as mock_find:
            with patch('issue_orchestrator.adapters.terminal._tmux.list_sessions', return_value=[]):
                mock_config = Mock()
                mock_config.repo = 'test/repo'
                mock_config.max_concurrent_sessions = 3
                mock_config.agents = {'agent:web': Mock(), 'agent:mobile': Mock()}
                mock_config.filter_label = None
                mock_config.filter_milestone = None
                mock_config.filter_milestones = []
                mock_find.return_value = mock_config

                args = argparse.Namespace()
                result = cmd_status(args)

                assert result == 0

    def test_cmd_status_shows_active_sessions(self):
        """Verify status shows active sessions."""
        with patch('issue_orchestrator.config.Config.find_and_load') as mock_find:
            with patch('issue_orchestrator.adapters.terminal._tmux.list_sessions') as mock_list:
                mock_config = Mock()
                mock_config.repo = 'test/repo'
                mock_config.max_concurrent_sessions = 3
                mock_config.agents = {'agent:web': Mock()}
                mock_config.filter_label = None
                mock_config.filter_milestone = None
                mock_config.filter_milestones = []
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
            with patch('issue_orchestrator.cli._resolve_repo', side_effect=RuntimeError("no repo")):
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

    def test_main_dispatches_to_start(self):
        """Verify main dispatches start command correctly."""
        with patch('issue_orchestrator.cli.cmd_start') as mock_cmd_start:
            mock_cmd_start.return_value = 0

            with patch('sys.argv', ['issue-orchestrator', 'start']):
                result = main()

            mock_cmd_start.assert_called_once()
            assert result == 0

    def test_main_dispatches_to_init(self):
        """Verify main dispatches init command correctly."""
        with patch('issue_orchestrator.cli.cmd_init') as mock_cmd_init:
            mock_cmd_init.return_value = 0

            with patch('sys.argv', ['issue-orchestrator', 'init']):
                result = main()

            mock_cmd_init.assert_called_once()
            assert result == 0

    def test_main_parses_start_with_test_mode(self):
        """Verify main parses --test-mode flag correctly."""
        with patch('issue_orchestrator.cli.cmd_start') as mock_cmd_start:
            mock_cmd_start.return_value = 0

            with patch('sys.argv', ['issue-orchestrator', 'start', '--test-mode']):
                result = main()

            # Verify the test_mode argument was passed
            args = mock_cmd_start.call_args[0][0]
            assert args.test_mode is True

    def test_main_parses_start_with_dry_run(self):
        """Verify main parses --dry-run flag correctly."""
        with patch('issue_orchestrator.cli.cmd_start') as mock_cmd_start:
            mock_cmd_start.return_value = 0

            with patch('sys.argv', ['issue-orchestrator', 'start', '--dry-run']):
                result = main()

            args = mock_cmd_start.call_args[0][0]
            assert args.dry_run is True

    def test_main_parses_start_with_milestone(self):
        """Verify main parses --milestone argument correctly."""
        with patch('issue_orchestrator.cli.cmd_start') as mock_cmd_start:
            mock_cmd_start.return_value = 0

            with patch('sys.argv', ['issue-orchestrator', 'start', '--milestone', 'v1.0']):
                result = main()

            args = mock_cmd_start.call_args[0][0]
            assert args.milestone == 'v1.0'

    def test_main_parses_start_with_ui_mode(self):
        """Verify main parses --ui-mode argument correctly."""
        with patch('issue_orchestrator.cli.cmd_start') as mock_cmd_start:
            mock_cmd_start.return_value = 0

            with patch('sys.argv', ['issue-orchestrator', 'start', '--ui-mode', 'web']):
                result = main()

            args = mock_cmd_start.call_args[0][0]
            assert args.ui_mode == 'web'

    def test_main_parses_start_with_port(self):
        """Verify main parses --port argument correctly."""
        with patch('issue_orchestrator.cli.cmd_start') as mock_cmd_start:
            mock_cmd_start.return_value = 0

            with patch('sys.argv', ['issue-orchestrator', 'start', '--port', '9000']):
                result = main()

            args = mock_cmd_start.call_args[0][0]
            assert args.port == 9000

    def test_main_parses_attach_with_issue_number(self):
        """Verify main parses attach command with issue number."""
        with patch('issue_orchestrator.cli.cmd_attach') as mock_cmd_attach:
            mock_cmd_attach.return_value = 0

            with patch('sys.argv', ['issue-orchestrator', 'attach', '123']):
                result = main()

            args = mock_cmd_attach.call_args[0][0]
            assert args.issue_number == 123

    def test_main_parses_switch_with_issue_number(self):
        """Verify main parses switch command with issue number."""
        with patch('issue_orchestrator.cli.cmd_switch') as mock_cmd_switch:
            mock_cmd_switch.return_value = 0

            with patch('sys.argv', ['issue-orchestrator', 'switch', '456']):
                result = main()

            args = mock_cmd_switch.call_args[0][0]
            assert args.issue_number == 456

    def test_main_parses_output_with_lines(self):
        """Verify main parses output command with --lines."""
        with patch('issue_orchestrator.cli.cmd_output') as mock_cmd_output:
            mock_cmd_output.return_value = 0

            with patch('sys.argv', ['issue-orchestrator', 'output', '789', '--lines', '50']):
                result = main()

            args = mock_cmd_output.call_args[0][0]
            assert args.issue_number == 789
            assert args.lines == 50


class TestCmdAttach:
    """Tests for the attach command."""

    def test_cmd_attach_no_session(self):
        """Verify attach fails when no session exists."""
        with patch('issue_orchestrator.adapters.terminal._tmux.get_manager') as mock_get_mgr:
            mock_mgr = Mock()
            mock_mgr.has_session.return_value = False
            mock_get_mgr.return_value = mock_mgr

            args = argparse.Namespace(issue_number=None)
            result = cmd_attach(args)

            assert result == 1

    def test_cmd_attach_success(self):
        """Verify attach succeeds when session exists."""
        with patch('issue_orchestrator.adapters.terminal._tmux.get_manager') as mock_get_mgr:
            with patch('issue_orchestrator.adapters.terminal._tmux.attach_session') as mock_attach:
                mock_mgr = Mock()
                mock_mgr.has_session.return_value = True
                mock_get_mgr.return_value = mock_mgr

                args = argparse.Namespace(issue_number=None)
                result = cmd_attach(args)

                mock_attach.assert_called_once_with("")
                assert result == 0

    def test_cmd_attach_with_issue_number(self):
        """Verify attach switches to specific issue window."""
        with patch('issue_orchestrator.adapters.terminal._tmux.get_manager') as mock_get_mgr:
            with patch('issue_orchestrator.adapters.terminal._tmux.attach_session') as mock_attach:
                mock_mgr = Mock()
                mock_mgr.has_session.return_value = True
                mock_mgr.select_window.return_value = True
                mock_get_mgr.return_value = mock_mgr

                args = argparse.Namespace(issue_number=123)
                result = cmd_attach(args)

                mock_mgr.select_window.assert_called_once_with(123)
                mock_attach.assert_called_once_with("")


class TestCmdSwitch:
    """Tests for the switch command."""

    def test_cmd_switch_no_session(self):
        """Verify switch fails when no session exists."""
        with patch('issue_orchestrator.adapters.terminal._tmux.get_manager') as mock_get_mgr:
            mock_mgr = Mock()
            mock_mgr.has_session.return_value = False
            mock_get_mgr.return_value = mock_mgr

            args = argparse.Namespace(issue_number=123)
            result = cmd_switch(args)

            assert result == 1

    def test_cmd_switch_window_not_found(self):
        """Verify switch fails when window doesn't exist."""
        with patch('issue_orchestrator.adapters.terminal._tmux.get_manager') as mock_get_mgr:
            mock_mgr = Mock()
            mock_mgr.has_session.return_value = True
            mock_mgr.select_window.return_value = False
            mock_get_mgr.return_value = mock_mgr

            args = argparse.Namespace(issue_number=123)
            result = cmd_switch(args)

            assert result == 1

    def test_cmd_switch_success(self):
        """Verify switch succeeds when window exists."""
        with patch('issue_orchestrator.adapters.terminal._tmux.get_manager') as mock_get_mgr:
            mock_mgr = Mock()
            mock_mgr.has_session.return_value = True
            mock_mgr.select_window.return_value = True
            mock_get_mgr.return_value = mock_mgr

            args = argparse.Namespace(issue_number=123)
            result = cmd_switch(args)

            mock_mgr.select_window.assert_called_once_with(123)
            assert result == 0


class TestCmdDashboard:
    """Tests for the dashboard command."""

    def test_cmd_dashboard_no_session(self):
        """Verify dashboard fails when no session exists."""
        with patch('issue_orchestrator.adapters.terminal._tmux.get_manager') as mock_get_mgr:
            mock_mgr = Mock()
            mock_mgr.has_session.return_value = False
            mock_get_mgr.return_value = mock_mgr

            args = argparse.Namespace()
            result = cmd_dashboard(args)

            assert result == 1

    def test_cmd_dashboard_not_found(self):
        """Verify dashboard fails when window doesn't exist."""
        with patch('issue_orchestrator.adapters.terminal._tmux.get_manager') as mock_get_mgr:
            mock_mgr = Mock()
            mock_mgr.has_session.return_value = True
            mock_mgr.select_dashboard.return_value = False
            mock_get_mgr.return_value = mock_mgr

            args = argparse.Namespace()
            result = cmd_dashboard(args)

            assert result == 1

    def test_cmd_dashboard_success(self):
        """Verify dashboard succeeds when window exists."""
        with patch('issue_orchestrator.adapters.terminal._tmux.get_manager') as mock_get_mgr:
            mock_mgr = Mock()
            mock_mgr.has_session.return_value = True
            mock_mgr.select_dashboard.return_value = True
            mock_get_mgr.return_value = mock_mgr

            args = argparse.Namespace()
            result = cmd_dashboard(args)

            mock_mgr.select_dashboard.assert_called_once()
            assert result == 0


class TestCmdOutput:
    """Tests for the output command."""

    def test_cmd_output_window_not_found(self):
        """Verify output fails when window doesn't exist."""
        with patch('issue_orchestrator.adapters.terminal._tmux.get_manager') as mock_get_mgr:
            mock_mgr = Mock()
            mock_mgr.capture_pane_output.return_value = None
            mock_get_mgr.return_value = mock_mgr

            args = argparse.Namespace(issue_number=123, lines=20)
            result = cmd_output(args)

            assert result == 1

    def test_cmd_output_success(self):
        """Verify output displays pane content successfully."""
        with patch('issue_orchestrator.adapters.terminal._tmux.get_manager') as mock_get_mgr:
            mock_mgr = Mock()
            mock_mgr.capture_pane_output.return_value = "Sample output\nLine 2"
            mock_get_mgr.return_value = mock_mgr

            args = argparse.Namespace(issue_number=123, lines=20)
            result = cmd_output(args)

            mock_mgr.capture_pane_output.assert_called_once_with(123, lines=20)
            assert result == 0

    def test_cmd_output_custom_lines(self):
        """Verify output respects custom line count."""
        with patch('issue_orchestrator.adapters.terminal._tmux.get_manager') as mock_get_mgr:
            mock_mgr = Mock()
            mock_mgr.capture_pane_output.return_value = "Output"
            mock_get_mgr.return_value = mock_mgr

            args = argparse.Namespace(issue_number=456, lines=50)
            result = cmd_output(args)

            mock_mgr.capture_pane_output.assert_called_once_with(456, lines=50)
            assert result == 0


class TestCmdPauseResume:
    """Tests for pause and resume commands."""

    def test_cmd_pause(self):
        """Verify pause command returns successfully."""
        args = argparse.Namespace()
        result = cmd_pause(args)
        assert result == 0

    def test_cmd_resume(self):
        """Verify resume command returns successfully."""
        args = argparse.Namespace()
        result = cmd_resume(args)
        assert result == 0


class TestCmdNext:
    """Tests for the next command."""

    def test_cmd_next(self):
        """Verify next command returns successfully."""
        args = argparse.Namespace(issue_number=789)
        result = cmd_next(args)
        assert result == 0


class TestCmdTestReset:
    """Tests for the test-reset command."""

    def test_cmd_test_reset_scripts_found(self):
        """Verify test-reset executes teardown and setup scripts."""
        with patch('issue_orchestrator.cli.Path') as mock_path_class:
            with patch('subprocess.run') as mock_run:
                # Mock script paths
                mock_scripts_dir = Mock()
                mock_scripts_dir.exists.return_value = True
                mock_teardown = Mock()
                mock_teardown.exists.return_value = True
                mock_setup = Mock()
                mock_setup.exists.return_value = True

                # Setup path resolution
                mock_path_class.return_value.parent.parent.parent = Mock()
                mock_path_class.return_value.parent.parent.parent.__truediv__ = Mock(return_value=mock_scripts_dir)
                mock_scripts_dir.__truediv__ = Mock(side_effect=[mock_teardown, mock_setup])

                # Mock subprocess to succeed
                mock_run.return_value = Mock(returncode=0)

                args = argparse.Namespace()
                result = cmd_test_reset(args)

                assert result == 0
                # Should be called twice (teardown + setup)
                assert mock_run.call_count == 2

    def test_cmd_test_reset_scripts_not_found(self):
        """Verify test-reset handles missing scripts directory."""
        from pathlib import Path
        with patch.object(Path, 'exists', return_value=False):
            args = argparse.Namespace()
            result = cmd_test_reset(args)

            assert result == 1


class TestSetupLogging:
    """Tests for setup_logging function."""

    def setup_method(self):
        """Reset logging config before each test."""
        from issue_orchestrator.infra.logging_config import reset_logging
        reset_logging()

    def test_setup_logging_default(self):
        """Verify logging setup with default settings."""
        with patch('logging.getLogger') as mock_get_logger:
            with patch('logging.FileHandler') as mock_file_handler:
                with patch('logging.info'):
                    mock_logger = Mock()
                    mock_logger.handlers = []
                    mock_get_logger.return_value = mock_logger

                    mock_handler = Mock()
                    mock_file_handler.return_value = mock_handler

                    setup_logging(level="INFO")

                    mock_logger.setLevel.assert_called_once()
                    mock_logger.addHandler.assert_called_once_with(mock_handler)

    def test_setup_logging_debug_mode(self):
        """Verify logging setup with debug mode enabled."""
        with patch('logging.getLogger') as mock_get_logger:
            with patch('logging.FileHandler') as mock_file_handler:
                with patch('logging.info'):
                    mock_logger = Mock()
                    mock_logger.handlers = []
                    mock_get_logger.return_value = mock_logger

                    mock_handler = Mock()
                    mock_file_handler.return_value = mock_handler

                    setup_logging(level="DEBUG")

                    # Verify debug level was set
                    import logging
                    mock_logger.setLevel.assert_called_once()

    def test_setup_logging_removes_existing_handlers(self):
        """Verify logging removes existing handlers before setup."""
        with patch('logging.getLogger') as mock_get_logger:
            with patch('logging.FileHandler') as mock_file_handler:
                with patch('logging.info'):
                    # Mock existing handlers
                    existing_handler = Mock()
                    mock_logger = Mock()
                    mock_logger.handlers = [existing_handler]
                    mock_get_logger.return_value = mock_logger

                    mock_handler = Mock()
                    mock_file_handler.return_value = mock_handler

                    setup_logging(level="INFO")

                    # Should remove existing handler
                    mock_logger.removeHandler.assert_called_once_with(existing_handler)


def _mock_issue(number: int) -> Mock:
    """Create a mock Issue object with the given number."""
    issue = Mock()
    issue.number = number
    return issue


class TestRunTestSetup:
    """Tests for _run_test_setup function."""

    def test_run_test_setup_success(self):
        """Verify test setup creates issues successfully."""
        config = Mock()
        config.repo = "owner/repo"
        config.github_token = None
        config.github_token_env = None
        config.github_api_url = "https://api.github.com"
        config.github_http_timeout_seconds = 20.0
        with patch('issue_orchestrator.cli._github_adapter_for_config') as mock_adapter_factory:
            adapter = Mock()
            adapter.list_issues.return_value = [_mock_issue(1), _mock_issue(2)]
            adapter.create_issue.return_value = 123
            mock_adapter_factory.return_value = adapter

            result = _run_test_setup(config)

            assert result is True
            assert adapter.list_issues.called
            assert adapter.create_issue.called

    def test_run_test_setup_creates_labels(self):
        """Verify test setup creates required labels."""
        config = Mock()
        config.repo = "owner/repo"
        config.github_token = None
        config.github_token_env = None
        config.github_api_url = "https://api.github.com"
        config.github_http_timeout_seconds = 20.0
        with patch('issue_orchestrator.cli._github_adapter_for_config') as mock_adapter_factory:
            adapter = Mock()
            adapter.list_issues.return_value = []
            mock_adapter_factory.return_value = adapter

            result = _run_test_setup(config)

            assert result is True
            assert adapter.create_label.called


class TestCmdStartAdvanced:
    """Advanced tests for cmd_start covering various scenarios."""

    def test_cmd_start_test_mode_without_repo(self):
        """Verify test mode fails when repo not configured."""
        with patch('issue_orchestrator.config.Config.find_and_load') as mock_find:
            mock_config = Mock()
            mock_config.repo = None
            mock_config.agents = {'agent:test': Mock()}
            mock_config.max_concurrent_sessions = 2
            mock_find.return_value = mock_config

            args = argparse.Namespace(
                test_mode=True,
                milestone=None,
                dry_run=False,
                no_dashboard=False,
                debug=False,
            )

            result = cmd_start(args)
            assert result == 1

    def test_cmd_start_test_mode_success(self):
        """Verify test mode sets filter_label to test-data."""
        with patch('issue_orchestrator.config.Config.find_and_load') as mock_find:
            with patch('issue_orchestrator.cli._run_test_setup') as mock_test_setup:
                with patch('issue_orchestrator.bootstrap.build_orchestrator') as mock_build:
                    with patch('issue_orchestrator.dashboard.run_with_dashboard') as mock_dashboard:
                        with patch('issue_orchestrator.cli.asyncio') as mock_asyncio:
                            mock_config = Mock()
                            mock_config.repo = 'test/repo'
                            mock_config.agents = {'agent:test': Mock()}
                            mock_config.max_concurrent_sessions = 2
                            mock_config.ui_mode = 'tmux'
                            mock_config.validate.return_value = []  # Pass validation
                            mock_find.return_value = mock_config

                            mock_test_setup.return_value = True
                            mock_asyncio.run.side_effect = _run_and_close
                            mock_build.return_value = Mock()

                            args = argparse.Namespace(
                                test_mode=True,
                                milestone=None,
                                dry_run=False,
                                no_dashboard=False,
                                debug=False,
                            )

                            result = cmd_start(args)

                            # Verify test setup was called
                            mock_test_setup.assert_called_once_with(mock_config)
                            # Verify filter_label was set
                            assert mock_config.filter_label == 'test-data'

    def test_cmd_start_milestone_override(self):
        """Verify milestone argument overrides config."""
        with patch('issue_orchestrator.config.Config.find_and_load') as mock_find:
            with patch('issue_orchestrator.bootstrap.build_orchestrator') as mock_build:
                with patch('issue_orchestrator.dashboard.run_with_dashboard') as mock_dashboard:
                    with patch('issue_orchestrator.cli.asyncio') as mock_asyncio:
                        mock_config = Mock()
                        mock_config.agents = {'agent:test': Mock()}
                        mock_config.max_concurrent_sessions = 2
                        mock_config.ui_mode = 'tmux'
                        mock_config.filter_milestone = None
                        mock_config.validate.return_value = []  # Pass validation
                        mock_find.return_value = mock_config

                        mock_asyncio.run.side_effect = _run_and_close
                        mock_build.return_value = Mock()

                        args = argparse.Namespace(
                            test_mode=False,
                            milestone='v2.0',
                            milestones=None,
                            dry_run=False,
                            no_dashboard=False,
                            debug=False,
                        )

                        result = cmd_start(args)

                        assert mock_config.filter_milestone == 'v2.0'

    def test_cmd_start_milestones_override(self):
        """Verify milestones argument overrides config."""
        with patch('issue_orchestrator.config.Config.find_and_load') as mock_find:
            with patch('issue_orchestrator.bootstrap.build_orchestrator') as mock_build:
                with patch('issue_orchestrator.dashboard.run_with_dashboard') as mock_dashboard:
                    with patch('issue_orchestrator.cli.asyncio') as mock_asyncio:
                        mock_config = Mock()
                        mock_config.agents = {'agent:test': Mock()}
                        mock_config.max_concurrent_sessions = 2
                        mock_config.ui_mode = 'tmux'
                        mock_config.filter_milestone = None
                        mock_config.filter_milestones = []
                        mock_config.validate.return_value = []  # Pass validation
                        mock_find.return_value = mock_config

                        mock_asyncio.run.side_effect = _run_and_close
                        mock_build.return_value = Mock()

                        args = argparse.Namespace(
                            test_mode=False,
                            milestone=None,
                            milestones='M1,M2',
                            dry_run=False,
                            no_dashboard=False,
                            debug=False,
                        )

                        cmd_start(args)

                        assert mock_config.filter_milestones == ['M1', 'M2']
                        assert mock_config.filter_milestone is None

    def test_cmd_start_ui_mode_override(self):
        """Verify ui_mode argument overrides config."""
        with patch('issue_orchestrator.config.Config.find_and_load') as mock_find:
            with patch('issue_orchestrator.bootstrap.build_orchestrator') as mock_build:
                with patch('issue_orchestrator.dashboard.run_with_dashboard') as mock_dashboard:
                    with patch('issue_orchestrator.cli.asyncio') as mock_asyncio:
                        with patch('issue_orchestrator.adapters.terminal._iterm2.is_running_in_iterm2') as mock_is_iterm2:
                            mock_config = Mock()
                            mock_config.agents = {'agent:test': Mock()}
                            mock_config.max_concurrent_sessions = 2
                            mock_config.ui_mode = 'tmux'
                            mock_config.validate.return_value = []  # Pass validation
                            mock_find.return_value = mock_config

                            mock_asyncio.run.side_effect = _run_and_close
                            mock_build.return_value = Mock()
                            # Simulate running in iTerm2 to avoid launching it
                            mock_is_iterm2.return_value = True

                            args = argparse.Namespace(
                                test_mode=False,
                                milestone=None,
                                dry_run=False,
                                no_dashboard=False,
                                debug=False,
                                ui_mode='iterm2',
                            )

                            result = cmd_start(args)

                            assert mock_config.ui_mode == 'iterm2'

    def test_cmd_start_queue_refresh_override(self):
        """Verify queue_refresh argument overrides config."""
        with patch('issue_orchestrator.config.Config.find_and_load') as mock_find:
            with patch('issue_orchestrator.bootstrap.build_orchestrator') as mock_build:
                with patch('issue_orchestrator.dashboard.run_with_dashboard') as mock_dashboard:
                    with patch('issue_orchestrator.cli.asyncio') as mock_asyncio:
                        mock_config = Mock()
                        mock_config.agents = {'agent:test': Mock()}
                        mock_config.max_concurrent_sessions = 2
                        mock_config.ui_mode = 'tmux'
                        mock_config.queue_refresh_seconds = 600
                        mock_config.validate.return_value = []  # Pass validation
                        mock_find.return_value = mock_config

                        mock_asyncio.run.side_effect = _run_and_close
                        mock_build.return_value = Mock()

                        args = argparse.Namespace(
                            test_mode=False,
                            milestone=None,
                            dry_run=False,
                            no_dashboard=False,
                            debug=False,
                            queue_refresh=300,
                        )

                        result = cmd_start(args)

                        assert mock_config.queue_refresh_seconds == 300

    def test_cmd_start_max_issues_override(self):
        """Verify max_issues argument overrides config."""
        with patch('issue_orchestrator.config.Config.find_and_load') as mock_find:
            with patch('issue_orchestrator.bootstrap.build_orchestrator') as mock_build:
                with patch('issue_orchestrator.dashboard.run_with_dashboard') as mock_dashboard:
                    with patch('issue_orchestrator.cli.asyncio') as mock_asyncio:
                        mock_config = Mock()
                        mock_config.agents = {'agent:test': Mock()}
                        mock_config.max_concurrent_sessions = 2
                        mock_config.ui_mode = 'tmux'
                        mock_config.max_issues_to_start = 0
                        mock_config.validate.return_value = []  # Pass validation
                        mock_find.return_value = mock_config

                        mock_asyncio.run.side_effect = _run_and_close
                        mock_build.return_value = Mock()

                        args = argparse.Namespace(
                            test_mode=False,
                            milestone=None,
                            dry_run=False,
                            no_dashboard=False,
                            debug=False,
                            max_issues=5,
                        )

                        result = cmd_start(args)

                        assert mock_config.max_issues_to_start == 5

    def test_cmd_start_web_mode(self):
        """Verify web mode launches web dashboard."""
        with patch('issue_orchestrator.config.Config.find_and_load') as mock_find:
            with patch('issue_orchestrator.bootstrap.build_orchestrator') as mock_build:
                with patch('issue_orchestrator.web.run_with_web_dashboard') as mock_web:
                    with patch('issue_orchestrator.cli.asyncio') as mock_asyncio:
                        mock_config = Mock()
                        mock_config.agents = {'agent:test': Mock()}
                        mock_config.max_concurrent_sessions = 2
                        mock_config.ui_mode = 'web'
                        mock_config.web_port = 8080
                        mock_config.validate.return_value = []  # Pass validation
                        mock_find.return_value = mock_config

                        mock_asyncio.run.side_effect = _run_and_close
                        mock_build.return_value = Mock()

                        args = argparse.Namespace(
                            test_mode=False,
                            milestone=None,
                            dry_run=False,
                            no_dashboard=False,
                            debug=False,
                            port=8080,
                        )

                        result = cmd_start(args)

                        # Verify web dashboard was called (startup now runs in background)
                        assert mock_asyncio.run.call_count >= 1

    def test_cmd_start_web_mode_custom_port(self):
        """Verify web mode respects custom port."""
        with patch('issue_orchestrator.config.Config.find_and_load') as mock_find:
            with patch('issue_orchestrator.bootstrap.build_orchestrator') as mock_build:
                with patch('issue_orchestrator.web.run_with_web_dashboard') as mock_web:
                    with patch('issue_orchestrator.cli.asyncio') as mock_asyncio:
                        mock_config = Mock()
                        mock_config.agents = {'agent:test': Mock()}
                        mock_config.max_concurrent_sessions = 2
                        mock_config.ui_mode = 'web'
                        mock_config.web_port = 8080
                        mock_config.validate.return_value = []  # Pass validation
                        mock_find.return_value = mock_config

                        mock_asyncio.run.side_effect = _run_and_close
                        mock_build.return_value = Mock()

                        args = argparse.Namespace(
                            test_mode=False,
                            milestone=None,
                            dry_run=False,
                            no_dashboard=False,
                            debug=False,
                            port=9000,
                        )

                        result = cmd_start(args)

                        # Should use custom port (startup now runs in background)
                        assert mock_asyncio.run.call_count >= 1

    def test_cmd_start_keyboard_interrupt(self):
        """Verify keyboard interrupt is handled gracefully."""
        with patch('issue_orchestrator.config.Config.find_and_load') as mock_find:
            with patch('issue_orchestrator.bootstrap.build_orchestrator') as mock_build:
                with patch('issue_orchestrator.dashboard.run_with_dashboard') as mock_dashboard:
                    with patch('issue_orchestrator.cli.asyncio') as mock_asyncio:
                        mock_config = Mock()
                        mock_config.agents = {'agent:test': Mock()}
                        mock_config.max_concurrent_sessions = 2
                        mock_config.ui_mode = 'tmux'
                        mock_config.validate.return_value = []  # Pass validation
                        mock_find.return_value = mock_config

                        mock_build.return_value = Mock()
                        # First call succeeds (startup), second raises KeyboardInterrupt
                        call_count = {"count": 0}

                        def _run_with_interrupt(coro):
                            call_count["count"] += 1
                            if call_count["count"] == 2:
                                raise KeyboardInterrupt()
                            return _run_and_close(coro)

                        mock_asyncio.run.side_effect = _run_with_interrupt

                        args = argparse.Namespace(
                            test_mode=False,
                            milestone=None,
                            dry_run=False,
                            no_dashboard=False,
                            debug=False,
                        )

                        result = cmd_start(args)

                        # Should return 0 despite interrupt
                        assert result == 0

    def test_cmd_start_unexpected_error(self):
        """Verify unexpected errors during config load are handled."""
        with patch('issue_orchestrator.config.Config.find_and_load') as mock_find:
            mock_find.side_effect = ValueError("Unexpected error")

            args = argparse.Namespace(
                test_mode=False,
                milestone=None,
                dry_run=False,
                no_dashboard=False,
                debug=False,
            )

            result = cmd_start(args)
            assert result == 1


class TestCmdInitAdvanced:
    """Advanced tests for cmd_init."""

    def test_cmd_init_creates_all_labels(self):
        """Verify init creates all required labels."""
        with patch('issue_orchestrator.config.Config.find_and_load') as mock_find:
            with patch('issue_orchestrator.cli._github_adapter_for_config') as mock_client_factory:
                mock_config = Mock()
                mock_config.repo = 'test/repo'
                mock_config.agents = {'agent:backend': Mock(), 'agent:frontend': Mock()}
                mock_config.get_label_in_progress.return_value = 'in-progress'
                mock_config.get_label_blocked.return_value = 'blocked'
                mock_config.get_label_needs_human.return_value = 'needs-human'
                mock_config.github_api_url = 'https://api.github.com'
                mock_config.github_http_timeout_seconds = 20.0
                mock_find.return_value = mock_config

                mock_client = Mock()
                mock_client.list_labels.return_value = []
                mock_client_factory.return_value = mock_client

                args = argparse.Namespace()
                result = cmd_init(args)

                assert result == 0
                # Should create: in-progress, blocked, needs-human, 3 priority labels, 2 agent labels
                assert mock_client.create_label.call_count >= 8

    def test_cmd_init_handles_failures(self):
        """Verify init reports failures correctly."""
        with patch('issue_orchestrator.config.Config.find_and_load') as mock_find:
            with patch('issue_orchestrator.cli._github_adapter_for_config') as mock_client_factory:
                mock_config = Mock()
                mock_config.repo = 'test/repo'
                mock_config.agents = {'agent:test': Mock()}
                mock_config.get_label_in_progress.return_value = 'in-progress'
                mock_config.get_label_blocked.return_value = 'blocked'
                mock_config.get_label_needs_human.return_value = 'needs-human'
                mock_config.github_api_url = 'https://api.github.com'
                mock_config.github_http_timeout_seconds = 20.0
                mock_find.return_value = mock_config

                mock_client = Mock()
                mock_client.list_labels.return_value = []
                mock_client.create_label.side_effect = Exception("Error")
                mock_client_factory.return_value = mock_client

                args = argparse.Namespace()
                result = cmd_init(args)

                assert result == 1


class TestLoadConfig:
    """Tests for the _load_config helper function."""

    def test_load_config_without_explicit_path(self):
        """Verify _load_config calls find_and_load when no --config provided."""
        with patch('issue_orchestrator.config.Config.find_and_load') as mock_find:
            mock_config = Mock()
            mock_find.return_value = mock_config

            args = argparse.Namespace(config=None)
            result = _load_config(args)

            mock_find.assert_called_once()
            assert result == mock_config

    def test_load_config_with_explicit_path(self, tmp_path):
        """Verify _load_config calls Config.load when --config provided."""
        # Create a test config file
        config_file = tmp_path / "custom-config.yaml"
        config_file.write_text("""
agents:
  agent:test:
    prompt: /tmp/prompt.txt
    worktree_base: /tmp
""")

        with patch('issue_orchestrator.config.Config.load') as mock_load:
            mock_config = Mock()
            mock_config.repo_root_from_yaml = False
            mock_load.return_value = mock_config

            args = argparse.Namespace(config=str(config_file))
            result = _load_config(args)

            mock_load.assert_called_once()
            # Verify it was called with a Path object
            call_args = mock_load.call_args[0][0]
            assert str(call_args) == str(config_file)
            # Verify repo_root was set to config file's parent
            assert mock_config.repo_root == config_file.parent.resolve()

    def test_load_config_explicit_path_not_found(self, tmp_path):
        """Verify _load_config raises FileNotFoundError for missing config."""
        nonexistent = tmp_path / "does-not-exist.yaml"

        args = argparse.Namespace(config=str(nonexistent))

        with pytest.raises(FileNotFoundError):
            _load_config(args)

    def test_load_config_no_config_attribute(self):
        """Verify _load_config uses find_and_load when args lacks config attr."""
        with patch('issue_orchestrator.config.Config.find_and_load') as mock_find:
            mock_config = Mock()
            mock_find.return_value = mock_config

            args = argparse.Namespace()  # No config attribute
            result = _load_config(args)

            mock_find.assert_called_once()
            assert result == mock_config


class TestConfigCliArgument:
    """Tests for --config CLI argument parsing."""

    def test_main_parses_config_argument(self):
        """Verify main parses --config argument correctly."""
        with patch('issue_orchestrator.cli.cmd_start') as mock_cmd_start:
            mock_cmd_start.return_value = 0

            with patch('sys.argv', ['issue-orchestrator', '--config', '/path/to/config.yaml', 'start']):
                result = main()

            args = mock_cmd_start.call_args[0][0]
            assert args.config == '/path/to/config.yaml'

    def test_main_parses_config_short_form(self):
        """Verify main parses -c short form correctly."""
        with patch('issue_orchestrator.cli.cmd_start') as mock_cmd_start:
            mock_cmd_start.return_value = 0

            with patch('sys.argv', ['issue-orchestrator', '-c', '/custom/path.yaml', 'start']):
                result = main()

            args = mock_cmd_start.call_args[0][0]
            assert args.config == '/custom/path.yaml'

    def test_main_config_default_is_none(self):
        """Verify --config defaults to None when not provided."""
        with patch('issue_orchestrator.cli.cmd_start') as mock_cmd_start:
            mock_cmd_start.return_value = 0

            with patch('sys.argv', ['issue-orchestrator', 'start']):
                result = main()

            args = mock_cmd_start.call_args[0][0]
            assert args.config is None

    def test_config_argument_with_status_command(self):
        """Verify --config works with status command."""
        with patch('issue_orchestrator.cli.cmd_status') as mock_cmd_status:
            mock_cmd_status.return_value = 0

            with patch('sys.argv', ['issue-orchestrator', '--config', '/path/config.yaml', 'status']):
                result = main()

            args = mock_cmd_status.call_args[0][0]
            assert args.config == '/path/config.yaml'

    def test_config_argument_with_init_command(self):
        """Verify --config works with init command."""
        with patch('issue_orchestrator.cli.cmd_init') as mock_cmd_init:
            mock_cmd_init.return_value = 0

            with patch('sys.argv', ['issue-orchestrator', '--config', '/path/config.yaml', 'init']):
                result = main()

            args = mock_cmd_init.call_args[0][0]
            assert args.config == '/path/config.yaml'

    def test_cmd_start_uses_explicit_config(self, tmp_path):
        """Verify cmd_start uses explicit config when --config provided."""
        # Create a test config file
        config_file = tmp_path / "test-config.yaml"
        config_file.write_text("""
repo: test/explicit-repo
agents:
  agent:test:
    prompt: /tmp/prompt.txt
    worktree_base: /tmp
""")

        with patch('issue_orchestrator.config.Config.load') as mock_load:
            with patch('issue_orchestrator.config.Config.find_and_load') as mock_find:
                mock_config = Mock()
                mock_config.agents = {'agent:test': Mock()}
                mock_config.max_concurrent_sessions = 2
                mock_config.ui_mode = 'tmux'
                mock_config.repo = 'test/explicit-repo'
                mock_load.return_value = mock_config

                args = argparse.Namespace(
                    config=str(config_file),
                    test_mode=False,
                    milestone=None,
                    dry_run=False,
                    no_dashboard=True,
                    debug=False,
                )

                with patch('issue_orchestrator.orchestrator.Orchestrator'):
                    with patch('issue_orchestrator.cli.asyncio') as mock_asyncio:
                        mock_asyncio.run.side_effect = _run_and_close
                        result = cmd_start(args)

                # Config.load should be called, not find_and_load
                mock_load.assert_called_once()
                mock_find.assert_not_called()
