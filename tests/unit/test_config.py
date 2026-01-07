"""Unit tests for configuration loading and management."""

import pytest
from pathlib import Path
from issue_orchestrator.infra.config import Config
from issue_orchestrator.domain.models import AgentConfig


class TestConfig:
    """Test the Config class."""

    def test_config_creation(self):
        """Test basic Config creation with defaults."""
        config = Config()

        assert config.agents == {}
        assert config.max_concurrent_sessions == 3
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
  max_concurrent_sessions: 4
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
        assert config.max_concurrent_sessions == 4
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
        assert config.max_concurrent_sessions == 3
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
  max_concurrent_sessions: 2
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.agents == {}
        assert config.max_concurrent_sessions == 2

    def test_config_find_and_load_current_dir(self, tmp_path, monkeypatch):
        """Test finding config in current directory."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        # Config is now in .issue-orchestrator/config/default.yaml
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "default.yaml"
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
"""
        # Config is now in .issue-orchestrator/config/default.yaml
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "default.yaml"
        config_file.write_text(config_content)

        # Create a subdirectory
        subdir = tmp_path / "subdir"
        subdir.mkdir()

        monkeypatch.chdir(subdir)

        config = Config.find_and_load()

        assert "agent:parent" in config.agents

    def test_config_find_and_load_in_hidden_dir(self, tmp_path, monkeypatch):
        """Test finding config in .issue-orchestrator/config subdirectory."""
        # Config is now in .issue-orchestrator/config/default.yaml
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)

        config_content = """
agents:
  agent:hidden:
    prompt: /tmp/prompt.txt
"""
        config_file = config_dir / "default.yaml"
        config_file.write_text(config_content)

        monkeypatch.chdir(tmp_path)

        config = Config.find_and_load()

        assert "agent:hidden" in config.agents

    def test_config_find_and_load_not_found(self, tmp_path, monkeypatch):
        """Test that FileNotFoundError is raised when config not found."""
        monkeypatch.chdir(tmp_path)

        with pytest.raises(FileNotFoundError):
            Config.find_and_load()

    def test_config_find_uses_standard_location(self, tmp_path, monkeypatch):
        """Test that config is loaded from .issue-orchestrator/config/ directory."""
        # Config must be in .issue-orchestrator/config/default.yaml
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)

        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = config_dir / "default.yaml"
        config_file.write_text(config_content)

        monkeypatch.chdir(tmp_path)

        config = Config.find_and_load()

        assert "agent:test" in config.agents
        assert config.config_path == config_file

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
worktree_base: {tmp_path}
agents:
  agent:full:
    prompt: {prompt_file}
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
        assert config.worktree_base == tmp_path  # Now top-level

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
        assert config.get_filter_milestones() == ["v1.0"]

    def test_config_with_filter_milestones(self, tmp_path):
        """Test config with filter_milestones specified."""
        config_content = """
filter_milestones:
  - "M1"
  - "M2"
agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.filter_milestones == ["M1", "M2"]
        assert config.get_filter_milestones() == ["M1", "M2"]

    def test_config_filter_milestone_default(self):
        """Test default filter_milestone is None."""
        config = Config()
        assert config.filter_milestone is None
        assert config.filter_milestones == []
        assert config.get_filter_milestones() == []

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

    def test_queue_refresh_seconds_default(self):
        """Test that queue_refresh_seconds defaults to 600."""
        config = Config()
        assert config.queue_refresh_seconds == 600

    def test_github_write_verify_defaults(self):
        """Test that gh write-verify defaults are set."""
        config = Config()
        assert config.github_token is None
        assert config.github_token_env is None
        assert config.github_api_url == "https://api.github.com"
        assert config.github_http_timeout_seconds == 20.0
        assert config.github_required_scopes == []
        assert config.github_allowed_scopes == []
        assert config.gh_write_verify_timeout_seconds == 20
        assert config.gh_write_verify_initial_delay_ms == 250
        assert config.gh_write_verify_max_delay_ms == 2000
        assert config.gh_write_verify_backoff == 1.5
        assert config.gh_write_verify_jitter_ms == 0

    def test_max_issues_to_start_default(self):
        """Test that max_issues_to_start defaults to 0 (unlimited)."""
        config = Config()
        assert config.max_issues_to_start == 0

    def test_yaml_overrides_apply_to_nested_keys(self, tmp_path):
        """Test CLI overrides apply to nested YAML settings."""
        config_content = """
labels:
  in_progress: in-progress
review:
  code_review_agent: "agent:reviewer"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(
            config_file,
            overrides=[
                "labels.in_progress=claimed",
                "review.code_review_agent=agent:code-review",
                "queue_refresh_seconds=120",
                "filter_milestones=[\"M1\", \"M2\"]",
            ],
        )

        assert config.get_label_in_progress() == "claimed"
        assert config.code_review_agent == "agent:code-review"
        assert config.queue_refresh_seconds == 120
        assert config.filter_milestones == ["M1", "M2"]

    def test_github_scopes_parse_from_strings(self, tmp_path):
        config_content = """
repo: owner/repo
github_required_scopes: "repo, read:org"
github_allowed_scopes: "repo, read:org, read:user"
agents:
  agent:test:
    prompt: /tmp/prompt.txt
    worktree_base: /tmp
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.github_required_scopes == ["repo", "read:org"]
        assert config.github_allowed_scopes == ["repo", "read:org", "read:user"]

    def test_queue_refresh_seconds_from_yaml(self, tmp_path):
        """Test loading queue_refresh_seconds from YAML."""
        config_content = """
queue_refresh_seconds: 300
agents:
  agent:test:
    prompt: /tmp/prompt.txt
    worktree_base: /tmp
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.queue_refresh_seconds == 300

    def test_session_output_settings_from_yaml(self, tmp_path):
        """Test loading session output settings from YAML."""
        config_content = """
session_no_output_seconds: 180
session_no_output_tail_lines: 25
session_no_output_max_bytes: 5000
session_no_output_repeat_seconds: 300
gh_write_verify_timeout_seconds: 30
gh_write_verify_initial_delay_ms: 300
gh_write_verify_max_delay_ms: 2500
gh_write_verify_backoff: 1.8
gh_write_verify_jitter_ms: 50
agents:
  agent:test:
    prompt: /tmp/prompt.txt
    worktree_base: /tmp
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.session_no_output_seconds == 180
        assert config.session_no_output_tail_lines == 25
        assert config.session_no_output_max_bytes == 5000
        assert config.session_no_output_repeat_seconds == 300
        assert config.gh_write_verify_timeout_seconds == 30
        assert config.gh_write_verify_initial_delay_ms == 300
        assert config.gh_write_verify_max_delay_ms == 2500
        assert config.gh_write_verify_backoff == 1.8
        assert config.gh_write_verify_jitter_ms == 50

    def test_max_issues_to_start_from_yaml(self, tmp_path):
        """Test loading max_issues_to_start from YAML."""
        config_content = """
max_issues_to_start: 5
agents:
  agent:test:
    prompt: /tmp/prompt.txt
    worktree_base: /tmp
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.max_issues_to_start == 5

    def test_queue_refresh_seconds_zero_disables_auto_refresh(self, tmp_path):
        """Test that queue_refresh_seconds=0 means manual refresh only."""
        config_content = """
queue_refresh_seconds: 0
agents:
  agent:test:
    prompt: /tmp/prompt.txt
    worktree_base: /tmp
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.queue_refresh_seconds == 0

    def test_both_new_settings_from_yaml(self, tmp_path):
        """Test loading both queue_refresh_seconds and max_issues_to_start from YAML."""
        config_content = """
queue_refresh_seconds: 120
max_issues_to_start: 10
agents:
  agent:test:
    prompt: /tmp/prompt.txt
    worktree_base: /tmp
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.queue_refresh_seconds == 120
        assert config.max_issues_to_start == 10

    def test_review_workflow_defaults(self):
        """Test that review workflow options default to disabled."""
        config = Config()
        # Code review defaults (all None when not configured)
        assert config.code_review_agent is None
        assert config.code_review_label is None
        assert config.code_reviewed_label is None
        # triage review defaults (all None when not configured)
        assert config.triage_review_agent is None
        assert config.triage_review_label is None
        assert config.triage_reviewed_label is None
        assert config.triage_review_threshold == 0

    def test_review_workflow_from_yaml(self, tmp_path):
        """Test loading review workflow config from YAML."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt
    worktree_base: /tmp

review:
  code_review_agent: agent:reviewer
  code_review_label: needs-code-review
  code_reviewed_label: code-reviewed
  triage_review_agent: agent:triage
  triage_reviewed_label: triage-reviewed
  triage_review_threshold: 5
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.code_review_agent == "agent:reviewer"
        assert config.code_review_label == "needs-code-review"
        assert config.code_reviewed_label == "code-reviewed"
        assert config.triage_review_agent == "agent:triage"
        assert config.triage_reviewed_label == "triage-reviewed"
        assert config.triage_review_threshold == 5

    def test_review_workflow_partial_config(self, tmp_path):
        """Test loading review workflow with partial config (code review only)."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt
    worktree_base: /tmp

review:
  code_review_agent: agent:reviewer
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.code_review_agent == "agent:reviewer"
        assert config.code_review_label == "needs-code-review"  # default
        assert config.code_reviewed_label == "code-reviewed"  # default
        assert config.triage_review_agent is None  # not configured
        assert config.triage_review_threshold == 0  # default

    def test_review_threshold_zero_means_manual_only(self):
        """Test that triage_review_threshold=0 means manual triage review only."""
        config = Config()
        config.triage_review_agent = "agent:triage"
        config.triage_review_threshold = 0

        # Threshold of 0 means auto-trigger is disabled
        assert config.triage_review_threshold == 0

    def test_per_agent_reviewer_field(self, tmp_path):
        """Test that per-agent reviewer field is parsed from YAML."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Prompt content")

        config_content = f"""
agents:
  agent:frontend:
    prompt: {prompt_file}
    worktree_base: {tmp_path}
    reviewer: agent:web-reviewer
  agent:backend:
    prompt: {prompt_file}
    worktree_base: {tmp_path}
  agent:web-reviewer:
    prompt: {prompt_file}
    worktree_base: {tmp_path}
  agent:reviewer:
    prompt: {prompt_file}
    worktree_base: {tmp_path}

review:
  enabled: true
  default: agent:reviewer
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        # Per-agent reviewer should be set
        assert config.agents["agent:frontend"].reviewer == "agent:web-reviewer"
        # Backend has no per-agent reviewer
        assert config.agents["agent:backend"].reviewer is None
        # Default reviewer should be set
        assert config.review_enabled is True
        assert config.code_review_agent == "agent:reviewer"

    def test_get_reviewer_for_agent_with_per_agent_override(self, tmp_path):
        """Test get_reviewer_for_agent returns per-agent reviewer when set."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Prompt content")

        config_content = f"""
agents:
  agent:frontend:
    prompt: {prompt_file}
    worktree_base: {tmp_path}
    reviewer: agent:web-reviewer
  agent:backend:
    prompt: {prompt_file}
    worktree_base: {tmp_path}
  agent:web-reviewer:
    prompt: {prompt_file}
    worktree_base: {tmp_path}
  agent:reviewer:
    prompt: {prompt_file}
    worktree_base: {tmp_path}

review:
  enabled: true
  default: agent:reviewer
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        # Frontend should use per-agent reviewer
        assert config.get_reviewer_for_agent("agent:frontend") == "agent:web-reviewer"
        # Backend should use default reviewer
        assert config.get_reviewer_for_agent("agent:backend") == "agent:reviewer"
        # Unknown agent should use default reviewer
        assert config.get_reviewer_for_agent("agent:unknown") == "agent:reviewer"

    def test_get_reviewer_for_agent_no_default(self, tmp_path):
        """Test get_reviewer_for_agent returns None when no default reviewer."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Prompt content")

        config_content = f"""
agents:
  agent:frontend:
    prompt: {prompt_file}
    worktree_base: {tmp_path}
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        # No default reviewer configured
        assert config.code_review_agent is None
        assert config.get_reviewer_for_agent("agent:frontend") is None

    def test_review_enabled_and_default(self, tmp_path):
        """Test that review.enabled and review.default are parsed correctly."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Prompt content")

        config_content = f"""
agents:
  agent:test:
    prompt: {prompt_file}
    worktree_base: {tmp_path}
  agent:new-reviewer:
    prompt: {prompt_file}
    worktree_base: {tmp_path}

review:
  enabled: true
  default: agent:new-reviewer
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.review_enabled is True
        assert config.code_review_agent == "agent:new-reviewer"

    def test_validate_per_agent_reviewer_exists(self, tmp_path):
        """Test that validation catches non-existent per-agent reviewers."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Prompt content")

        config_content = f"""
agents:
  agent:frontend:
    prompt: {prompt_file}
    worktree_base: {tmp_path}
    reviewer: agent:nonexistent-reviewer
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        errors = config.validate()
        assert any("reviewer 'agent:nonexistent-reviewer' not found" in e for e in errors)

    def test_validate_default_reviewer_required_when_enabled(self, tmp_path):
        """Test that default reviewer is required when review.enabled is true."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Prompt content")

        config_content = f"""
agents:
  agent:frontend:
    prompt: {prompt_file}
    worktree_base: {tmp_path}

review:
  enabled: true
  # No default set!
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        errors = config.validate()
        assert any("no default reviewer set" in e for e in errors)

    def test_validate_no_error_when_review_disabled(self, tmp_path):
        """Test that no error when review.enabled is false (default)."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Prompt content")

        config_content = f"""
agents:
  agent:frontend:
    prompt: {prompt_file}
    worktree_base: {tmp_path}

# No review section - defaults to enabled: false
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        errors = config.validate()
        # Should not have any review-related errors
        assert not any("reviewer" in e.lower() for e in errors)


class TestCleanupConfig:
    """Tests for cleanup configuration."""

    def test_cleanup_config_defaults(self):
        """Test that cleanup config has sensible defaults."""
        config = Config()

        # with_triage defaults
        assert config.cleanup.with_triage.close_ai_session_tabs is True
        assert config.cleanup.with_triage.remove_worktrees is False

        # without_triage defaults
        assert config.cleanup.without_triage.wait_for_code_review is True
        assert config.cleanup.without_triage.close_ai_session_tabs is True
        assert config.cleanup.without_triage.remove_worktrees is False

    def test_cleanup_config_from_yaml_with_triage(self, tmp_path):
        """Test loading cleanup config for CTO workflow."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt
    worktree_base: /tmp

cleanup:
  with_triage:
    close_ai_session_tabs: false
    remove_worktrees: true
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.cleanup.with_triage.close_ai_session_tabs is False
        assert config.cleanup.with_triage.remove_worktrees is True
        # without_triage should have defaults
        assert config.cleanup.without_triage.wait_for_code_review is True
        assert config.cleanup.without_triage.close_ai_session_tabs is True

    def test_cleanup_config_from_yaml_without_triage(self, tmp_path):
        """Test loading cleanup config for non-CTO workflow."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt
    worktree_base: /tmp

cleanup:
  without_triage:
    wait_for_code_review: false
    close_ai_session_tabs: true
    remove_worktrees: true
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.cleanup.without_triage.wait_for_code_review is False
        assert config.cleanup.without_triage.close_ai_session_tabs is True
        assert config.cleanup.without_triage.remove_worktrees is True
        # with_triage should have defaults
        assert config.cleanup.with_triage.close_ai_session_tabs is True
        assert config.cleanup.with_triage.remove_worktrees is False

    def test_cleanup_config_from_yaml_both_sections(self, tmp_path):
        """Test loading cleanup config with both sections specified."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt
    worktree_base: /tmp

cleanup:
  with_triage:
    close_ai_session_tabs: true
    remove_worktrees: true
  without_triage:
    wait_for_code_review: false
    close_ai_session_tabs: false
    remove_worktrees: false
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        # with_triage
        assert config.cleanup.with_triage.close_ai_session_tabs is True
        assert config.cleanup.with_triage.remove_worktrees is True

        # without_triage
        assert config.cleanup.without_triage.wait_for_code_review is False
        assert config.cleanup.without_triage.close_ai_session_tabs is False
        assert config.cleanup.without_triage.remove_worktrees is False

    def test_cleanup_config_partial_fields_use_defaults(self, tmp_path):
        """Test that unspecified cleanup fields use defaults."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt
    worktree_base: /tmp

cleanup:
  with_triage:
    remove_worktrees: true
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        # Specified field
        assert config.cleanup.with_triage.remove_worktrees is True
        # Unspecified field should use default
        assert config.cleanup.with_triage.close_ai_session_tabs is True

    def test_cleanup_config_empty_section_uses_defaults(self, tmp_path):
        """Test that empty cleanup section uses all defaults."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt
    worktree_base: /tmp

cleanup: {}
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        # All defaults
        assert config.cleanup.with_triage.close_ai_session_tabs is True
        assert config.cleanup.with_triage.remove_worktrees is False
        assert config.cleanup.without_triage.wait_for_code_review is True
        assert config.cleanup.without_triage.close_ai_session_tabs is True
        assert config.cleanup.without_triage.remove_worktrees is False

    def test_cleanup_config_missing_section_uses_defaults(self, tmp_path):
        """Test that missing cleanup section uses all defaults."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt
    worktree_base: /tmp
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        # All defaults when section is missing
        assert config.cleanup.with_triage.close_ai_session_tabs is True
        assert config.cleanup.with_triage.remove_worktrees is False
        assert config.cleanup.without_triage.wait_for_code_review is True


class TestConfigValidation:
    """Tests for config validation at startup."""

    def test_validate_missing_prompt_file(self, tmp_path):
        """Test validation catches missing prompt files."""
        config_content = """
agents:
  agent:test:
    prompt: /nonexistent/path/prompt.md
    worktree_base: /tmp
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        errors = config.validate()

        assert any("prompt file not found" in e for e in errors)

    def test_validate_worktree_base_resolved_to_absolute(self, tmp_path):
        """Test that relative worktree_base is resolved to absolute path."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("# Test prompt")

        config_content = f"""
worktree_base: ./worktrees
agents:
  agent:test:
    prompt: {prompt_file}
"""
        # Config must be at <repo>/.issue-orchestrator/config/<name>.yaml
        # so repo_root is correctly calculated (3 levels up)
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "default.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        # Relative path should be resolved to absolute (top-level worktree_base)
        assert config.worktree_base.is_absolute()
        assert str(config.worktree_base).startswith(str(tmp_path))

    def test_validate_worktree_base_created_if_missing(self, tmp_path):
        """Test that worktree_base directory is created during load."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("# Test prompt")
        worktree_dir = tmp_path / "new-worktrees"

        config_content = f"""
worktree_base: {worktree_dir}
agents:
  agent:test:
    prompt: {prompt_file}
"""
        # Config must be at <repo>/.issue-orchestrator/config/<name>.yaml
        config_dir = tmp_path / ".issue-orchestrator" / "config"
        config_dir.mkdir(parents=True)
        config_file = config_dir / "default.yaml"
        config_file.write_text(config_content)

        # Directory doesn't exist yet
        assert not worktree_dir.exists()

        config = Config.load(config_file)

        # Directory should be created (top-level worktree_base)
        assert worktree_dir.exists()
        assert worktree_dir.is_dir()
        assert config.worktree_base == worktree_dir

    def test_validate_invalid_review_agent_reference(self, tmp_path):
        """Test validation catches invalid code_review_agent reference."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("# Test prompt")
        worktree_dir = tmp_path / "worktrees"
        worktree_dir.mkdir()

        config_content = f"""
agents:
  agent:test:
    prompt: {prompt_file}
    worktree_base: {worktree_dir}

review:
  enabled: true
  default: agent:nonexistent
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        errors = config.validate()

        assert any("review.default 'agent:nonexistent' not found" in e for e in errors)

    def test_validate_valid_config_returns_empty(self, tmp_path):
        """Test that valid config returns no errors."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("# Test prompt")
        worktree_dir = tmp_path / "worktrees"
        worktree_dir.mkdir()

        config_content = f"""
agents:
  agent:test:
    prompt: {prompt_file}
    worktree_base: {worktree_dir}
    model: haiku
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        errors = config.validate()

        assert errors == []

    def test_validate_or_raise_raises_on_errors(self, tmp_path):
        """Test validate_or_raise raises ValueError with all errors."""
        config_content = """
agents:
  agent:test:
    prompt: /nonexistent/prompt.md
    worktree_base: /tmp
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        import pytest
        with pytest.raises(ValueError) as exc_info:
            config.validate_or_raise()

        assert "Configuration errors" in str(exc_info.value)
        assert "prompt file not found" in str(exc_info.value)


class TestE2EPRLabelsConfig:
    """Tests for e2e_pr_labels configuration."""

    def test_e2e_pr_labels_defaults_to_empty_list(self):
        """e2e_pr_labels should default to empty list."""
        config = Config()
        assert config.e2e_pr_labels == []

    def test_e2e_pr_labels_loaded_from_yaml(self, tmp_path):
        """e2e_pr_labels should be loaded correctly from YAML."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt
    worktree_base: /tmp

e2e_pr_labels:
  - test-data
  - e2e-test
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.e2e_pr_labels == ["test-data", "e2e-test"]

    def test_e2e_pr_labels_inline_yaml_syntax(self, tmp_path):
        """e2e_pr_labels should work with inline YAML list syntax."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt
    worktree_base: /tmp

e2e_pr_labels: ["test-data"]
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.e2e_pr_labels == ["test-data"]

    def test_e2e_pr_labels_included_in_to_event_dict(self):
        """e2e_pr_labels should be included in to_event_dict output."""
        config = Config()
        config.e2e_pr_labels = ["test-data", "cleanup"]

        result = config.to_event_dict()

        assert result["e2e_pr_labels"] == ["test-data", "cleanup"]

    def test_e2e_pr_labels_not_specified_defaults_to_empty(self, tmp_path):
        """e2e_pr_labels should default to empty list when not in YAML."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt
    worktree_base: /tmp
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.e2e_pr_labels == []
