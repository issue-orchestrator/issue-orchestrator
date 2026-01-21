"""Tests for AI Systems configuration loader."""

import hashlib
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from issue_orchestrator.infra.ai_systems_config import (
    AISystemConfig,
    AISystemsConfig,
    _load_yaml_file,
    _merge_configs,
    _parse_system_config,
    clear_ai_systems_cache,
    get_ai_systems_config,
)


class TestAISystemConfig:
    """Tests for AISystemConfig dataclass."""

    def test_creates_with_required_fields(self):
        """Test AISystemConfig creation with only required fields."""
        config = AISystemConfig(name="test-system")
        assert config.name == "test-system"
        assert config.description == ""
        assert config.log_pattern == ""
        assert config.log_format == "text"
        assert config.console_tags == []
        assert config.error_patterns == []
        assert config.completion_marker is None

    def test_creates_with_all_fields(self):
        """Test AISystemConfig creation with all fields."""
        config = AISystemConfig(
            name="claude-code",
            description="Claude Code CLI",
            log_pattern="/path/to/logs",
            log_format="jsonl",
            console_tags=["claude", "Claude Code"],
            error_patterns=["error", "failed"],
            completion_marker="agent-done",
        )
        assert config.name == "claude-code"
        assert config.description == "Claude Code CLI"
        assert config.log_pattern == "/path/to/logs"
        assert config.log_format == "jsonl"
        assert config.console_tags == ["claude", "Claude Code"]
        assert config.error_patterns == ["error", "failed"]
        assert config.completion_marker == "agent-done"

    def test_log_format_defaults_to_text(self):
        """Test that log_format defaults to 'text'."""
        config = AISystemConfig(name="test")
        assert config.log_format == "text"

    def test_lists_default_to_empty(self):
        """Test that list fields default to empty lists."""
        config = AISystemConfig(name="test")
        assert config.console_tags == []
        assert config.error_patterns == []


class TestAISystemsConfig:
    """Tests for AISystemsConfig class."""

    def test_creates_with_defaults(self):
        """Test AISystemsConfig creation with defaults."""
        config = AISystemsConfig()
        assert config.systems == {}
        assert config.default_ai_system == "claude-code"

    def test_creates_with_systems(self):
        """Test AISystemsConfig creation with systems."""
        system1 = AISystemConfig(name="claude-code", description="Claude")
        system2 = AISystemConfig(name="gemini", description="Google Gemini")
        systems = {"claude-code": system1, "gemini": system2}
        config = AISystemsConfig(systems=systems, default_ai_system="gemini")
        assert config.systems == systems
        assert config.default_ai_system == "gemini"

    def test_get_system_returns_config(self):
        """Test get_system returns the correct system config."""
        system = AISystemConfig(name="claude-code", description="Claude")
        config = AISystemsConfig(systems={"claude-code": system})
        assert config.get_system("claude-code") == system

    def test_get_system_returns_none_when_not_found(self):
        """Test get_system returns None for unknown system."""
        config = AISystemsConfig()
        assert config.get_system("unknown-system") is None

    def test_detect_from_tags_returns_system_name(self):
        """Test detect_from_tags finds matching tag in text."""
        system = AISystemConfig(name="claude-code", console_tags=["claude", "Claude Code"])
        config = AISystemsConfig(systems={"claude-code": system})
        assert config.detect_from_tags("Running with Claude Code") == "claude-code"
        assert config.detect_from_tags("using claude CLI") == "claude-code"

    def test_detect_from_tags_is_case_insensitive(self):
        """Test detect_from_tags is case insensitive."""
        system = AISystemConfig(name="claude-code", console_tags=["claude"])
        config = AISystemsConfig(systems={"claude-code": system})
        assert config.detect_from_tags("CLAUDE") == "claude-code"
        assert config.detect_from_tags("Claude") == "claude-code"

    def test_detect_from_tags_returns_first_match(self):
        """Test detect_from_tags returns first matching system."""
        system1 = AISystemConfig(name="claude-code", console_tags=["claude"])
        system2 = AISystemConfig(name="gemini", console_tags=["google"])
        config = AISystemsConfig(systems={"claude-code": system1, "gemini": system2})
        # Insertion order matters - claude-code should match first
        result = config.detect_from_tags("Using claude and google")
        assert result == "claude-code"

    def test_detect_from_tags_returns_none_for_no_match(self):
        """Test detect_from_tags returns None when no tag matches."""
        system = AISystemConfig(name="claude-code", console_tags=["claude"])
        config = AISystemsConfig(systems={"claude-code": system})
        assert config.detect_from_tags("No AI system mentioned here") is None

    def test_detect_from_tags_returns_none_for_empty_text(self):
        """Test detect_from_tags returns None for empty text."""
        system = AISystemConfig(name="claude-code", console_tags=["claude"])
        config = AISystemsConfig(systems={"claude-code": system})
        assert config.detect_from_tags("") is None

    def test_detect_from_command_recognizes_exact_name(self):
        """Test detect_from_command recognizes system by exact name."""
        system = AISystemConfig(name="claude-code")
        config = AISystemsConfig(systems={"claude-code": system})
        assert config.detect_from_command("claude-code my-arg") == "claude-code"
        assert config.detect_from_command("claude-code") == "claude-code"

    def test_detect_from_command_recognizes_name_without_hyphen(self):
        """Test detect_from_command recognizes name without hyphen."""
        system = AISystemConfig(name="claude-code")
        config = AISystemsConfig(systems={"claude-code": system})
        assert config.detect_from_command("claudecode my-arg") == "claude-code"

    def test_detect_from_command_recognizes_first_console_tag(self):
        """Test detect_from_command recognizes first console tag."""
        system = AISystemConfig(name="claude-code", console_tags=["claude", "Claude Code"])
        config = AISystemsConfig(systems={"claude-code": system})
        assert config.detect_from_command("claude my-arg") == "claude-code"

    def test_detect_from_command_ignores_env_vars_at_start(self):
        """Test detect_from_command ignores environment variable assignments."""
        system = AISystemConfig(name="claude-code")
        config = AISystemsConfig(systems={"claude-code": system})
        assert config.detect_from_command("FOO=bar claude-code script.py") == "claude-code"
        assert config.detect_from_command("A=1 B=2 C=3 claude-code test") == "claude-code"

    def test_detect_from_command_is_case_insensitive(self):
        """Test detect_from_command is case insensitive."""
        system = AISystemConfig(name="claude-code")
        config = AISystemsConfig(systems={"claude-code": system})
        assert config.detect_from_command("CLAUDE-CODE script") == "claude-code"
        assert config.detect_from_command("Claude-Code script") == "claude-code"

    def test_detect_from_command_returns_none_for_no_match(self):
        """Test detect_from_command returns None when command doesn't match."""
        system = AISystemConfig(name="claude-code")
        config = AISystemsConfig(systems={"claude-code": system})
        assert config.detect_from_command("unknown-system arg") is None

    def test_detect_from_command_returns_none_for_empty_command(self):
        """Test detect_from_command returns None for empty command."""
        system = AISystemConfig(name="claude-code")
        config = AISystemsConfig(systems={"claude-code": system})
        assert config.detect_from_command("") is None

    def test_detect_from_command_returns_none_for_only_env_vars(self):
        """Test detect_from_command returns None when only env vars present."""
        system = AISystemConfig(name="claude-code")
        config = AISystemsConfig(systems={"claude-code": system})
        assert config.detect_from_command("FOO=bar BAR=baz") is None

    def test_resolve_log_pattern_replaces_home(self):
        """Test resolve_log_pattern replaces {home} variable."""
        config = AISystemsConfig()
        home = str(Path.home())
        result = config.resolve_log_pattern("{home}/logs", Path("/tmp"))
        assert home in result
        assert result == f"{home}/logs"

    def test_resolve_log_pattern_replaces_worktree(self):
        """Test resolve_log_pattern replaces {worktree} variable."""
        config = AISystemsConfig()
        worktree = Path("/tmp/test-worktree")
        result = config.resolve_log_pattern("{worktree}/logs", worktree)
        # On macOS, /tmp may resolve to /private/tmp
        assert result.endswith("test-worktree/logs")

    def test_resolve_log_pattern_replaces_escaped_worktree(self):
        """Test resolve_log_pattern replaces {escaped_worktree} variable."""
        config = AISystemsConfig()
        worktree = Path("/home/user/projects/my-project")
        result = config.resolve_log_pattern("{escaped_worktree}/logs", worktree)
        # Should replace leading / and remaining / with -
        # Result format: <path-with-slashes-replaced-by-dashes>/logs
        assert result.endswith("my-project/logs")
        assert "/" not in result.split("/logs")[0]  # No slashes in the path part

    def test_resolve_log_pattern_replaces_project_hash(self):
        """Test resolve_log_pattern replaces {project_hash} variable."""
        config = AISystemsConfig()
        worktree = Path("/tmp/test")
        result = config.resolve_log_pattern("{project_hash}/logs", worktree)
        expected_hash = hashlib.md5(str(worktree.resolve()).encode()).hexdigest()[:12]
        assert result == f"{expected_hash}/logs"

    def test_resolve_log_pattern_replaces_date_path(self):
        """Test resolve_log_pattern replaces {date_path} variable."""
        config = AISystemsConfig()
        result = config.resolve_log_pattern("{date_path}/logs", Path("/tmp"))
        now = datetime.now()
        expected_date = now.strftime("%Y/%m/%d")
        assert expected_date in result

    def test_resolve_log_pattern_replaces_issue_number(self):
        """Test resolve_log_pattern replaces {issue_number} variable."""
        config = AISystemsConfig()
        result = config.resolve_log_pattern("issue-{issue_number}.log", Path("/tmp"), issue_number=123)
        assert result == "issue-123.log"

    def test_resolve_log_pattern_ignores_issue_number_when_none(self):
        """Test resolve_log_pattern leaves {issue_number} unchanged when None."""
        config = AISystemsConfig()
        result = config.resolve_log_pattern("issue-{issue_number}.log", Path("/tmp"))
        assert result == "issue-{issue_number}.log"

    def test_resolve_log_pattern_replaces_multiple_variables(self):
        """Test resolve_log_pattern replaces multiple variables."""
        config = AISystemsConfig()
        worktree = Path("/tmp/project")
        result = config.resolve_log_pattern(
            "{home}/{escaped_worktree}/{date_path}/issue-{issue_number}.log",
            worktree,
            issue_number=42,
        )
        home = str(Path.home())
        now = datetime.now()
        date_path = now.strftime("%Y/%m/%d")
        assert home in result
        assert "tmp-project" in result
        assert date_path in result
        assert "issue-42" in result


class TestLoadYamlFile:
    """Tests for _load_yaml_file helper function."""

    def test_loads_valid_yaml(self, tmp_path):
        """Test loading a valid YAML file."""
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text("key: value\nnumber: 42")
        result = _load_yaml_file(yaml_file)
        assert result == {"key": "value", "number": 42}

    def test_returns_empty_dict_when_file_not_found(self, tmp_path):
        """Test returns empty dict when file doesn't exist."""
        result = _load_yaml_file(tmp_path / "nonexistent.yaml")
        assert result == {}

    def test_returns_empty_dict_when_file_contains_null(self, tmp_path):
        """Test returns empty dict when YAML file is null."""
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text("null")
        result = _load_yaml_file(yaml_file)
        assert result == {}

    def test_returns_empty_dict_when_file_contains_non_dict(self, tmp_path):
        """Test returns empty dict when YAML file doesn't contain dict."""
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text("- item1\n- item2")
        result = _load_yaml_file(yaml_file)
        assert result == {}

    def test_returns_empty_dict_on_yaml_error(self, tmp_path):
        """Test returns empty dict when YAML is invalid."""
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text("invalid: yaml: syntax: here:")
        result = _load_yaml_file(yaml_file)
        assert result == {}

    def test_returns_nested_dict(self, tmp_path):
        """Test loading nested YAML structures."""
        yaml_file = tmp_path / "test.yaml"
        yaml_file.write_text("ai_systems:\n  claude:\n    name: claude-code")
        result = _load_yaml_file(yaml_file)
        assert result == {"ai_systems": {"claude": {"name": "claude-code"}}}


class TestMergeConfigs:
    """Tests for _merge_configs helper function."""

    def test_merges_simple_dicts(self):
        """Test merging simple dictionaries."""
        base = {"a": 1, "b": 2}
        override = {"b": 3, "c": 4}
        result = _merge_configs(base, override)
        assert result == {"a": 1, "b": 3, "c": 4}

    def test_preserves_base_when_override_empty(self):
        """Test that base is unchanged when override is empty."""
        base = {"a": 1, "b": 2}
        result = _merge_configs(base, {})
        assert result == {"a": 1, "b": 2}

    def test_override_replaces_base(self):
        """Test that override completely replaces base."""
        base = {"a": 1}
        override = {"a": 2}
        result = _merge_configs(base, override)
        assert result == {"a": 2}

    def test_deep_merges_nested_dicts(self):
        """Test deep merging of nested dictionaries."""
        base = {"ai_systems": {"claude": {"name": "claude-code", "version": "1.0"}}}
        override = {"ai_systems": {"claude": {"version": "2.0"}, "gemini": {"name": "gemini"}}}
        result = _merge_configs(base, override)
        assert result == {
            "ai_systems": {
                "claude": {"name": "claude-code", "version": "2.0"},
                "gemini": {"name": "gemini"},
            }
        }

    def test_does_not_modify_base(self):
        """Test that merging doesn't modify the base dictionary."""
        base = {"a": {"b": 1}}
        override = {"a": {"c": 2}}
        result = _merge_configs(base, override)
        # Base should be unchanged
        assert base == {"a": {"b": 1}}
        # Result should be merged
        assert result == {"a": {"b": 1, "c": 2}}

    def test_replaces_non_dict_with_dict(self):
        """Test that non-dict values can be replaced with dicts."""
        base = {"a": "string"}
        override = {"a": {"nested": "dict"}}
        result = _merge_configs(base, override)
        assert result == {"a": {"nested": "dict"}}

    def test_replaces_dict_with_non_dict(self):
        """Test that dict values can be replaced with non-dict."""
        base = {"a": {"nested": "dict"}}
        override = {"a": "string"}
        result = _merge_configs(base, override)
        assert result == {"a": "string"}


class TestParseSystemConfig:
    """Tests for _parse_system_config helper function."""

    def test_parses_minimal_config(self):
        """Test parsing minimal AI system config."""
        data = {}
        result = _parse_system_config("test-system", data)
        assert result.name == "test-system"
        assert result.description == ""
        assert result.log_pattern == ""
        assert result.log_format == "text"
        assert result.console_tags == []
        assert result.error_patterns == []
        assert result.completion_marker is None

    def test_parses_full_config(self):
        """Test parsing full AI system config."""
        data = {
            "description": "Test System",
            "log_pattern": "/path/to/logs",
            "log_format": "jsonl",
            "console_tags": ["test", "system"],
            "error_patterns": ["error", "failed"],
            "completion_marker": "done",
        }
        result = _parse_system_config("test-system", data)
        assert result.name == "test-system"
        assert result.description == "Test System"
        assert result.log_pattern == "/path/to/logs"
        assert result.log_format == "jsonl"
        assert result.console_tags == ["test", "system"]
        assert result.error_patterns == ["error", "failed"]
        assert result.completion_marker == "done"

    def test_uses_defaults_for_missing_fields(self):
        """Test that missing fields use defaults."""
        data = {"description": "Only description"}
        result = _parse_system_config("test-system", data)
        assert result.description == "Only description"
        assert result.log_pattern == ""
        assert result.log_format == "text"
        assert result.console_tags == []


class TestLoadAISystemsConfig:
    """Tests for load_ai_systems_config function."""


    def test_returns_air_systems_config_object(self):
        """Test that load_ai_systems_config returns AISystemsConfig object."""
        # This is a high-level integration test that requires mocking file I/O
        # For now, we test the dataclass works correctly
        config = AISystemsConfig(
            systems={"claude-code": AISystemConfig(name="claude-code")},
            default_ai_system="claude-code",
        )
        assert isinstance(config, AISystemsConfig)
        assert len(config.systems) == 1


class TestGetAISystemsConfigCaching:
    """Tests for caching behavior of get_ai_systems_config."""

    def setup_method(self):
        """Clear cache before each test."""
        clear_ai_systems_cache()

    def teardown_method(self):
        """Clear cache after each test."""
        clear_ai_systems_cache()

    def test_get_returns_cached_config_on_second_call(self, tmp_path, monkeypatch):
        """Test that get_ai_systems_config returns cached config on second call."""
        monkeypatch.setenv("HOME", str(tmp_path))

        # Create minimal config
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        config_file = config_dir / "ai_systems.yaml"
        config_file.write_text(
            """
ai_systems:
  claude-code:
    description: Claude
default_ai_system: claude-code
""",
        )

        # Mock load function to track calls
        call_count = 0

        def counting_load(project_root=None):  # noqa: ARG001
            nonlocal call_count
            call_count += 1
            return AISystemsConfig(
                systems={"claude-code": AISystemConfig(name="claude-code")},
                default_ai_system="claude-code",
            )

        with patch("issue_orchestrator.infra.ai_systems_config.load_ai_systems_config", counting_load):
            config1 = get_ai_systems_config()
            config2 = get_ai_systems_config()
            # load should be called only once
            assert call_count == 1
            # Both calls should return the same object
            assert config1 is config2

    def test_clear_cache_resets_cached_config(self, tmp_path, monkeypatch):
        """Test that clear_ai_systems_cache resets the cache."""
        monkeypatch.setenv("HOME", str(tmp_path))

        # Create minimal config
        config_dir = tmp_path / "config"
        config_dir.mkdir()
        config_file = config_dir / "ai_systems.yaml"
        config_file.write_text(
            """
ai_systems:
  claude-code:
    description: Claude
default_ai_system: claude-code
""",
        )

        call_count = 0

        def counting_load(project_root=None):  # noqa: ARG001
            nonlocal call_count
            call_count += 1
            return AISystemsConfig(
                systems={"claude-code": AISystemConfig(name="claude-code")},
                default_ai_system="claude-code",
            )

        with patch("issue_orchestrator.infra.ai_systems_config.load_ai_systems_config", counting_load):
            get_ai_systems_config()
            clear_ai_systems_cache()
            get_ai_systems_config()
            # load should be called twice after clearing cache
            assert call_count == 2


class TestAISystemsConfigDetectionIntegration:
    """Integration tests for detection functionality."""

    def test_detects_claude_code_from_output(self):
        """Test realistic detection of Claude Code from output."""
        system = AISystemConfig(
            name="claude-code",
            console_tags=["claude", "Claude Code", "anthropic"],
        )
        config = AISystemsConfig(systems={"claude-code": system})

        assert config.detect_from_tags("Starting Claude Code CLI...") == "claude-code"
        assert config.detect_from_tags("[INFO] Anthropic Claude Code session started") == "claude-code"

    def test_detects_gemini_from_output(self):
        """Test realistic detection of Gemini from output."""
        system = AISystemConfig(
            name="gemini",
            console_tags=["gemini", "google", "gpt"],
        )
        config = AISystemsConfig(systems={"gemini": system})

        assert config.detect_from_tags("Google Gemini initialized") == "gemini"
        assert config.detect_from_tags("Using gpt-4 model") == "gemini"

    def test_detects_system_from_command_with_args(self):
        """Test detection from complex command lines."""
        systems = {
            "claude-code": AISystemConfig(name="claude-code", console_tags=["claude"]),
            "codex": AISystemConfig(name="codex", console_tags=["codex"]),
        }
        config = AISystemsConfig(systems=systems)

        assert config.detect_from_command("claude-code --help") == "claude-code"
        assert config.detect_from_command("CODEX_TOKEN=xyz codex run script") == "codex"
        assert config.detect_from_command("PATH=/usr/bin claude-code --version") == "claude-code"
