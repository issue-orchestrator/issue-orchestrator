"""Tests for session_failure_diagnosis module.

Tests the public API only:
- SessionFailureDiagnosis dataclass and to_dict()
- create_session_failure_diagnosis() function

Per tests/CLAUDE.md: "Tests should verify WHAT the code does, not HOW it does it."
Private helper functions are implementation details tested through the public API.
"""

import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.infra.session_failure_diagnosis import (
    SessionFailureDiagnosis,
    create_session_failure_diagnosis,
)


class TestSessionFailureDiagnosis:
    """Tests for SessionFailureDiagnosis dataclass."""

    def test_initialization_with_defaults(self):
        """Test creating a diagnosis with default values."""
        diagnosis = SessionFailureDiagnosis(
            issue_number=123,
            ai_system="claude-code",
            permission_mode="default",
            worktree_path="/path/to/worktree",
            log_path="/path/to/log",
            log_exists=True,
            log_context="some context",
            history_status="completed",
            history_reason="success",
        )
        assert diagnosis.issue_number == 123
        assert diagnosis.ai_system == "claude-code"
        assert diagnosis.warnings == []
        assert diagnosis.suggestions == []

    def test_initialization_with_warnings_and_suggestions(self):
        """Test creating a diagnosis with warnings and suggestions."""
        warnings = ["warning1", "warning2"]
        suggestions = ["suggestion1"]
        diagnosis = SessionFailureDiagnosis(
            issue_number=123,
            ai_system="claude-code",
            permission_mode="default",
            worktree_path="/path/to/worktree",
            log_path="/path/to/log",
            log_exists=True,
            log_context="some context",
            history_status="completed",
            history_reason="success",
            warnings=warnings,
            suggestions=suggestions,
        )
        assert diagnosis.warnings == warnings
        assert diagnosis.suggestions == suggestions

    def test_to_dict_serialization(self):
        """Test converting diagnosis to dict for JSON serialization."""
        diagnosis = SessionFailureDiagnosis(
            issue_number=456,
            ai_system="anthropic-claude",
            permission_mode="bypassPermissions",
            worktree_path="/path/to/wt",
            log_path="/path/to/log.txt",
            log_exists=True,
            log_context="error context",
            history_status="blocked",
            history_reason="permissions",
            warnings=["warn1"],
            suggestions=["suggest1"],
        )
        result = diagnosis.to_dict()

        assert result["issue_number"] == 456
        assert result["ai_system"] == "anthropic-claude"
        assert result["permission_mode"] == "bypassPermissions"
        assert result["worktree_path"] == "/path/to/wt"
        assert result["log_path"] == "/path/to/log.txt"
        assert result["log_exists"] is True
        assert result["log_context"] == "error context"
        assert result["history_status"] == "blocked"
        assert result["history_reason"] == "permissions"
        assert result["warnings"] == ["warn1"]
        assert result["suggestions"] == ["suggest1"]

    def test_to_dict_with_none_values(self):
        """Test serializing diagnosis with None values."""
        diagnosis = SessionFailureDiagnosis(
            issue_number=789,
            ai_system="claude-code",
            permission_mode="default",
            worktree_path=None,
            log_path=None,
            log_exists=False,
            log_context=None,
            history_status=None,
            history_reason=None,
        )
        result = diagnosis.to_dict()

        assert result["worktree_path"] is None
        assert result["log_path"] is None
        assert result["log_context"] is None
        assert result["history_status"] is None
        assert result["history_reason"] is None


class TestCreateSessionFailureDiagnosis:
    """Tests for create_session_failure_diagnosis main function.

    These tests verify behavior through the public API by setting up
    appropriate inputs and verifying observable outputs.
    """

    # =========================================================================
    # Active Session Tests
    # =========================================================================

    @patch("issue_orchestrator.adapters.session_log.registry.get_log_provider")
    def test_finds_worktree_from_active_session(self, mock_get_provider):
        """When issue is in active sessions, uses that worktree."""
        mock_get_provider.return_value = None

        agent_config = Mock(permission_mode="bypassPermissions", command="claude-code")
        active_session = Mock(
            issue=Mock(number=123),
            worktree_path="/path/to/wt",
            agent_config=agent_config,
        )
        config = Mock()
        config.repo_root = Path("/repo")
        config.repo = "org/repo"
        config.worktree_base = None
        config.agents = {}

        diagnosis = create_session_failure_diagnosis(
            issue_number=123,
            session_history=[],
            active_sessions=[active_session],
            config=config,
            agents={},
        )

        assert diagnosis.worktree_path == "/path/to/wt"

    @patch("issue_orchestrator.adapters.session_log.registry.get_log_provider")
    def test_uses_first_matching_active_session(self, mock_get_provider):
        """When multiple active sessions match, uses the first one."""
        mock_get_provider.return_value = None

        config1 = Mock(effective_permission_mode="mode1", command="cmd1")
        config2 = Mock(effective_permission_mode="mode2", command="cmd2")
        session1 = Mock(issue=Mock(number=123), worktree_path="/path1", agent_config=config1)
        session2 = Mock(issue=Mock(number=123), worktree_path="/path2", agent_config=config2)

        config = Mock()
        config.repo_root = Path("/repo")
        config.repo = "org/repo"
        config.worktree_base = None
        config.agents = {}

        diagnosis = create_session_failure_diagnosis(
            issue_number=123,
            session_history=[],
            active_sessions=[session1, session2],
            config=config,
            agents={},
        )

        assert diagnosis.worktree_path == "/path1"
        assert diagnosis.permission_mode == "mode1"

    # =========================================================================
    # Worktree Discovery Tests (via filesystem)
    # =========================================================================

    @patch("issue_orchestrator.adapters.session_log.registry.get_log_provider")
    def test_finds_worktree_by_direct_name_match(self, mock_get_provider):
        """Finds worktree when directory name is repo-{issue_number}."""
        mock_get_provider.return_value = None

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            worktree = base / "repo-123"
            worktree.mkdir()

            config = Mock()
            config.repo_root = base / "repo"
            config.repo_root.mkdir()
            config.repo = "org/repo"
            config.worktree_base = str(base)
            config.agents = {}

            diagnosis = create_session_failure_diagnosis(
                issue_number=123,
                session_history=[],
                active_sessions=[],
                config=config,
                agents={},
            )

            assert diagnosis.worktree_path == str(worktree.resolve())

    @patch("issue_orchestrator.adapters.session_log.registry.get_log_provider")
    def test_finds_worktree_by_session_directory(self, mock_get_provider):
        """Finds worktree via .issue-orchestrator/sessions/issue-{N} pattern."""
        mock_get_provider.return_value = None

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            worktree = base / "repo-999"  # Different issue number in name
            worktree.mkdir()
            sessions_dir = worktree / ".issue-orchestrator" / "sessions"
            sessions_dir.mkdir(parents=True)
            (sessions_dir / "issue-123-session-data").mkdir()

            config = Mock()
            config.repo_root = base / "repo"
            config.repo_root.mkdir()
            config.repo = "org/repo"
            config.worktree_base = str(base)
            config.agents = {}

            diagnosis = create_session_failure_diagnosis(
                issue_number=123,
                session_history=[],
                active_sessions=[],
                config=config,
                agents={},
            )

            assert diagnosis.worktree_path == str(worktree.resolve())

    @patch("issue_orchestrator.adapters.session_log.registry.get_log_provider")
    def test_finds_worktree_by_review_session_directory(self, mock_get_provider):
        """Finds worktree via review-{N} session directory pattern."""
        mock_get_provider.return_value = None

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            worktree = base / "repo-999"
            worktree.mkdir()
            sessions_dir = worktree / ".issue-orchestrator" / "sessions"
            sessions_dir.mkdir(parents=True)
            (sessions_dir / "review-456-session").mkdir()

            config = Mock()
            config.repo_root = base / "repo"
            config.repo_root.mkdir()
            config.repo = "org/repo"
            config.worktree_base = str(base)
            config.agents = {}

            diagnosis = create_session_failure_diagnosis(
                issue_number=456,
                session_history=[],
                active_sessions=[],
                config=config,
                agents={},
            )

            assert diagnosis.worktree_path == str(worktree.resolve())

    @patch("issue_orchestrator.adapters.session_log.registry.get_log_provider")
    def test_finds_worktree_by_rework_session_directory(self, mock_get_provider):
        """Finds worktree via rework-{N} session directory pattern."""
        mock_get_provider.return_value = None

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            worktree = base / "repo-999"
            worktree.mkdir()
            sessions_dir = worktree / ".issue-orchestrator" / "sessions"
            sessions_dir.mkdir(parents=True)
            (sessions_dir / "rework-789-session").mkdir()

            config = Mock()
            config.repo_root = base / "repo"
            config.repo_root.mkdir()
            config.repo = "org/repo"
            config.worktree_base = str(base)
            config.agents = {}

            diagnosis = create_session_failure_diagnosis(
                issue_number=789,
                session_history=[],
                active_sessions=[],
                config=config,
                agents={},
            )

            assert diagnosis.worktree_path == str(worktree.resolve())

    @patch("issue_orchestrator.adapters.session_log.registry.get_log_provider")
    def test_worktree_not_found_returns_none(self, mock_get_provider):
        """When worktree cannot be found, worktree_path is None."""
        mock_get_provider.return_value = None

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)

            config = Mock()
            config.repo_root = base / "repo"
            config.repo_root.mkdir()
            config.repo = "org/repo"
            config.worktree_base = str(base)
            config.agents = {}

            diagnosis = create_session_failure_diagnosis(
                issue_number=123,
                session_history=[],
                active_sessions=[],
                config=config,
                agents={},
            )

            assert diagnosis.worktree_path is None

    @patch("issue_orchestrator.adapters.session_log.registry.get_log_provider")
    def test_searches_agent_worktree_bases(self, mock_get_provider):
        """Searches agent-specific worktree_base directories."""
        mock_get_provider.return_value = None

        with tempfile.TemporaryDirectory() as tmpdir:
            agent_base = Path(tmpdir) / "agent-base"
            agent_base.mkdir()
            worktree = agent_base / "repo-123"
            worktree.mkdir()

            agent_config = Mock(worktree_base=str(agent_base))

            config = Mock()
            config.repo_root = Path(tmpdir) / "repo"
            config.repo_root.mkdir()
            config.repo = "org/repo"
            config.worktree_base = None
            config.agents = {"agent1": agent_config}

            diagnosis = create_session_failure_diagnosis(
                issue_number=123,
                session_history=[],
                active_sessions=[],
                config=config,
                agents={},
            )

            assert diagnosis.worktree_path == str(worktree.resolve())

    # =========================================================================
    # Session History Tests
    # =========================================================================

    @patch("issue_orchestrator.adapters.session_log.registry.get_log_provider")
    def test_extracts_history_status_and_reason(self, mock_get_provider):
        """History status and reason are extracted from matching history entry."""
        mock_get_provider.return_value = None

        history_entry = Mock(
            issue_number=123,
            status="blocked",
            status_reason="permissions",
            agent_type="agent-default",
        )

        config = Mock()
        config.repo_root = Path("/repo")
        config.repo = "org/repo"
        config.worktree_base = None
        config.agents = {}

        diagnosis = create_session_failure_diagnosis(
            issue_number=123,
            session_history=[history_entry],
            active_sessions=[],
            config=config,
            agents={},
        )

        assert diagnosis.history_status == "blocked"
        assert diagnosis.history_reason == "permissions"

    @patch("issue_orchestrator.adapters.session_log.registry.get_log_provider")
    def test_uses_most_recent_history_entry(self, mock_get_provider):
        """When multiple history entries exist, uses the most recent (last)."""
        mock_get_provider.return_value = None

        old_entry = Mock(
            issue_number=123,
            status="completed",
            status_reason="success",
            agent_type="agent-default",
        )
        new_entry = Mock(
            issue_number=123,
            status="blocked",
            status_reason="timeout",
            agent_type="agent-default",
        )

        config = Mock()
        config.repo_root = Path("/repo")
        config.repo = "org/repo"
        config.worktree_base = None
        config.agents = {}

        diagnosis = create_session_failure_diagnosis(
            issue_number=123,
            session_history=[old_entry, new_entry],
            active_sessions=[],
            config=config,
            agents={},
        )

        assert diagnosis.history_status == "blocked"
        assert diagnosis.history_reason == "timeout"

    @patch("issue_orchestrator.adapters.session_log.registry.get_log_provider")
    def test_no_history_returns_none_status(self, mock_get_provider):
        """When no history entry matches, status and reason are None."""
        mock_get_provider.return_value = None

        config = Mock()
        config.repo_root = Path("/repo")
        config.repo = "org/repo"
        config.worktree_base = None
        config.agents = {}

        diagnosis = create_session_failure_diagnosis(
            issue_number=123,
            session_history=[],
            active_sessions=[],
            config=config,
            agents={},
        )

        assert diagnosis.history_status is None
        assert diagnosis.history_reason is None

    # =========================================================================
    # AI System Detection Tests
    # =========================================================================

    @patch("issue_orchestrator.adapters.session_log.registry.get_log_provider")
    @patch("issue_orchestrator.ports.session_log.detect_ai_system_from_command")
    def test_detects_ai_system_from_active_session_config(
        self, mock_detect, mock_get_provider
    ):
        """AI system is detected from active session's agent config command."""
        mock_detect.return_value = "anthropic-claude"
        mock_get_provider.return_value = None

        agent_config = Mock(command="claude-code", permission_mode="bypassPermissions")
        active_session = Mock(
            issue=Mock(number=123),
            worktree_path="/path/to/wt",
            agent_config=agent_config,
        )
        config = Mock()
        config.repo_root = Path("/repo")
        config.repo = "org/repo"
        config.worktree_base = None
        config.agents = {}

        diagnosis = create_session_failure_diagnosis(
            issue_number=123,
            session_history=[],
            active_sessions=[active_session],
            config=config,
            agents={},
        )

        assert diagnosis.ai_system == "anthropic-claude"
        mock_detect.assert_called_with("claude-code")

    @patch("issue_orchestrator.adapters.session_log.registry.get_log_provider")
    @patch("issue_orchestrator.ports.session_log.detect_ai_system_from_command")
    def test_detects_ai_system_from_history_agent_type(
        self, mock_detect, mock_get_provider
    ):
        """AI system is detected from history entry's agent type."""
        mock_detect.return_value = "cursor"
        mock_get_provider.return_value = None

        history_entry = Mock(
            issue_number=123,
            status="blocked",
            status_reason="error",
            agent_type="agent-cursor",
        )
        agent_config = Mock(command="cursor", permission_mode="default")

        config = Mock()
        config.repo_root = Path("/repo")
        config.repo = "org/repo"
        config.worktree_base = None
        config.agents = {}

        diagnosis = create_session_failure_diagnosis(
            issue_number=123,
            session_history=[history_entry],
            active_sessions=[],
            config=config,
            agents={"agent-cursor": agent_config},
        )

        assert diagnosis.ai_system == "cursor"

    @patch("issue_orchestrator.adapters.session_log.registry.get_log_provider")
    def test_defaults_to_claude_code_when_no_config(self, mock_get_provider):
        """Defaults to claude-code when no agent config is found."""
        mock_get_provider.return_value = None

        config = Mock()
        config.repo_root = Path("/repo")
        config.repo = "org/repo"
        config.worktree_base = None
        config.agents = {}

        diagnosis = create_session_failure_diagnosis(
            issue_number=123,
            session_history=[],
            active_sessions=[],
            config=config,
            agents={},
        )

        assert diagnosis.ai_system == "claude-code"
        assert diagnosis.permission_mode == "unknown"

    # =========================================================================
    # Permission Mode Tests
    # =========================================================================

    @patch("issue_orchestrator.adapters.session_log.registry.get_log_provider")
    def test_extracts_permission_mode_from_agent_config(self, mock_get_provider):
        """Permission mode is extracted from active session's agent config."""
        mock_get_provider.return_value = None

        agent_config = AgentConfig(
            prompt_path=Path("prompt.md"), permission_mode="bypassPermissions"
        )
        active_session = Mock(
            issue=Mock(number=123),
            worktree_path="/path/to/wt",
            agent_config=agent_config,
        )
        config = Mock()
        config.repo_root = Path("/repo")
        config.repo = "org/repo"
        config.worktree_base = None
        config.agents = {}

        diagnosis = create_session_failure_diagnosis(
            issue_number=123,
            session_history=[],
            active_sessions=[active_session],
            config=config,
            agents={},
        )

        assert diagnosis.permission_mode == "bypassPermissions"

    @patch("issue_orchestrator.adapters.session_log.registry.get_log_provider")
    def test_provider_args_permission_mode_suppresses_default_warning(
        self, mock_get_provider
    ):
        """provider_args permission_mode is honored by diagnostics: no false
        'default' warning for an agent launched with bypassPermissions."""
        mock_get_provider.return_value = None

        agent_config = AgentConfig(
            prompt_path=Path("prompt.md"),
            provider="claude-code",
            provider_args={"permission_mode": "bypassPermissions"},
        )
        active_session = Mock(
            issue=Mock(number=123),
            worktree_path="/path/to/wt",
            agent_config=agent_config,
        )
        config = Mock()
        config.repo_root = Path("/repo")
        config.repo = "org/repo"
        config.worktree_base = None
        config.agents = {}

        diagnosis = create_session_failure_diagnosis(
            issue_number=123,
            session_history=[],
            active_sessions=[active_session],
            config=config,
            agents={},
        )

        assert diagnosis.permission_mode == "bypassPermissions"
        assert not any("default" in w.lower() for w in diagnosis.warnings)

    # =========================================================================
    # Warnings and Suggestions Tests
    # =========================================================================

    @patch("issue_orchestrator.adapters.session_log.registry.get_log_provider")
    def test_warns_about_default_permission_mode(self, mock_get_provider):
        """Warns when permission_mode is 'default'."""
        mock_get_provider.return_value = None

        agent_config = AgentConfig(prompt_path=Path("prompt.md"))
        active_session = Mock(
            issue=Mock(number=123),
            worktree_path="/path/to/wt",
            agent_config=agent_config,
        )
        config = Mock()
        config.repo_root = Path("/repo")
        config.repo = "org/repo"
        config.worktree_base = None
        config.agents = {}

        diagnosis = create_session_failure_diagnosis(
            issue_number=123,
            session_history=[],
            active_sessions=[active_session],
            config=config,
            agents={},
        )

        assert any("default" in w.lower() for w in diagnosis.warnings)
        assert any("permission_mode" in s.lower() for s in diagnosis.suggestions)

    @patch("issue_orchestrator.adapters.session_log.registry.get_log_provider")
    def test_warns_about_missing_log(self, mock_get_provider):
        """Warns when no log file is found."""
        mock_provider = Mock()
        mock_provider.get_log_path.return_value = None
        mock_get_provider.return_value = mock_provider

        agent_config = Mock(permission_mode="bypassPermissions", command="claude-code")
        active_session = Mock(
            issue=Mock(number=123),
            worktree_path="/path/to/wt",
            agent_config=agent_config,
        )
        config = Mock()
        config.repo_root = Path("/repo")
        config.repo = "org/repo"
        config.worktree_base = None
        config.agents = {}

        diagnosis = create_session_failure_diagnosis(
            issue_number=123,
            session_history=[],
            active_sessions=[active_session],
            config=config,
            agents={},
        )

        assert any("log" in w.lower() for w in diagnosis.warnings)

    @patch("issue_orchestrator.adapters.session_log.registry.get_log_provider")
    def test_warns_about_permission_errors_in_log_context(self, mock_get_provider):
        """Warns when log context contains permission-related errors."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "log.txt"
            log_path.touch()

            mock_provider = Mock()
            mock_provider.get_log_path.return_value = log_path
            mock_provider.get_failure_context.return_value = (
                "ERROR: permission denied when accessing file"
            )
            mock_get_provider.return_value = mock_provider

            agent_config = Mock(permission_mode="bypassPermissions", command="claude-code")
            active_session = Mock(
                issue=Mock(number=123),
                worktree_path="/path/to/wt",
                agent_config=agent_config,
            )
            config = Mock()
            config.repo_root = Path("/repo")
            config.repo = "org/repo"
            config.worktree_base = None
            config.agents = {}

            diagnosis = create_session_failure_diagnosis(
                issue_number=123,
                session_history=[],
                active_sessions=[active_session],
                config=config,
                agents={},
            )

            assert any("permission" in w.lower() for w in diagnosis.warnings)

    @patch("issue_orchestrator.adapters.session_log.registry.get_log_provider")
    def test_no_warnings_for_healthy_state(self, mock_get_provider):
        """No warnings when everything looks good."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "log.txt"
            log_path.touch()

            mock_provider = Mock()
            mock_provider.get_log_path.return_value = log_path
            mock_provider.get_failure_context.return_value = "Session completed successfully"
            mock_get_provider.return_value = mock_provider

            agent_config = Mock(permission_mode="bypassPermissions", command="claude-code")
            active_session = Mock(
                issue=Mock(number=123),
                worktree_path="/path/to/wt",
                agent_config=agent_config,
            )
            config = Mock()
            config.repo_root = Path("/repo")
            config.repo = "org/repo"
            config.worktree_base = None
            config.agents = {}

            diagnosis = create_session_failure_diagnosis(
                issue_number=123,
                session_history=[],
                active_sessions=[active_session],
                config=config,
                agents={},
            )

            assert diagnosis.warnings == []
            assert diagnosis.suggestions == []

    # =========================================================================
    # Log Provider Integration Tests
    # =========================================================================

    @patch("issue_orchestrator.adapters.session_log.registry.get_log_provider")
    def test_sets_log_exists_correctly(self, mock_get_provider):
        """log_exists is set based on actual path existence."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "log.txt"
            log_path.touch()

            mock_provider = Mock()
            mock_provider.get_log_path.return_value = log_path
            mock_provider.get_failure_context.return_value = "context"
            mock_get_provider.return_value = mock_provider

            agent_config = Mock(permission_mode="bypassPermissions", command="claude-code")
            active_session = Mock(
                issue=Mock(number=123),
                worktree_path="/path/to/wt",
                agent_config=agent_config,
            )
            config = Mock()
            config.repo_root = Path("/repo")
            config.repo = "org/repo"
            config.worktree_base = None
            config.agents = {}

            diagnosis = create_session_failure_diagnosis(
                issue_number=123,
                session_history=[],
                active_sessions=[active_session],
                config=config,
                agents={},
            )

            assert diagnosis.log_exists is True
            assert diagnosis.log_path == str(log_path)

    @patch("issue_orchestrator.adapters.session_log.registry.get_log_provider")
    def test_handles_missing_log_provider(self, mock_get_provider):
        """Handles when log provider is not available."""
        mock_get_provider.return_value = None

        agent_config = Mock(permission_mode="bypassPermissions", command="claude-code")
        active_session = Mock(
            issue=Mock(number=123),
            worktree_path="/path/to/wt",
            agent_config=agent_config,
        )
        config = Mock()
        config.repo_root = Path("/repo")
        config.repo = "org/repo"
        config.worktree_base = None
        config.agents = {}

        diagnosis = create_session_failure_diagnosis(
            issue_number=123,
            session_history=[],
            active_sessions=[active_session],
            config=config,
            agents={},
        )

        assert diagnosis.log_path is None
        assert diagnosis.log_exists is False

    @patch("issue_orchestrator.adapters.session_log.registry.get_log_provider")
    def test_captures_log_context_from_provider(self, mock_get_provider):
        """Log context is captured from the provider."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "log.txt"
            log_path.touch()

            mock_provider = Mock()
            mock_provider.get_log_path.return_value = log_path
            mock_provider.get_failure_context.return_value = "Detailed failure context"
            mock_get_provider.return_value = mock_provider

            agent_config = Mock(permission_mode="bypassPermissions", command="claude-code")
            active_session = Mock(
                issue=Mock(number=123),
                worktree_path="/path/to/wt",
                agent_config=agent_config,
            )
            config = Mock()
            config.repo_root = Path("/repo")
            config.repo = "org/repo"
            config.worktree_base = None
            config.agents = {}

            diagnosis = create_session_failure_diagnosis(
                issue_number=123,
                session_history=[],
                active_sessions=[active_session],
                config=config,
                agents={},
            )

            assert diagnosis.log_context == "Detailed failure context"

    # =========================================================================
    # Edge Case Tests (prevent regressions)
    # =========================================================================

    @patch("issue_orchestrator.adapters.session_log.registry.get_log_provider")
    @patch("issue_orchestrator.ports.session_log.detect_ai_system_from_command")
    def test_prefers_agent_config_over_history_for_ai_detection(
        self, mock_detect, mock_get_provider
    ):
        """When both active session and history exist, uses active session's config."""
        # Return different values for each call to distinguish sources
        mock_detect.side_effect = ["from-active-session", "from-history"]
        mock_get_provider.return_value = None

        # Active session with its own config
        active_config = Mock(command="active-cmd", effective_permission_mode="active-mode")
        active_session = Mock(
            issue=Mock(number=123),
            worktree_path="/active/path",
            agent_config=active_config,
        )

        # History entry with different agent
        history_entry = Mock(
            issue_number=123,
            status="blocked",
            status_reason="error",
            agent_type="agent-history",
        )
        history_config = Mock(command="history-cmd", permission_mode="history-mode")

        config = Mock()
        config.repo_root = Path("/repo")
        config.repo = "org/repo"
        config.worktree_base = None
        config.agents = {}

        diagnosis = create_session_failure_diagnosis(
            issue_number=123,
            session_history=[history_entry],
            active_sessions=[active_session],
            config=config,
            agents={"agent-history": history_config},
        )

        # Should use active session's config, not history
        assert diagnosis.ai_system == "from-active-session"
        assert diagnosis.permission_mode == "active-mode"

    @patch("issue_orchestrator.adapters.session_log.registry.get_log_provider")
    def test_suggestion_mentions_claude_projects_path(self, mock_get_provider):
        """When log is missing, suggestion mentions .claude/projects path."""
        mock_provider = Mock()
        mock_provider.get_log_path.return_value = None
        mock_get_provider.return_value = mock_provider

        agent_config = Mock(permission_mode="bypassPermissions", command="claude-code")
        active_session = Mock(
            issue=Mock(number=123),
            worktree_path="/path/to/wt",
            agent_config=agent_config,
        )
        config = Mock()
        config.repo_root = Path("/repo")
        config.repo = "org/repo"
        config.worktree_base = None
        config.agents = {}

        diagnosis = create_session_failure_diagnosis(
            issue_number=123,
            session_history=[],
            active_sessions=[active_session],
            config=config,
            agents={},
        )

        assert any(".claude/projects" in s for s in diagnosis.suggestions)

    @patch("issue_orchestrator.adapters.session_log.registry.get_log_provider")
    def test_ignores_non_matching_history_entries(self, mock_get_provider):
        """History entries for other issues are ignored."""
        mock_get_provider.return_value = None

        # History entries for different issues
        other_entry1 = Mock(issue_number=100, status="completed", status_reason="ok")
        other_entry2 = Mock(issue_number=200, status="blocked", status_reason="error")

        config = Mock()
        config.repo_root = Path("/repo")
        config.repo = "org/repo"
        config.worktree_base = None
        config.agents = {}

        diagnosis = create_session_failure_diagnosis(
            issue_number=123,  # Different from all entries
            session_history=[other_entry1, other_entry2],
            active_sessions=[],
            config=config,
            agents={},
        )

        # Should not find any matching history
        assert diagnosis.history_status is None
        assert diagnosis.history_reason is None

    @patch("issue_orchestrator.adapters.session_log.registry.get_log_provider")
    def test_ignores_non_matching_active_sessions(self, mock_get_provider):
        """Active sessions for other issues are ignored."""
        mock_get_provider.return_value = None

        # Active sessions for different issues
        other_session1 = Mock(
            issue=Mock(number=100),
            worktree_path="/path1",
            agent_config=Mock(permission_mode="mode1", command="cmd1"),
        )
        other_session2 = Mock(
            issue=Mock(number=200),
            worktree_path="/path2",
            agent_config=Mock(permission_mode="mode2", command="cmd2"),
        )

        config = Mock()
        config.repo_root = Path("/repo")
        config.repo = "org/repo"
        config.worktree_base = None
        config.agents = {}

        diagnosis = create_session_failure_diagnosis(
            issue_number=123,  # Different from all sessions
            session_history=[],
            active_sessions=[other_session1, other_session2],
            config=config,
            agents={},
        )

        # Should not find worktree from active sessions
        assert diagnosis.worktree_path is None
        # Should fall back to defaults
        assert diagnosis.permission_mode == "unknown"

    @patch("issue_orchestrator.adapters.session_log.registry.get_log_provider")
    def test_log_exists_false_when_path_returned_but_file_missing(self, mock_get_provider):
        """log_exists is False when provider returns path but file doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Provider returns a path that doesn't exist
            nonexistent_log = Path(tmpdir) / "missing.log"

            mock_provider = Mock()
            mock_provider.get_log_path.return_value = nonexistent_log
            mock_provider.get_failure_context.return_value = None
            mock_get_provider.return_value = mock_provider

            agent_config = Mock(permission_mode="bypassPermissions", command="claude-code")
            active_session = Mock(
                issue=Mock(number=123),
                worktree_path="/path/to/wt",
                agent_config=agent_config,
            )
            config = Mock()
            config.repo_root = Path("/repo")
            config.repo = "org/repo"
            config.worktree_base = None
            config.agents = {}

            diagnosis = create_session_failure_diagnosis(
                issue_number=123,
                session_history=[],
                active_sessions=[active_session],
                config=config,
                agents={},
            )

            assert diagnosis.log_path == str(nonexistent_log)
            assert diagnosis.log_exists is False

    @patch("issue_orchestrator.adapters.session_log.registry.get_log_provider")
    def test_searches_repo_root_parent_as_worktree_base(self, mock_get_provider):
        """Always searches repo_root.parent as a fallback worktree base."""
        mock_get_provider.return_value = None

        with tempfile.TemporaryDirectory() as tmpdir:
            # repo_root.parent contains the worktree
            parent_dir = Path(tmpdir)
            repo_root = parent_dir / "main-repo"
            repo_root.mkdir()
            worktree = parent_dir / "repo-123"
            worktree.mkdir()

            config = Mock()
            config.repo_root = repo_root
            config.repo = "org/repo"
            config.worktree_base = None  # No explicit worktree_base
            config.agents = {}

            diagnosis = create_session_failure_diagnosis(
                issue_number=123,
                session_history=[],
                active_sessions=[],
                config=config,
                agents={},
            )

            # Should find worktree in repo_root.parent
            assert diagnosis.worktree_path == str(worktree.resolve())

    @patch("issue_orchestrator.adapters.session_log.registry.get_log_provider")
    def test_direct_worktree_match_takes_priority(self, mock_get_provider):
        """Direct repo-{issue} match is found even with other worktrees present."""
        mock_get_provider.return_value = None

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)

            # Create multiple worktrees
            (base / "repo-100").mkdir()
            (base / "repo-123").mkdir()  # Direct match
            (base / "repo-200").mkdir()

            config = Mock()
            config.repo_root = base / "repo"
            config.repo_root.mkdir()
            config.repo = "org/repo"
            config.worktree_base = str(base)
            config.agents = {}

            diagnosis = create_session_failure_diagnosis(
                issue_number=123,
                session_history=[],
                active_sessions=[],
                config=config,
                agents={},
            )

            # Should find the exact match
            assert diagnosis.worktree_path == str((base / "repo-123").resolve())
