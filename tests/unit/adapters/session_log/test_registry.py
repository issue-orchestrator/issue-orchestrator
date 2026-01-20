"""Unit tests for session_log/registry.py."""

import json
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from issue_orchestrator.adapters.session_log.registry import (
    DataDrivenLogProvider,
    get_log_provider,
    get_failure_context_for_session,
)
from issue_orchestrator.infra.ai_systems_config import AISystemConfig, AISystemsConfig


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def mock_ai_system_config():
    """Create a mock AI system config for testing."""
    return AISystemConfig(
        name="test-system",
        description="Test AI System",
        log_pattern="{worktree}/.logs/test.log",
        log_format="text",
        console_tags=["test-tag"],
        error_patterns=["error", "failed", "exception"],
        completion_marker="agent-done",
    )


@pytest.fixture
def mock_systems_config(mock_ai_system_config):
    """Create a mock AISystemsConfig with test systems."""
    config = AISystemsConfig(
        systems={
            "test-system": mock_ai_system_config,
            "claude-code": AISystemConfig(
                name="claude-code",
                log_pattern="{home}/.claude/logs/session-{project_hash}.jsonl",
                log_format="jsonl",
                console_tags=["claude"],
                error_patterns=["error", "failed"],
                completion_marker="agent-done",
            ),
            "gemini": AISystemConfig(
                name="gemini",
                log_pattern="/tmp/gemini-{project_hash}.json",
                log_format="json",
                console_tags=["gemini"],
                error_patterns=["error"],
            ),
        },
        default_ai_system="test-system",
    )
    return config


@pytest.fixture
def temp_log_dir(tmp_path):
    """Create a temporary directory for log files."""
    return tmp_path


@pytest.fixture
def text_log_file(temp_log_dir):
    """Create a sample text log file."""
    log_path = temp_log_dir / "test.log"
    content = """Starting test run
Processing input
Error: Something went wrong
Recovery attempted
Session completed
"""
    log_path.write_text(content)
    return log_path


@pytest.fixture
def jsonl_log_file(temp_log_dir):
    """Create a sample JSONL log file."""
    log_path = temp_log_dir / "test.jsonl"
    entries = [
        {"type": "user", "content": "Hello"},
        {"type": "assistant", "content": "Hi there"},
        {"type": "tool_use", "name": "bash", "input": "ls -la"},
        {"type": "tool_result", "result": {"is_error": False, "content": "file1.txt"}},
        {"type": "error", "message": "error: something failed", "severity": "high"},
        {"type": "event", "content": "Permission denied on /tmp"},
        {"type": "tool_result", "result": {"is_error": True, "content": "Access denied"}},
        {"type": "event", "content": "agent-done completed"},
    ]
    content = "\n".join(json.dumps(entry) for entry in entries)
    log_path.write_text(content)
    return log_path


@pytest.fixture
def json_log_file(temp_log_dir):
    """Create a sample JSON log file."""
    log_path = temp_log_dir / "test.json"
    data = {
        "messages": [
            {"role": "user", "content": "Hello"},
            {"role": "assistant", "content": "Hi there"},
            {"role": "tool", "content": "Tool output"},
            {"role": "user", "content": "Error occurred"},
            {"role": "error", "content": "Something failed"},
        ],
        "error": "Connection timeout during execution",
    }
    log_path.write_text(json.dumps(data))
    return log_path


@pytest.fixture
def markdown_log_file(temp_log_dir):
    """Create a sample markdown log file."""
    log_path = temp_log_dir / "test.md"
    content = """# Session Log

## Start
Initialized session

## Processing
- Task 1: Running
- Error: Failed to connect
- Status: retry

## Errors Found
- error: Connection refused
- failed: Network timeout

## Final Status
Completed with warnings
"""
    log_path.write_text(content)
    return log_path


@pytest.fixture
def malformed_jsonl_file(temp_log_dir):
    """Create a JSONL file with some malformed entries."""
    log_path = temp_log_dir / "malformed.jsonl"
    content = """{"type": "user", "content": "valid"}
this is not json
{"type": "assistant", "content": "valid again"}
also not json at all
{"type": "error", "message": "real error"}
"""
    log_path.write_text(content)
    return log_path


@pytest.fixture
def provider(mock_ai_system_config, mock_systems_config):
    """Create a DataDrivenLogProvider for testing."""
    return DataDrivenLogProvider(mock_ai_system_config, mock_systems_config)


# ============================================================================
# Tests for DataDrivenLogProvider
# ============================================================================


class TestDataDrivenLogProviderInit:
    """Tests for DataDrivenLogProvider initialization."""

    def test_init_stores_config(self, mock_ai_system_config, mock_systems_config):
        """Provider stores the config during initialization."""
        provider = DataDrivenLogProvider(mock_ai_system_config, mock_systems_config)
        assert provider._config is mock_ai_system_config
        assert provider._systems_config is mock_systems_config

    def test_ai_system_property(self, provider, mock_ai_system_config):
        """ai_system property returns the system name."""
        assert provider.ai_system == "test-system"
        assert provider.ai_system == mock_ai_system_config.name


class TestGetLogPath:
    """Tests for get_log_path method."""

    def test_get_log_path_with_no_pattern(self, mock_systems_config):
        """get_log_path returns None when config has no pattern."""
        config = AISystemConfig(
            name="no-pattern",
            log_pattern="",  # Empty pattern
        )
        provider = DataDrivenLogProvider(config, mock_systems_config)
        result: Path | None = provider.get_log_path(Path("/tmp"), "session1")
        assert result is None

    def test_get_log_path_with_direct_path_exists(self, temp_log_dir, mock_systems_config):
        """get_log_path returns path when file exists (no glob)."""
        log_file = temp_log_dir / "test.log"
        log_file.write_text("test log")

        config = AISystemConfig(
            name="test",
            log_pattern=str(log_file),  # Direct path, not a glob
        )
        provider = DataDrivenLogProvider(config, mock_systems_config)
        result = provider.get_log_path(temp_log_dir, "session1")
        assert result == log_file

    def test_get_log_path_with_direct_path_not_exists(self, temp_log_dir, mock_systems_config):
        """get_log_path returns None when direct path doesn't exist."""
        config = AISystemConfig(
            name="test",
            log_pattern=str(temp_log_dir / "nonexistent.log"),
        )
        provider = DataDrivenLogProvider(config, mock_systems_config)
        result = provider.get_log_path(temp_log_dir, "session1")
        assert result is None

    def test_get_log_path_with_glob_pattern_no_matches(self, temp_log_dir, mock_systems_config):
        """get_log_path returns None when glob pattern has no matches."""
        config = AISystemConfig(
            name="test",
            log_pattern=str(temp_log_dir / "*.nonexistent"),  # Glob with no matches
        )
        provider = DataDrivenLogProvider(config, mock_systems_config)
        result = provider.get_log_path(temp_log_dir, "session1")
        assert result is None

    def test_get_log_path_with_glob_pattern_single_match(self, temp_log_dir, mock_systems_config):
        """get_log_path returns the file when glob matches one file."""
        log_file = temp_log_dir / "test.log"
        log_file.write_text("test")

        config = AISystemConfig(
            name="test",
            log_pattern=str(temp_log_dir / "*.log"),  # Glob pattern
        )
        provider = DataDrivenLogProvider(config, mock_systems_config)
        result = provider.get_log_path(temp_log_dir, "session1")
        assert result == log_file

    def test_get_log_path_with_glob_pattern_multiple_matches_returns_newest(
        self, temp_log_dir, mock_systems_config
    ):
        """get_log_path returns the most recently modified file."""
        import time

        log1 = temp_log_dir / "test1.log"
        log2 = temp_log_dir / "test2.log"
        log1.write_text("old")
        time.sleep(0.01)  # Ensure different mtime
        log2.write_text("new")

        config = AISystemConfig(
            name="test",
            log_pattern=str(temp_log_dir / "*.log"),
        )
        provider = DataDrivenLogProvider(config, mock_systems_config)
        result = provider.get_log_path(temp_log_dir, "session1")
        assert result == log2


class TestGetFailureContext:
    """Tests for get_failure_context method."""

    def test_get_failure_context_with_nonexistent_file(self, mock_systems_config):
        """get_failure_context returns None when file doesn't exist."""
        config = AISystemConfig(name="test", log_format="text")
        provider = DataDrivenLogProvider(config, mock_systems_config)
        result = provider.get_failure_context(Path("/nonexistent/log.txt"))
        assert result is None

    def test_get_failure_context_text_format(self, text_log_file, mock_systems_config):
        """get_failure_context parses text log format."""
        config = AISystemConfig(
            name="test",
            log_format="text",
        )
        provider = DataDrivenLogProvider(config, mock_systems_config)
        result = provider.get_failure_context(text_log_file)
        assert result is not None
        assert "Session completed" in result
        assert "Processing input" in result

    def test_get_failure_context_text_format_last_n_lines(self, temp_log_dir, mock_systems_config):
        """get_failure_context returns last N lines for text format."""
        log_path = temp_log_dir / "test.log"
        lines = [f"Line {i}" for i in range(200)]
        log_path.write_text("\n".join(lines))

        config = AISystemConfig(name="test", log_format="text")
        provider = DataDrivenLogProvider(config, mock_systems_config)
        result = provider.get_failure_context(log_path, lines=50)
        # Should have last 50 lines
        for i in range(150, 200):
            assert f"Line {i}" in result

    def test_get_failure_context_jsonl_format(self, jsonl_log_file, mock_systems_config):
        """get_failure_context parses JSONL log format."""
        config = AISystemConfig(
            name="test",
            log_format="jsonl",
            error_patterns=["error", "failed"],
            completion_marker="agent-done",
        )
        provider = DataDrivenLogProvider(config, mock_systems_config)
        result = provider.get_failure_context(jsonl_log_file)
        assert result is not None
        assert "Errors Found" in result
        assert "something failed" in result
        assert "Permission" in result
        assert "Completion: completed" in result

    def test_get_failure_context_jsonl_empty_file(self, temp_log_dir, mock_systems_config):
        """get_failure_context handles empty JSONL files."""
        log_path = temp_log_dir / "empty.jsonl"
        log_path.write_text("")

        config = AISystemConfig(name="test", log_format="jsonl")
        provider = DataDrivenLogProvider(config, mock_systems_config)
        result = provider.get_failure_context(log_path)
        assert result == "Log file is empty"

    def test_get_failure_context_jsonl_with_malformed_entries(
        self, malformed_jsonl_file, mock_systems_config
    ):
        """get_failure_context handles malformed JSON entries gracefully."""
        config = AISystemConfig(
            name="test",
            log_format="jsonl",
            error_patterns=["error"],
        )
        provider = DataDrivenLogProvider(config, mock_systems_config)
        result = provider.get_failure_context(malformed_jsonl_file)
        assert result is not None
        assert "real error" in result

    def test_get_failure_context_json_format(self, json_log_file, mock_systems_config):
        """get_failure_context parses JSON log format."""
        config = AISystemConfig(
            name="test",
            log_format="json",
        )
        provider = DataDrivenLogProvider(config, mock_systems_config)
        result = provider.get_failure_context(json_log_file)
        assert result is not None
        assert "Recent Messages" in result
        assert "Connection timeout" in result
        assert "Error:" in result

    def test_get_failure_context_markdown_format(self, markdown_log_file, mock_systems_config):
        """get_failure_context parses markdown log format."""
        config = AISystemConfig(
            name="test",
            log_format="markdown",
            error_patterns=["error", "failed"],
        )
        provider = DataDrivenLogProvider(config, mock_systems_config)
        result = provider.get_failure_context(markdown_log_file)
        assert result is not None
        assert "Errors Found" in result
        assert "Failed to connect" in result
        assert "Connection refused" in result

    def test_get_failure_context_unknown_format_defaults_to_text(
        self, text_log_file, mock_systems_config
    ):
        """get_failure_context defaults to text format for unknown formats."""
        config = AISystemConfig(
            name="test",
            log_format="unknown_format",
        )
        provider = DataDrivenLogProvider(config, mock_systems_config)
        result = provider.get_failure_context(text_log_file)
        assert result is not None
        assert "Session completed" in result

    def test_get_failure_context_handles_parse_error(self, temp_log_dir, mock_systems_config):
        """get_failure_context returns error message on parse failure."""
        log_path = temp_log_dir / "bad.json"
        log_path.write_text("{invalid json")

        config = AISystemConfig(name="test", log_format="json")
        provider = DataDrivenLogProvider(config, mock_systems_config)
        result = provider.get_failure_context(log_path)
        assert result is not None
        # Should contain error information
        assert "Failed to read JSON log" in result or "Failed to parse" in result

    def test_get_failure_context_jsonl_custom_lines(self, jsonl_log_file, mock_systems_config):
        """get_failure_context respects custom max_lines parameter."""
        config = AISystemConfig(
            name="test",
            log_format="jsonl",
            error_patterns=["error"],
        )
        provider = DataDrivenLogProvider(config, mock_systems_config)
        # Result should still work with max_lines parameter
        result = provider.get_failure_context(jsonl_log_file, lines=5)
        assert result is not None


class TestLogExtractionMethods:
    """Tests for log extraction behavior through public methods."""

    def test_extracts_errors_from_jsonl(self, jsonl_log_file, mock_systems_config):
        """Error extraction works through get_failure_context."""
        config = AISystemConfig(
            name="test",
            log_format="jsonl",
            error_patterns=["error", "failed"],
        )
        provider = DataDrivenLogProvider(config, mock_systems_config)
        result = provider.get_failure_context(jsonl_log_file)
        assert result is not None
        assert "Errors Found" in result
        assert "something failed" in result

    def test_extracts_permission_issues_from_jsonl(self, jsonl_log_file, mock_systems_config):
        """Permission issue extraction works through get_failure_context."""
        config = AISystemConfig(
            name="test",
            log_format="jsonl",
        )
        provider = DataDrivenLogProvider(config, mock_systems_config)
        result = provider.get_failure_context(jsonl_log_file)
        assert result is not None
        assert "Permission Issues" in result
        assert "Permission denied" in result
        assert "bypassPermissions" in result

    def test_extracts_recent_activity_from_jsonl(self, jsonl_log_file, mock_systems_config):
        """Recent activity extraction works through get_failure_context."""
        config = AISystemConfig(
            name="test",
            log_format="jsonl",
        )
        provider = DataDrivenLogProvider(config, mock_systems_config)
        result = provider.get_failure_context(jsonl_log_file)
        assert result is not None
        assert "Recent Activity" in result
        assert "Tool:" in result
        assert "Assistant:" in result

    def test_detects_completion_marker(self, jsonl_log_file, mock_systems_config):
        """Completion marker detection works through get_failure_context."""
        config = AISystemConfig(
            name="test",
            log_format="jsonl",
            completion_marker="agent-done",
        )
        provider = DataDrivenLogProvider(config, mock_systems_config)
        result = provider.get_failure_context(jsonl_log_file)
        assert result is not None
        assert "Completion:" in result
        assert "completed" in result

    def test_reports_missing_completion_marker(self, temp_log_dir, mock_systems_config):
        """Reports when completion marker is not found."""
        log_path = temp_log_dir / "test.jsonl"
        entries = [
            {"type": "user", "content": "Hello"},
            {"type": "assistant", "content": "Processing"},
        ]
        log_path.write_text("\n".join(json.dumps(e) for e in entries))

        config = AISystemConfig(
            name="test",
            log_format="jsonl",
            completion_marker="agent-done",
        )
        provider = DataDrivenLogProvider(config, mock_systems_config)
        result = provider.get_failure_context(log_path)
        assert result is not None
        assert "NOT CALLED" in result

    def test_extracts_tool_errors_from_jsonl(self, jsonl_log_file, mock_systems_config):
        """Tool error extraction works through get_failure_context."""
        config = AISystemConfig(
            name="test",
            log_format="jsonl",
        )
        provider = DataDrivenLogProvider(config, mock_systems_config)
        result = provider.get_failure_context(jsonl_log_file)
        assert result is not None
        assert "Tool error:" in result or "Access denied" in result


class TestGetLogProvider:
    """Tests for get_log_provider function."""

    def test_get_log_provider_returns_none_for_unknown_system(self):
        """get_log_provider returns None for unknown AI system."""
        with patch(
            "issue_orchestrator.infra.ai_systems_config.get_ai_systems_config"
        ) as mock_get_config:
            config = AISystemsConfig(
                systems={
                    "known-system": AISystemConfig(name="known-system"),
                },
                default_ai_system="known-system",
            )
            mock_get_config.return_value = config
            result = get_log_provider("unknown-system")
            assert result is None

    def test_get_log_provider_returns_provider_for_known_system(self):
        """get_log_provider returns provider for known system."""
        with patch(
            "issue_orchestrator.infra.ai_systems_config.get_ai_systems_config"
        ) as mock_get_config:
            config = AISystemsConfig(
                systems={
                    "test-system": AISystemConfig(name="test-system"),
                },
                default_ai_system="test-system",
            )
            mock_get_config.return_value = config
            result = get_log_provider("test-system")
            assert result is not None
            assert isinstance(result, DataDrivenLogProvider)
            assert result.ai_system == "test-system"

    def test_get_log_provider_uses_default_system(self):
        """get_log_provider uses default system when none specified."""
        with patch(
            "issue_orchestrator.infra.ai_systems_config.get_ai_systems_config"
        ) as mock_get_config:
            config = AISystemsConfig(
                systems={
                    "claude-code": AISystemConfig(name="claude-code"),
                },
                default_ai_system="claude-code",
            )
            mock_get_config.return_value = config
            result = get_log_provider(None)  # No system specified
            assert result is not None
            assert result.ai_system == "claude-code"

    def test_get_log_provider_passes_project_root(self):
        """get_log_provider passes project_root to get_ai_systems_config."""
        with patch(
            "issue_orchestrator.infra.ai_systems_config.get_ai_systems_config"
        ) as mock_get_config:
            config = AISystemsConfig(
                systems={
                    "test": AISystemConfig(name="test"),
                },
            )
            mock_get_config.return_value = config
            project_root = Path("/project")
            get_log_provider("test", project_root=project_root)
            mock_get_config.assert_called_once_with(project_root)


class TestGetFailureContextForSession:
    """Tests for get_failure_context_for_session function."""

    def test_get_failure_context_with_explicit_ai_system(self):
        """Uses explicit ai_system when provided."""
        with patch(
            "issue_orchestrator.adapters.session_log.registry.get_log_provider"
        ) as mock_get_provider, patch(
            "issue_orchestrator.infra.ai_systems_config.get_ai_systems_config"
        ) as mock_get_config:
            mock_provider = MagicMock()
            mock_provider.get_log_path.return_value = Path("/tmp/test.log")
            mock_provider.get_failure_context.return_value = "Context"
            mock_get_provider.return_value = mock_provider
            mock_get_config.return_value = MagicMock()

            result = get_failure_context_for_session(
                Path("/tmp"),
                "session1",
                ai_system="explicit-system",
            )
            mock_get_provider.assert_called_once_with("explicit-system", None)
            assert result == "Context"

    def test_get_failure_context_detects_from_terminal_output(self):
        """Detects AI system from terminal output tags."""
        with patch(
            "issue_orchestrator.adapters.session_log.registry.get_log_provider"
        ) as mock_get_provider, patch(
            "issue_orchestrator.infra.ai_systems_config.get_ai_systems_config"
        ) as mock_get_config:
            config_obj = AISystemsConfig(
                systems={
                    "claude-code": AISystemConfig(
                        name="claude-code",
                        console_tags=["claude"],
                    ),
                },
                default_ai_system="test",
            )
            mock_get_config.return_value = config_obj
            mock_provider = MagicMock()
            mock_provider.get_log_path.return_value = Path("/tmp/test.log")
            mock_provider.get_failure_context.return_value = "Context"
            mock_get_provider.return_value = mock_provider

            get_failure_context_for_session(
                Path("/tmp"),
                "session1",
                terminal_output="[CLAUDE] Session started",
            )
            # Should detect "claude" tag and call get_log_provider with "claude-code"
            mock_get_provider.assert_called_once_with("claude-code", None)

    def test_get_failure_context_detects_from_command(self):
        """Detects AI system from command."""
        with patch(
            "issue_orchestrator.adapters.session_log.registry.get_log_provider"
        ) as mock_get_provider, patch(
            "issue_orchestrator.infra.ai_systems_config.get_ai_systems_config"
        ) as mock_get_config:
            config_obj = AISystemsConfig(
                systems={
                    "codex": AISystemConfig(name="codex"),
                },
                default_ai_system="test",
            )
            mock_get_config.return_value = config_obj
            mock_provider = MagicMock()
            mock_provider.get_log_path.return_value = Path("/tmp/test.log")
            mock_provider.get_failure_context.return_value = "Context"
            mock_get_provider.return_value = mock_provider

            get_failure_context_for_session(
                Path("/tmp"),
                "session1",
                command="codex run /project",
            )
            mock_get_provider.assert_called_once_with("codex", None)

    def test_get_failure_context_uses_default_system(self):
        """Uses default system when detection fails."""
        with patch(
            "issue_orchestrator.adapters.session_log.registry.get_log_provider"
        ) as mock_get_provider, patch(
            "issue_orchestrator.infra.ai_systems_config.get_ai_systems_config"
        ) as mock_get_config:
            config_obj = AISystemsConfig(default_ai_system="default-system")
            mock_get_config.return_value = config_obj
            mock_provider = MagicMock()
            mock_provider.get_log_path.return_value = Path("/tmp/test.log")
            mock_provider.get_failure_context.return_value = "Context"
            mock_get_provider.return_value = mock_provider

            get_failure_context_for_session(
                Path("/tmp"),
                "session1",
            )
            mock_get_provider.assert_called_once_with("default-system", None)

    def test_get_failure_context_returns_none_if_no_provider(self):
        """Returns None when provider is not found."""
        with patch(
            "issue_orchestrator.adapters.session_log.registry.get_log_provider"
        ) as mock_get_provider, patch(
            "issue_orchestrator.infra.ai_systems_config.get_ai_systems_config"
        ) as mock_get_config:
            mock_get_provider.return_value = None
            mock_get_config.return_value = AISystemsConfig()

            result = get_failure_context_for_session(
                Path("/tmp"),
                "session1",
            )
            assert result is None

    def test_get_failure_context_returns_message_if_no_log_found(self):
        """Returns message when log file is not found."""
        with patch(
            "issue_orchestrator.adapters.session_log.registry.get_log_provider"
        ) as mock_get_provider, patch(
            "issue_orchestrator.infra.ai_systems_config.get_ai_systems_config"
        ) as mock_get_config:
            config_obj = AISystemsConfig(default_ai_system="test-system")
            mock_get_config.return_value = config_obj
            mock_provider = MagicMock()
            mock_provider.get_log_path.return_value = None
            mock_get_provider.return_value = mock_provider

            result = get_failure_context_for_session(
                Path("/tmp"),
                "session1",
            )
            assert result is not None
            assert "No test-system log found" in result

    def test_get_failure_context_returns_context_from_provider(self):
        """Returns failure context from provider."""
        with patch(
            "issue_orchestrator.adapters.session_log.registry.get_log_provider"
        ) as mock_get_provider, patch(
            "issue_orchestrator.infra.ai_systems_config.get_ai_systems_config"
        ) as mock_get_config:
            mock_get_config.return_value = AISystemsConfig(default_ai_system="test")
            mock_provider = MagicMock()
            mock_provider.get_log_path.return_value = Path("/tmp/test.log")
            mock_provider.get_failure_context.return_value = "Detailed failure context"
            mock_get_provider.return_value = mock_provider

            result = get_failure_context_for_session(
                Path("/tmp"),
                "session1",
            )
            assert result == "Detailed failure context"

    def test_get_failure_context_passes_project_root(self):
        """Passes project_root to get_log_provider."""
        with patch(
            "issue_orchestrator.adapters.session_log.registry.get_log_provider"
        ) as mock_get_provider, patch(
            "issue_orchestrator.infra.ai_systems_config.get_ai_systems_config"
        ) as mock_get_config:
            config_obj = AISystemsConfig(default_ai_system="test")
            mock_get_config.return_value = config_obj
            mock_provider = MagicMock()
            mock_provider.get_log_path.return_value = None
            mock_get_provider.return_value = mock_provider

            project_root = Path("/my/project")
            get_failure_context_for_session(
                Path("/tmp"),
                "session1",
                project_root=project_root,
            )
            mock_get_provider.assert_called_once_with("test", project_root)


class TestPatternResolution:
    """Tests for pattern resolution with system config."""

    def test_resolve_pattern_with_worktree_variable(self, mock_systems_config):
        """Resolves {worktree} variable in log pattern."""
        pattern = "{worktree}/.logs/test.log"
        worktree = Path("/tmp/project")
        resolved = mock_systems_config.resolve_log_pattern(pattern, worktree)
        assert "/tmp/project/.logs/test.log" in resolved

    def test_resolve_pattern_with_home_variable(self, mock_systems_config):
        """Resolves {home} variable in log pattern."""
        pattern = "{home}/.logs/test.log"
        resolved = mock_systems_config.resolve_log_pattern(pattern, Path("/tmp"))
        assert str(Path.home()) in resolved

    def test_resolve_pattern_with_escaped_worktree_variable(self, mock_systems_config):
        """Resolves {escaped_worktree} variable in log pattern."""
        pattern = "/tmp/{escaped_worktree}.log"
        worktree = Path("/home/user/my-project")
        resolved = mock_systems_config.resolve_log_pattern(pattern, worktree)
        # escaped_worktree converts / to -
        assert "home-user-my-project" in resolved or resolved.startswith("/tmp/home-user")

    def test_resolve_pattern_with_project_hash_variable(self, mock_systems_config):
        """Resolves {project_hash} variable in log pattern."""
        pattern = "/tmp/logs-{project_hash}.jsonl"
        worktree = Path("/home/user/project")
        resolved = mock_systems_config.resolve_log_pattern(pattern, worktree)
        # Should have replaced {project_hash} with MD5 hash (first 12 chars)
        assert resolved.startswith("/tmp/logs-")
        assert "{project_hash}" not in resolved
        # The MD5 hash is 12 chars, so resolved should be shorter than pattern
        assert resolved.endswith(".jsonl")

    def test_resolve_pattern_with_date_path_variable(self, mock_systems_config):
        """Resolves {date_path} variable in log pattern."""
        pattern = "/logs/{date_path}/session.log"
        resolved = mock_systems_config.resolve_log_pattern(pattern, Path("/tmp"))
        # Should have YYYY/MM/DD
        assert "/" in resolved
        parts = resolved.split("/")
        # Check format contains numbers
        assert any(part.isdigit() for part in parts)


class TestEdgeCases:
    """Tests for edge cases and error conditions."""

    def test_json_log_without_messages_field(self, temp_log_dir, mock_systems_config):
        """Handles JSON log that lacks messages field."""
        log_path = temp_log_dir / "test.json"
        log_path.write_text(json.dumps({"status": "success"}))

        config = AISystemConfig(name="test", log_format="json")
        provider = DataDrivenLogProvider(config, mock_systems_config)
        result = provider.get_failure_context(log_path)
        assert result == "No specific failure context found"

    def test_json_log_with_non_dict_data(self, temp_log_dir, mock_systems_config):
        """Handles JSON log with non-dict data."""
        log_path = temp_log_dir / "test.json"
        log_path.write_text(json.dumps(["array", "data"]))

        config = AISystemConfig(name="test", log_format="json")
        provider = DataDrivenLogProvider(config, mock_systems_config)
        result = provider.get_failure_context(log_path)
        # Should handle gracefully
        assert result is not None

    def test_markdown_log_with_custom_error_patterns(
        self, markdown_log_file, mock_systems_config
    ):
        """Uses custom error patterns in markdown logs."""
        config = AISystemConfig(
            name="test",
            log_format="markdown",
            error_patterns=["timeout", "refused"],  # Custom patterns
        )
        provider = DataDrivenLogProvider(config, mock_systems_config)
        result = provider.get_failure_context(markdown_log_file)
        assert result is not None
        assert "Connection refused" in result

    def test_jsonl_log_with_no_matching_error_patterns(self, temp_log_dir, mock_systems_config):
        """JSONL log with no entries matching error patterns."""
        log_path = temp_log_dir / "test.jsonl"
        entries = [
            {"type": "user", "content": "Hello"},
            {"type": "assistant", "content": "Hi"},
        ]
        log_path.write_text("\n".join(json.dumps(e) for e in entries))

        config = AISystemConfig(
            name="test",
            log_format="jsonl",
            error_patterns=["critical-error"],  # Pattern that won't match
        )
        provider = DataDrivenLogProvider(config, mock_systems_config)
        result = provider.get_failure_context(log_path)
        # Should still work, just without error section
        assert result is not None
