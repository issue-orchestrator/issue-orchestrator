"""Unit tests for configuration loading and management."""

import pytest
from pathlib import Path
from issue_orchestrator.config import Config
from issue_orchestrator.models import AgentConfig


class TestConfig:
    """Test the Config class."""

    def test_config_creation(self):
        """Test basic Config creation with defaults."""
        config = Config()

        assert config.agents == {}
        assert config.max_sessions == 3
        assert config.session_timeout_minutes == 45
        assert config.label_in_progress == "in-progress"
        assert config.label_blocked == "blocked"
        assert config.label_needs_human == "needs-human"
        assert config.repo is None

    def test_config_load_from_yaml(self, mock_config_yaml, tmp_path):
        """Test loading config from YAML file."""
        # Create temporary prompt files
        prompt_web = tmp_path / "web_prompt.txt"
        prompt_web.write_text("Web prompt content")

        prompt_mobile = tmp_path / "mobile_prompt.txt"
        prompt_mobile.write_text("Mobile prompt content")

        # Create config YAML with absolute paths
        config_content = f"""
agents:
  agent:web:
    prompt: {prompt_web}
    worktree_base: {tmp_path}
    model: sonnet
    timeout_minutes: 45
  agent:mobile:
    prompt: {prompt_mobile}
    worktree_base: {tmp_path}
    model: haiku
    timeout_minutes: 60

concurrency:
  max_sessions: 4
  session_timeout_minutes: 60

labels:
  in_progress: working
  blocked: blocked-on
  needs_human: needs-review

repo: owner/repo
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        # Check agents
        assert len(config.agents) == 2
        assert "agent:web" in config.agents
        assert "agent:mobile" in config.agents

        web_config = config.agents["agent:web"]
        assert web_config.model == "sonnet"
        assert web_config.timeout_minutes == 45
        assert web_config.prompt_path == prompt_web

        mobile_config = config.agents["agent:mobile"]
        assert mobile_config.model == "haiku"
        assert mobile_config.timeout_minutes == 60
        assert mobile_config.prompt_path == prompt_mobile

        # Check concurrency settings
        assert config.max_sessions == 4
        assert config.session_timeout_minutes == 60

        # Check labels
        assert config.label_in_progress == "working"
        assert config.label_blocked == "blocked-on"
        assert config.label_needs_human == "needs-review"

        # Check repo
        assert config.repo == "owner/repo"

    def test_config_load_with_defaults(self, tmp_path):
        """Test loading config with minimal YAML uses defaults."""
        config_content = """
agents:
  agent:simple:
    prompt: /path/to/prompt.txt
    worktree_base: /tmp
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        # Check that defaults were applied
        simple_config = config.agents["agent:simple"]
        assert simple_config.model == "sonnet"
        assert simple_config.timeout_minutes == 45

        # Global defaults
        assert config.max_sessions == 3
        assert config.session_timeout_minutes == 45
        assert config.label_in_progress == "in-progress"
        assert config.label_blocked == "blocked"
        assert config.label_needs_human == "needs-human"

    def test_config_load_file_not_found(self, tmp_path):
        """Test that FileNotFoundError is raised for missing config."""
        config_file = tmp_path / "nonexistent.yaml"

        with pytest.raises(FileNotFoundError):
            Config.load(config_file)

    def test_config_load_invalid_yaml(self, tmp_path):
        """Test that invalid YAML raises an error."""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text("{ invalid yaml [ : }")

        with pytest.raises(Exception):  # YAML parse error
            Config.load(config_file)

    def test_config_empty_agents(self, tmp_path):
        """Test loading config with no agents defined."""
        config_content = """
concurrency:
  max_sessions: 2
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.agents == {}
        assert config.max_sessions == 2

    def test_config_find_and_load_current_dir(self, tmp_path, monkeypatch):
        """Test finding config in current directory."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt
    worktree_base: /tmp
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        monkeypatch.chdir(tmp_path)

        config = Config.find_and_load()

        assert "agent:test" in config.agents

    def test_config_find_and_load_parent_dir(self, tmp_path, monkeypatch):
        """Test finding config in parent directory."""
        config_content = """
agents:
  agent:parent:
    prompt: /tmp/prompt.txt
    worktree_base: /tmp
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        # Create a subdirectory
        subdir = tmp_path / "subdir"
        subdir.mkdir()

        monkeypatch.chdir(subdir)

        config = Config.find_and_load()

        assert "agent:parent" in config.agents

    def test_config_find_and_load_in_hidden_dir(self, tmp_path, monkeypatch):
        """Test finding config in .issue-orchestrator subdirectory."""
        hidden_dir = tmp_path / ".issue-orchestrator"
        hidden_dir.mkdir()

        config_content = """
agents:
  agent:hidden:
    prompt: /tmp/prompt.txt
    worktree_base: /tmp
"""
        config_file = hidden_dir / "config.yaml"
        config_file.write_text(config_content)

        monkeypatch.chdir(tmp_path)

        config = Config.find_and_load()

        assert "agent:hidden" in config.agents

    def test_config_find_and_load_not_found(self, tmp_path, monkeypatch):
        """Test that FileNotFoundError is raised when config not found."""
        monkeypatch.chdir(tmp_path)

        with pytest.raises(FileNotFoundError):
            Config.find_and_load()

    def test_config_find_prefers_root_over_hidden(self, tmp_path, monkeypatch):
        """Test that root config is preferred over hidden dir config."""
        # Create root config
        root_config_content = """
agents:
  agent:root:
    prompt: /tmp/prompt.txt
    worktree_base: /tmp
"""
        root_config_file = tmp_path / ".issue-orchestrator.yaml"
        root_config_file.write_text(root_config_content)

        # Create hidden dir config
        hidden_dir = tmp_path / ".issue-orchestrator"
        hidden_dir.mkdir()

        hidden_config_content = """
agents:
  agent:hidden:
    prompt: /tmp/prompt.txt
    worktree_base: /tmp
"""
        hidden_config_file = hidden_dir / "config.yaml"
        hidden_config_file.write_text(hidden_config_content)

        monkeypatch.chdir(tmp_path)

        config = Config.find_and_load()

        # Should find the root config first
        assert "agent:root" in config.agents

    def test_config_with_custom_repo(self, tmp_path):
        """Test config with custom repo specified."""
        config_content = """
repo: owner/private-repo
agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.repo == "owner/private-repo"

    def test_config_agent_with_all_fields(self, tmp_path):
        """Test agent config with all fields specified."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Prompt")

        config_content = f"""
agents:
  agent:full:
    prompt: {prompt_file}
    worktree_base: {tmp_path}
    model: opus
    timeout_minutes: 120
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        agent = config.agents["agent:full"]
        assert agent.model == "opus"
        assert agent.timeout_minutes == 120
        assert agent.prompt_path == prompt_file
        assert agent.worktree_base == tmp_path

    def test_config_state_file_default(self):
        """Test default state file path."""
        config = Config()
        assert config.state_file == Path(".issue-orchestrator/state.json")

    def test_config_multiple_agents(self, tmp_path):
        """Test loading config with multiple agents."""
        prompt1 = tmp_path / "prompt1.txt"
        prompt1.write_text("Prompt 1")
        prompt2 = tmp_path / "prompt2.txt"
        prompt2.write_text("Prompt 2")
        prompt3 = tmp_path / "prompt3.txt"
        prompt3.write_text("Prompt 3")

        config_content = f"""
agents:
  agent:web:
    prompt: {prompt1}
    worktree_base: {tmp_path}
  agent:mobile:
    prompt: {prompt2}
    worktree_base: {tmp_path}
  agent:backend:
    prompt: {prompt3}
    worktree_base: {tmp_path}
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert len(config.agents) == 3
        assert all(
            agent in config.agents
            for agent in ["agent:web", "agent:mobile", "agent:backend"]
        )

    def test_config_with_filter_milestone(self, tmp_path):
        """Test config with filter_milestone specified."""
        config_content = """
filter_milestone: "v1.0"
agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.filter_milestone == "v1.0"

    def test_config_filter_milestone_default(self):
        """Test default filter_milestone is None."""
        config = Config()
        assert config.filter_milestone is None

    def test_label_prefix_not_configured(self):
        """Test that labels are not prefixed when label_prefix is not set."""
        config = Config()

        assert config.label_prefix is None
        assert config.get_label_in_progress() == "in-progress"
        assert config.get_label_blocked() == "blocked"
        assert config.get_label_needs_human() == "needs-human"

    def test_label_prefix_configured(self, tmp_path):
        """Test that labels are prefixed when label_prefix is set."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt
    worktree_base: /tmp

labels:
  prefix: bot
  in_progress: working
  blocked: blocked-on
  needs_human: needs-review
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.label_prefix == "bot"
        assert config.label_in_progress == "working"
        assert config.label_blocked == "blocked-on"
        assert config.label_needs_human == "needs-review"

        # Test prefixed versions
        assert config.get_label_in_progress() == "bot:working"
        assert config.get_label_blocked() == "bot:blocked-on"
        assert config.get_label_needs_human() == "bot:needs-review"

    def test_prefixed_label_helper(self):
        """Test the prefixed_label helper method."""
        config = Config()

        # Without prefix
        assert config.prefixed_label("test-label") == "test-label"

        # With prefix
        config.label_prefix = "bot"
        assert config.prefixed_label("test-label") == "bot:test-label"
        assert config.prefixed_label("another") == "bot:another"

    def test_label_prefix_with_defaults(self, tmp_path):
        """Test label prefix with default label names."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt
    worktree_base: /tmp

labels:
  prefix: orchestrator
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.label_prefix == "orchestrator"
        assert config.label_in_progress == "in-progress"
        assert config.label_blocked == "blocked"
        assert config.label_needs_human == "needs-human"

        # Test prefixed versions with defaults
        assert config.get_label_in_progress() == "orchestrator:in-progress"
        assert config.get_label_blocked() == "orchestrator:blocked"
        assert config.get_label_needs_human() == "orchestrator:needs-human"

    def test_agent_repo_root_override(self, tmp_path):
        """Test per-agent repo_root configuration."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Prompt")
        backend_repo = tmp_path / "backend-repo"
        backend_repo.mkdir()

        config_content = f"""
agents:
  agent:backend:
    prompt: {prompt_file}
    worktree_base: {tmp_path}
    repo_root: {backend_repo}
  agent:frontend:
    prompt: {prompt_file}
    worktree_base: {tmp_path}
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        # Backend agent should have custom repo_root
        backend_agent = config.agents["agent:backend"]
        assert backend_agent.repo_root == backend_repo

        # Frontend agent should have None (uses global repo_root)
        frontend_agent = config.agents["agent:frontend"]
        assert frontend_agent.repo_root is None

    def test_agent_repo_root_not_specified(self, tmp_path):
        """Test that repo_root defaults to None when not specified."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt
    worktree_base: /tmp
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        agent = config.agents["agent:test"]
        assert agent.repo_root is None
