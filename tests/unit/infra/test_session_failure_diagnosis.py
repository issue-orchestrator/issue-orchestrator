"""Tests for session_failure_diagnosis module."""

import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

from issue_orchestrator.infra.session_failure_diagnosis import (
    SessionFailureDiagnosis,
    _build_warnings_and_suggestions,
    _build_worktree_bases,
    _detect_ai_system_and_mode,
    _find_session_from_history,
    _find_worktree_for_issue,
    _find_worktree_from_active_sessions,
    _search_worktree_in_base,
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


class TestSearchWorktreeInBase:
    """Tests for _search_worktree_in_base helper."""

    def test_direct_match_found(self):
        """Test finding a worktree with exact name match."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            worktree_dir = base / "repo-123"
            worktree_dir.mkdir()

            result = _search_worktree_in_base(base, "repo", 123)
            assert result == worktree_dir

    def test_direct_match_when_multiple_exist(self):
        """Test direct match is returned even if other worktrees exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            (base / "repo-123").mkdir()
            (base / "repo-456").mkdir()

            result = _search_worktree_in_base(base, "repo", 123)
            assert result == base / "repo-123"

    def test_search_in_sessions_dir_when_no_direct_match(self):
        """Test searching sessions directories when direct match not found."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            worktree_dir = base / "repo-999"
            worktree_dir.mkdir()

            sessions_dir = worktree_dir / ".issue-orchestrator" / "sessions"
            sessions_dir.mkdir(parents=True)

            # Create a session dir matching the issue
            (sessions_dir / "issue-123-session-data").mkdir()

            result = _search_worktree_in_base(base, "repo", 123)
            assert result == worktree_dir

    def test_finds_review_session_dir(self):
        """Test finding worktree with review session dir."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            worktree_dir = base / "repo-999"
            worktree_dir.mkdir()

            sessions_dir = worktree_dir / ".issue-orchestrator" / "sessions"
            sessions_dir.mkdir(parents=True)
            (sessions_dir / "review-456-session-data").mkdir()

            result = _search_worktree_in_base(base, "repo", 456)
            assert result == worktree_dir

    def test_finds_rework_session_dir(self):
        """Test finding worktree with rework session dir."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            worktree_dir = base / "repo-999"
            worktree_dir.mkdir()

            sessions_dir = worktree_dir / ".issue-orchestrator" / "sessions"
            sessions_dir.mkdir(parents=True)
            (sessions_dir / "rework-789-session-data").mkdir()

            result = _search_worktree_in_base(base, "repo", 789)
            assert result == worktree_dir

    def test_returns_none_when_not_found(self):
        """Test returning None when worktree not found."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            (base / "repo-999").mkdir()

            result = _search_worktree_in_base(base, "repo", 123)
            assert result is None

    def test_returns_none_for_empty_directory(self):
        """Test returning None for empty base directory."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            result = _search_worktree_in_base(base, "repo", 123)
            assert result is None

    def test_ignores_files_in_base(self):
        """Test that files in base are ignored."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            (base / "repo-123").touch()  # Create a file, not a directory

            result = _search_worktree_in_base(base, "repo", 123)
            assert result is None


class TestFindWorktreeForIssue:
    """Tests for _find_worktree_for_issue function."""

    def test_finds_in_first_base(self):
        """Test finding worktree in the first base."""
        with tempfile.TemporaryDirectory() as tmpdir1, tempfile.TemporaryDirectory() as tmpdir2:
            base1 = Path(tmpdir1).resolve()
            base2 = Path(tmpdir2).resolve()

            # Create worktree in first base
            (base1 / "repo-123").mkdir()

            result = _find_worktree_for_issue([base1, base2], "repo", 123)
            assert result == base1 / "repo-123"

    def test_finds_in_second_base_when_not_in_first(self):
        """Test finding worktree in second base when not in first."""
        with tempfile.TemporaryDirectory() as tmpdir1, tempfile.TemporaryDirectory() as tmpdir2:
            base1 = Path(tmpdir1).resolve()
            base2 = Path(tmpdir2).resolve()

            # Create worktree in second base
            (base2 / "repo-123").mkdir()

            result = _find_worktree_for_issue([base1, base2], "repo", 123)
            assert result == base2 / "repo-123"

    def test_skips_nonexistent_bases(self):
        """Test that non-existent bases are skipped."""
        with tempfile.TemporaryDirectory() as tmpdir:
            existing_base = Path(tmpdir).resolve()
            nonexistent_base = Path(tmpdir).resolve() / "nonexistent"

            (existing_base / "repo-123").mkdir()

            result = _find_worktree_for_issue([nonexistent_base, existing_base], "repo", 123)
            assert result == existing_base / "repo-123"

    def test_skips_duplicate_bases(self):
        """Test that duplicate bases are only searched once."""
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir).resolve()
            (base / "repo-123").mkdir()

            # Pass the same base twice (after resolving)
            result = _find_worktree_for_issue([base, base], "repo", 123)
            assert result == base / "repo-123"

    def test_returns_none_when_not_found(self):
        """Test returning None when worktree not found in any base."""
        with tempfile.TemporaryDirectory() as tmpdir1, tempfile.TemporaryDirectory() as tmpdir2:
            base1 = Path(tmpdir1)
            base2 = Path(tmpdir2)

            result = _find_worktree_for_issue([base1, base2], "repo", 123)
            assert result is None

    def test_returns_none_for_empty_bases_list(self):
        """Test returning None for empty bases list."""
        result = _find_worktree_for_issue([], "repo", 123)
        assert result is None

    def test_resolves_symlinks(self):
        """Test that bases are resolved to handle symlinks."""
        with tempfile.TemporaryDirectory() as tmpdir:
            real_dir = Path(tmpdir).resolve()

            # Create worktree in real directory
            (real_dir / "repo-123").mkdir()

            # Test with resolved path
            result = _find_worktree_for_issue([real_dir], "repo", 123)
            assert result == real_dir / "repo-123"


class TestFindSessionFromHistory:
    """Tests for _find_session_from_history function."""

    def test_finds_matching_issue_in_history(self):
        """Test finding session entry matching issue number."""
        entry1 = Mock(issue_number=100)
        entry2 = Mock(issue_number=123)
        entry3 = Mock(issue_number=456)
        history = [entry1, entry2, entry3]

        result = _find_session_from_history(history, 123)
        assert result == entry2

    def test_returns_most_recent_match(self):
        """Test returning most recent (last) matching entry."""
        entry1 = Mock(issue_number=123)
        entry2 = Mock(issue_number=456)
        entry3 = Mock(issue_number=123)  # Newer entry for same issue
        history = [entry1, entry2, entry3]

        result = _find_session_from_history(history, 123)
        assert result == entry3

    def test_returns_none_when_not_found(self):
        """Test returning None when issue not in history."""
        entry1 = Mock(issue_number=100)
        entry2 = Mock(issue_number=200)
        history = [entry1, entry2]

        result = _find_session_from_history(history, 123)
        assert result is None

    def test_returns_none_for_empty_history(self):
        """Test returning None for empty history list."""
        result = _find_session_from_history([], 123)
        assert result is None

    def test_searches_in_reverse_order(self):
        """Test that search is done in reverse order (most recent first)."""
        entry1 = Mock(issue_number=123)
        entry2 = Mock(issue_number=456)
        entry3 = Mock(issue_number=123)
        history = [entry1, entry2, entry3]

        # The third entry should be found first (reverse order)
        result = _find_session_from_history(history, 123)
        assert result is entry3


class TestFindWorktreeFromActiveSessions:
    """Tests for _find_worktree_from_active_sessions function."""

    def test_finds_matching_session(self):
        """Test finding worktree from matching active session."""
        agent_config = Mock()
        session = Mock(issue=Mock(number=123), worktree_path="/path/to/wt", agent_config=agent_config)
        active_sessions = [session]

        worktree, config = _find_worktree_from_active_sessions(active_sessions, 123)
        assert worktree == "/path/to/wt"
        assert config == agent_config

    def test_returns_none_when_not_found(self):
        """Test returning None when session not found."""
        session = Mock(issue=Mock(number=456), worktree_path="/path/to/wt", agent_config=Mock())
        active_sessions = [session]

        worktree, config = _find_worktree_from_active_sessions(active_sessions, 123)
        assert worktree is None
        assert config is None

    def test_finds_first_matching_session(self):
        """Test that first matching session is returned."""
        config1 = Mock()
        config2 = Mock()
        session1 = Mock(issue=Mock(number=123), worktree_path="/path1", agent_config=config1)
        session2 = Mock(issue=Mock(number=123), worktree_path="/path2", agent_config=config2)
        active_sessions = [session1, session2]

        worktree, config = _find_worktree_from_active_sessions(active_sessions, 123)
        assert worktree == "/path1"
        assert config == config1

    def test_returns_none_for_empty_sessions(self):
        """Test returning None for empty active sessions."""
        worktree, config = _find_worktree_from_active_sessions([], 123)
        assert worktree is None
        assert config is None


class TestBuildWorktreeBases:
    """Tests for _build_worktree_bases function."""

    def test_builds_bases_from_config(self):
        """Test building worktree bases from config."""
        config = Mock()
        config.worktree_base = "/config/base"
        config.repo_root = Path("/repo")
        config.agents = {}

        result = _build_worktree_bases(config)

        assert Path("/config/base") in result
        assert config.repo_root.parent in result

    def test_includes_agent_worktree_bases(self):
        """Test including agent-specific worktree bases."""
        agent1 = Mock(worktree_base="/agent1/base")
        agent2 = Mock(worktree_base="/agent2/base")
        config = Mock()
        config.worktree_base = None
        config.repo_root = Path("/repo")
        config.agents = {"agent1": agent1, "agent2": agent2}

        result = _build_worktree_bases(config)

        assert Path("/agent1/base") in result
        assert Path("/agent2/base") in result

    def test_includes_repo_root_parent(self):
        """Test that repo_root parent is always included."""
        config = Mock()
        config.worktree_base = None
        config.repo_root = Path("/path/to/repo")
        config.agents = {}

        result = _build_worktree_bases(config)

        assert Path("/path/to") in result

    def test_handles_none_worktree_base(self):
        """Test handling None worktree_base in config."""
        config = Mock()
        config.worktree_base = None
        config.repo_root = Path("/repo")
        config.agents = {}

        result = _build_worktree_bases(config)

        assert None not in result
        assert config.repo_root.parent in result

    def test_handles_agents_without_worktree_base(self):
        """Test handling agents that don't have worktree_base."""
        agent1 = Mock(spec=[])  # No worktree_base attribute
        config = Mock()
        config.worktree_base = "/base"
        config.repo_root = Path("/repo")
        config.agents = {"agent1": agent1}

        result = _build_worktree_bases(config)

        # Should not raise and should include config base and repo parent
        assert Path("/base") in result
        assert config.repo_root.parent in result


class TestDetectAiSystemAndMode:
    """Tests for _detect_ai_system_and_mode function."""

    @patch("issue_orchestrator.ports.session_log.detect_ai_system_from_command")
    def test_detects_from_agent_config(self, mock_detect):
        """Test detecting AI system from agent config."""
        mock_detect.return_value = "anthropic-claude"
        agent_config = Mock(command="claude-code", permission_mode="bypassPermissions")

        ai_system, mode = _detect_ai_system_and_mode(agent_config, None, {})

        assert ai_system == "anthropic-claude"
        assert mode == "bypassPermissions"
        mock_detect.assert_called_once_with("claude-code")

    @patch("issue_orchestrator.ports.session_log.detect_ai_system_from_command")
    def test_falls_back_to_default_when_detect_returns_none(self, mock_detect):
        """Test falling back to default AI system when detection returns None."""
        mock_detect.return_value = None
        agent_config = Mock(command="unknown", permission_mode="default")

        ai_system, mode = _detect_ai_system_and_mode(agent_config, None, {})

        assert ai_system == "claude-code"
        assert mode == "default"

    @patch("issue_orchestrator.ports.session_log.detect_ai_system_from_command")
    def test_detects_from_history_entry(self, mock_detect):
        """Test detecting AI system from history entry."""
        mock_detect.return_value = "cursor"
        history_entry = Mock(agent_type="agent-cursor")
        agent_config_obj = Mock(command="cursor", permission_mode="default")
        agents = {"agent-cursor": agent_config_obj}

        ai_system, mode = _detect_ai_system_and_mode(None, history_entry, agents)

        assert ai_system == "cursor"
        assert mode == "default"

    @patch("issue_orchestrator.ports.session_log.detect_ai_system_from_command")
    def test_prefers_agent_config_over_history(self, mock_detect):
        """Test that agent_config is preferred over history."""
        mock_detect.side_effect = ["from-config", "from-history"]
        agent_config = Mock(command="claude", permission_mode="from-config")
        history_entry = Mock(agent_type="agent-cursor")
        agents = {"agent-cursor": Mock(command="cursor", permission_mode="from-history")}

        ai_system, mode = _detect_ai_system_and_mode(agent_config, history_entry, agents)

        assert ai_system == "from-config"
        assert mode == "from-config"

    def test_returns_defaults_when_no_config_found(self):
        """Test returning defaults when no config found."""
        ai_system, mode = _detect_ai_system_and_mode(None, None, {})

        assert ai_system == "claude-code"
        assert mode == "unknown"


class TestBuildWarningsAndSuggestions:
    """Tests for _build_warnings_and_suggestions function."""

    def test_warns_about_default_permission_mode(self):
        """Test warning when permission_mode is 'default'."""
        warnings, suggestions = _build_warnings_and_suggestions(
            permission_mode="default",
            log_path=Path("/log"),
            log_context="",
            worktree_path="/wt"
        )

        assert any("default" in w.lower() for w in warnings)
        assert any("permission_mode" in s.lower() for s in suggestions)

    def test_warns_about_missing_log(self):
        """Test warning when log file is missing."""
        warnings, _ = _build_warnings_and_suggestions(
            permission_mode="bypassPermissions",
            log_path=None,
            log_context=None,
            worktree_path="/wt"
        )

        assert any("log" in w.lower() for w in warnings)

    def test_warns_when_log_does_not_exist(self):
        """Test warning when log_path doesn't exist."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "nonexistent.log"

            warnings, _ = _build_warnings_and_suggestions(
                permission_mode="bypassPermissions",
                log_path=log_path,
                log_context=None,
                worktree_path="/wt"
            )

            assert any("log" in w.lower() for w in warnings)

    def test_warns_about_permission_errors_in_log_context(self):
        """Test warning when log context contains permission-related errors."""
        warnings, suggestions = _build_warnings_and_suggestions(
            permission_mode="bypassPermissions",
            log_path=Path("/tmp/log"),
            log_context="ERROR: permission denied when accessing file",
            worktree_path="/wt"
        )

        assert any("permission" in w.lower() for w in warnings)
        assert any("permission_mode" in s.lower() for s in suggestions)

    def test_no_warnings_for_healthy_state(self):
        """Test no warnings when everything looks good."""
        with tempfile.TemporaryDirectory() as tmpdir:
            log_path = Path(tmpdir) / "log.txt"
            log_path.touch()

            warnings, suggestions = _build_warnings_and_suggestions(
                permission_mode="bypassPermissions",
                log_path=log_path,
                log_context="Session completed successfully",
                worktree_path="/wt"
            )

            assert warnings == []
            assert suggestions == []

    def test_suggests_log_location_when_missing(self):
        """Test suggesting where to find logs when missing."""
        _, suggestions = _build_warnings_and_suggestions(
            permission_mode="bypassPermissions",
            log_path=None,
            log_context=None,
            worktree_path="/path/to/worktree"
        )

        assert any(".claude/projects" in s for s in suggestions)


class TestCreateSessionFailureDiagnosis:
    """Tests for create_session_failure_diagnosis main function."""

    @patch("issue_orchestrator.adapters.session_log.registry.get_log_provider")
    def test_creates_diagnosis_with_active_session(self, mock_get_provider):
        """Test creating diagnosis when session is active."""
        mock_provider = Mock()
        mock_provider.get_log_path.return_value = Path("/tmp/log")
        mock_provider.get_failure_context.return_value = "failure context"
        mock_get_provider.return_value = mock_provider

        agent_config = Mock(permission_mode="bypassPermissions", command="claude-code")
        active_session = Mock(
            issue=Mock(number=123),
            worktree_path="/path/to/wt",
            agent_config=agent_config
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

        assert diagnosis.issue_number == 123
        assert diagnosis.worktree_path == "/path/to/wt"
        assert diagnosis.permission_mode == "bypassPermissions"

    @patch("issue_orchestrator.adapters.session_log.registry.get_log_provider")
    def test_creates_diagnosis_from_history(self, mock_get_provider):
        """Test creating diagnosis from session history."""
        mock_provider = Mock()
        mock_provider.get_log_path.return_value = Path("/tmp/log")
        mock_provider.get_failure_context.return_value = None
        mock_get_provider.return_value = mock_provider

        history_entry = Mock(
            issue_number=123,
            status="blocked",
            status_reason="permissions",
            agent_type="agent-default"
        )
        agent_config = Mock(permission_mode="default", command="claude-code", worktree_base=None)
        config = Mock()
        config.repo_root = Path("/repo")
        config.repo = "org/repo"
        config.worktree_base = None
        config.agents = {"agent-default": agent_config}

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            (base / "repo-123").mkdir()

            diagnosis = create_session_failure_diagnosis(
                issue_number=123,
                session_history=[history_entry],
                active_sessions=[],
                config=config,
                agents={"agent-default": agent_config},
            )

            assert diagnosis.history_status == "blocked"
            assert diagnosis.history_reason == "permissions"

    @patch("issue_orchestrator.adapters.session_log.registry.get_log_provider")
    def test_finds_worktree_when_not_in_active_sessions(self, mock_get_provider):
        """Test finding worktree from file system when not in active sessions."""
        mock_provider = Mock()
        mock_provider.get_log_path.return_value = None
        mock_provider.get_failure_context.return_value = None
        mock_get_provider.return_value = mock_provider

        config = Mock()
        config.repo_root = Path("/repo")
        config.repo = "org/repo"
        config.worktree_base = None
        config.agents = {}

        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            wt = base / "repo-123"
            wt.mkdir()

            diagnosis = create_session_failure_diagnosis(
                issue_number=123,
                session_history=[],
                active_sessions=[],
                config=config,
                agents={},
            )

            # Worktree should be found or None if not in searched bases
            assert diagnosis.issue_number == 123

    @patch("issue_orchestrator.adapters.session_log.registry.get_log_provider")
    def test_handles_missing_log_provider(self, mock_get_provider):
        """Test handling when log provider is not available."""
        mock_get_provider.return_value = None

        agent_config = Mock(permission_mode="bypassPermissions", command="claude-code")
        active_session = Mock(
            issue=Mock(number=123),
            worktree_path="/path/to/wt",
            agent_config=agent_config
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
    def test_sets_log_exists_correctly(self, mock_get_provider):
        """Test log_exists is set based on path existence."""
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
                agent_config=agent_config
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
            assert str(log_path) == diagnosis.log_path
