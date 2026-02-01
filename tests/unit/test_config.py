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
repo:
  name: owner/repo

worktrees:
  base: {tmp_path}

agents:
  agent:web:
    prompt: {prompt_web}
    model: sonnet
    timeout_minutes: 45
  agent:mobile:
    prompt: {prompt_mobile}
    model: haiku
    timeout_minutes: 60

execution:
  concurrency:
    max_concurrent_sessions: 4
    session_timeout_minutes: 60

labels:
  in_progress: working
  blocked: blocked-on
  needs_human: needs-review
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

    def test_worktree_branch_on_recreate_default(self, tmp_path):
        """Default worktree_branch_on_recreate should be delete."""
        prompt = tmp_path / "prompt.md"
        prompt.write_text("Prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(f"""
agents:
  agent:web:
    prompt: {prompt}
    model: sonnet
worktrees:
  base: {worktree_base}
""")

        config = Config.load(config_file)

        assert config.worktree_branch_on_recreate == "delete"

    def test_worktree_branch_on_recreate_configured(self, tmp_path):
        """Config can set worktree_branch_on_recreate."""
        prompt = tmp_path / "prompt.md"
        prompt.write_text("Prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(f"""
agents:
  agent:web:
    prompt: {prompt}
    model: sonnet
worktrees:
  base: {worktree_base}
  worktree_branch_on_recreate: create_new_branch
""")

        config = Config.load(config_file)

        assert config.worktree_branch_on_recreate == "create_new_branch"

    def test_worktree_branch_on_recreate_invalid(self, tmp_path):
        """Invalid worktree_branch_on_recreate value should fail validation."""
        prompt = tmp_path / "prompt.md"
        prompt.write_text("Prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(f"""
agents:
  agent:web:
    prompt: {prompt}
    model: sonnet
worktrees:
  base: {worktree_base}
  worktree_branch_on_recreate: nope
""")

        config = Config.load(config_file)

        errors = config.validate()
        assert any("worktree_branch_on_recreate" in err for err in errors)

    def test_allow_no_verify_dry_run_preflight_default(self, tmp_path):
        """Default allow_no_verify_dry_run_preflight should be True."""
        prompt = tmp_path / "prompt.md"
        prompt.write_text("Prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(f"""
agents:
  agent:web:
    prompt: {prompt}
    model: sonnet
worktrees:
  base: {worktree_base}
""")

        config = Config.load(config_file)

        assert config.allow_no_verify_dry_run_preflight is True

    def test_allow_no_verify_dry_run_preflight_configured(self, tmp_path):
        """Config can disable allow_no_verify_dry_run_preflight."""
        prompt = tmp_path / "prompt.md"
        prompt.write_text("Prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(f"""
agents:
  agent:web:
    prompt: {prompt}
    model: sonnet
worktrees:
  base: {worktree_base}
  allow_no_verify_dry_run_preflight: false
""")

        config = Config.load(config_file)

        assert config.allow_no_verify_dry_run_preflight is False

    def test_config_load_with_defaults(self, tmp_path):
        """Test loading config with minimal YAML uses defaults."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:simple:
    prompt: /path/to/prompt.txt
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
execution:
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
repo:
  name: owner/private-repo

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
worktrees:
  base: {tmp_path}

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
worktrees:
  base: {tmp_path}

agents:
  agent:web:
    prompt: {prompt1}
  agent:mobile:
    prompt: {prompt2}
  agent:backend:
    prompt: {prompt3}
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
        """Test config with filtering.milestone specified."""
        config_content = """
filtering:
  milestone: "v1.0"
agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.filtering.milestone == "v1.0"
        assert config.get_filter_milestones() == ["v1.0"]

    def test_config_with_filter_milestones(self, tmp_path):
        """Test config with filtering.milestones specified."""
        config_content = """
filtering:
  milestones:
    - "M1"
    - "M2"
agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.filtering.milestones == ["M1", "M2"]
        assert config.get_filter_milestones() == ["M1", "M2"]

    def test_config_filter_milestone_default(self):
        """Test default filtering.milestone is None."""
        config = Config()
        assert config.filtering.milestone is None
        assert config.filtering.milestones == []
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
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt

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
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt

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

    def test_github_cache_ttl_seconds_default(self):
        """Test that github_cache_ttl_seconds defaults to 300."""
        config = Config()
        assert config.github_cache_ttl_seconds == 300

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
        """Test that filtering.max_to_start defaults to 0 (unlimited)."""
        config = Config()
        assert config.filtering.max_to_start == 0

    def test_yaml_overrides_apply_to_nested_keys(self, tmp_path):
        """Test CLI overrides apply to nested YAML settings."""
        config_content = """
labels:
  in_progress: in-progress
review:
  default: "agent:reviewer"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(
            config_file,
            overrides=[
                "labels.in_progress=claimed",
                "review.default=agent:code-review",
                "ui.queue_refresh_seconds=120",
                "filtering.milestones=[\"M1\", \"M2\"]",
            ],
        )

        assert config.get_label_in_progress() == "claimed"
        assert config.code_review_agent == "agent:code-review"
        assert config.queue_refresh_seconds == 120
        assert config.filtering.milestones == ["M1", "M2"]

    def test_github_scopes_parse_from_strings(self, tmp_path):
        config_content = """
repo:
  name: owner/repo
  github:
    required_scopes: "repo, read:org"
    allowed_scopes: "repo, read:org, read:user"

worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.github_required_scopes == ["repo", "read:org"]
        assert config.github_allowed_scopes == ["repo", "read:org", "read:user"]

    def test_queue_refresh_seconds_from_yaml(self, tmp_path):
        """Test loading queue_refresh_seconds from YAML."""
        config_content = """
ui:
  queue_refresh_seconds: 300

worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.queue_refresh_seconds == 300

    def test_github_cache_ttl_seconds_from_yaml(self, tmp_path):
        """Test loading github_cache_ttl_seconds from YAML."""
        config_content = """
repo:
  github:
    cache_ttl_seconds: 120

worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.github_cache_ttl_seconds == 120

    def test_session_output_settings_from_yaml(self, tmp_path):
        """Test loading session output settings from YAML."""
        config_content = """
observability:
  session_no_output_seconds: 180
  session_no_output_tail_lines: 25
  session_no_output_max_bytes: 5000
  session_no_output_repeat_seconds: 300

repo:
  github:
    write_verify:
      timeout_seconds: 30
      initial_delay_ms: 300
      max_delay_ms: 2500
      backoff: 1.8
      jitter_ms: 50

worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt
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
        """Test loading filtering.max_to_start from YAML."""
        config_content = """
filtering:
  max_to_start: 5

worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.filtering.max_to_start == 5

    def test_queue_refresh_seconds_zero_disables_auto_refresh(self, tmp_path):
        """Test that queue_refresh_seconds=0 means manual refresh only."""
        config_content = """
ui:
  queue_refresh_seconds: 0

worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.queue_refresh_seconds == 0

    def test_both_new_settings_from_yaml(self, tmp_path):
        """Test loading both queue_refresh_seconds and filtering.max_to_start from YAML."""
        config_content = """
ui:
  queue_refresh_seconds: 120
filtering:
  max_to_start: 10

worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.queue_refresh_seconds == 120
        assert config.filtering.max_to_start == 10

    def test_review_workflow_defaults(self):
        """Test that review workflow options default to disabled."""
        config = Config()
        # Code review defaults (all None when not configured)
        assert config.code_review_agent is None
        assert config.code_review_label is None
        assert config.code_reviewed_label is None
        assert config.review_exchange_mode == "via-draft-pr"
        assert config.review_exchange_coder is None
        assert config.review_exchange_reviewer is None
        assert config.review_exchange_probe_schedule == "daily"
        assert config.review_exchange_probe_interval_days == 1
        assert config.review_exchange_max_rounds == 10
        assert config.review_exchange_max_no_progress == 2
        assert config.review_exchange_require_validation is True
        assert config.review_keep_current_approach_label == "reviewer-keep-current-approach"
        # triage review defaults (all None when not configured)
        assert config.triage_review_agent is None
        assert config.triage_review_label is None
        assert config.triage_reviewed_label is None
        assert config.triage_review_threshold == 0

    def test_goal_pilot_defaults(self):
        """Goal Pilot defaults to disabled with journeys-only approvals."""
        config = Config()
        assert config.goal_pilot.enabled is False
        assert config.goal_pilot.agent is None
        assert config.goal_pilot.approval_policy == "journeys_only"
        assert config.goal_pilot.approval_batch_size == 10
        assert config.goal_pilot.approval_batch_window_minutes == 60

    def test_goal_pilot_from_yaml(self, tmp_path):
        """Test loading goal pilot config from YAML."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:goal-pilot:
    prompt: /tmp/prompt.txt

goal_pilot:
  enabled: true
  agent: agent:goal-pilot
  approval_policy: batch
  approval_batch_size: 20
  approval_batch_window_minutes: 120
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.goal_pilot.enabled is True
        assert config.goal_pilot.agent == "agent:goal-pilot"
        assert config.goal_pilot.approval_policy == "batch"
        assert config.goal_pilot.approval_batch_size == 20
        assert config.goal_pilot.approval_batch_window_minutes == 120

    def test_goal_pilot_requires_agent_when_enabled(self, tmp_path):
        """Goal Pilot enabled requires a valid agent."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt

goal_pilot:
  enabled: true
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        errors = config.validate()

        assert any("goal_pilot.enabled is true" in e for e in errors)

    def test_review_workflow_from_yaml(self, tmp_path):
        """Test loading review workflow config from YAML."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt
  agent:coder:
    prompt: /tmp/prompt.txt
  agent:reviewer:
    prompt: /tmp/prompt.txt
  agent:triage:
    prompt: /tmp/prompt.txt

review:
  enabled: true
  default: agent:reviewer
  code_review_label: needs-code-review
  code_reviewed_label: code-reviewed
  exchange:
    mode: via-mcp
    agent_pair:
      coder: agent:coder
      reviewer: agent:reviewer
    probe:
      schedule: interval
      interval_days: 2
    loop:
      max_rounds: 6
      max_no_progress: 1
      require_validation: false
  keep_current_approach_label: reviewer-keep-current-approach
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
        assert config.review_exchange_mode == "via-mcp"
        assert config.review_exchange_coder == "agent:coder"
        assert config.review_exchange_reviewer == "agent:reviewer"
        assert config.review_exchange_probe_schedule == "interval"
        assert config.review_exchange_probe_interval_days == 2
        assert config.review_exchange_max_rounds == 6
        assert config.review_exchange_max_no_progress == 1
        assert config.review_exchange_require_validation is False
        assert config.review_keep_current_approach_label == "reviewer-keep-current-approach"
        assert config.triage_review_agent == "agent:triage"
        assert config.triage_reviewed_label == "triage-reviewed"
        assert config.triage_review_threshold == 5

    def test_review_workflow_partial_config(self, tmp_path):
        """Test loading review workflow with partial config (code review only)."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt

review:
  enabled: true
  default: agent:reviewer
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
worktrees:
  base: {tmp_path}

agents:
  agent:frontend:
    prompt: {prompt_file}
    reviewer: agent:web-reviewer
  agent:backend:
    prompt: {prompt_file}
  agent:web-reviewer:
    prompt: {prompt_file}
  agent:reviewer:
    prompt: {prompt_file}

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
worktrees:
  base: {tmp_path}

agents:
  agent:frontend:
    prompt: {prompt_file}
    reviewer: agent:web-reviewer
  agent:backend:
    prompt: {prompt_file}
  agent:web-reviewer:
    prompt: {prompt_file}
  agent:reviewer:
    prompt: {prompt_file}

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
worktrees:
  base: {tmp_path}

agents:
  agent:frontend:
    prompt: {prompt_file}
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
worktrees:
  base: {tmp_path}

agents:
  agent:test:
    prompt: {prompt_file}
  agent:new-reviewer:
    prompt: {prompt_file}

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
worktrees:
  base: {tmp_path}

agents:
  agent:frontend:
    prompt: {prompt_file}
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
worktrees:
  base: {tmp_path}

agents:
  agent:frontend:
    prompt: {prompt_file}

review:
  enabled: true
  # No default set!
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        errors = config.validate()
        assert any("no default reviewer set" in e for e in errors)

    def test_review_exchange_requires_agent_pair_for_mcp(self, tmp_path):
        """Test that via-mcp requires an agent_pair."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Prompt content")

        config_content = f"""
worktrees:
  base: {tmp_path}

agents:
  agent:reviewer:
    prompt: {prompt_file}

review:
  enabled: true
  default: agent:reviewer
  exchange:
    mode: via-mcp
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        errors = config.validate()
        assert any("review.exchange.agent_pair" in e for e in errors)

    def test_review_exchange_validates_supported_pair(self, tmp_path):
        """Test via-mcp requires a supported (coder, reviewer) system pair."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Prompt content")

        config_content = f"""
worktrees:
  base: {tmp_path}

agents:
  agent:coder:
    prompt: {prompt_file}
    ai_system: gemini
  agent:reviewer:
    prompt: {prompt_file}
    ai_system: claude-code

review:
  enabled: true
  default: agent:reviewer
  exchange:
    mode: via-mcp
    agent_pair:
      coder: agent:coder
      reviewer: agent:reviewer
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        errors = config.validate()
        assert any("not supported" in e for e in errors)

    def test_review_exchange_probe_invalid_schedule(self, tmp_path):
        """Test invalid probe schedule fails validation."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Prompt content")

        config_content = f"""
worktrees:
  base: {tmp_path}

agents:
  agent:coder:
    prompt: {prompt_file}
  agent:reviewer:
    prompt: {prompt_file}

review:
  enabled: true
  default: agent:reviewer
  exchange:
    mode: via-mcp
    agent_pair:
      coder: agent:coder
      reviewer: agent:reviewer
    probe:
      schedule: sometimes
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        errors = config.validate()
        assert any("probe.schedule" in e for e in errors)

    def test_validate_no_error_when_review_disabled(self, tmp_path):
        """Test that no error when review.enabled is false (default)."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("Prompt content")

        config_content = f"""
worktrees:
  base: {tmp_path}

agents:
  agent:frontend:
    prompt: {prompt_file}

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
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt

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
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt

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
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt

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
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt

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
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt

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
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt
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
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /nonexistent/path/prompt.md
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
worktrees:
  base: ./worktrees

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

        # Relative path should be resolved to absolute (worktrees.base)
        assert config.worktree_base.is_absolute()
        assert str(config.worktree_base).startswith(str(tmp_path))

    def test_validate_worktree_base_created_if_missing(self, tmp_path):
        """Test that worktree_base directory is created during load."""
        prompt_file = tmp_path / "prompt.md"
        prompt_file.write_text("# Test prompt")
        worktree_dir = tmp_path / "new-worktrees"

        config_content = f"""
worktrees:
  base: {worktree_dir}

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

        # Directory should be created (worktrees.base)
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
worktrees:
  base: {worktree_dir}

agents:
  agent:test:
    prompt: {prompt_file}

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
worktrees:
  base: {worktree_dir}

default_agent:
  provider: claude-code

agents:
  agent:test:
    prompt: {prompt_file}
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
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /nonexistent/prompt.md
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
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt

e2e:
  pr_labels:
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
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt

e2e:
  pr_labels: ["test-data"]
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

        assert result["e2e"]["pr_labels"] == ["test-data", "cleanup"]

    def test_e2e_pr_labels_not_specified_defaults_to_empty(self, tmp_path):
        """e2e_pr_labels should default to empty list when not in YAML."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.e2e_pr_labels == []


class TestE2EStopOnFirstFailureConfig:
    """Tests for e2e.stop_on_first_failure configuration."""

    def test_stop_on_first_failure_defaults_to_false(self):
        """stop_on_first_failure should default to False."""
        config = Config()
        assert config.e2e.stop_on_first_failure is False

    def test_stop_on_first_failure_true_from_yaml(self, tmp_path):
        """stop_on_first_failure=true should be loaded correctly."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt

e2e:
  stop_on_first_failure: true
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.e2e.stop_on_first_failure is True

    def test_stop_on_first_failure_false_from_yaml(self, tmp_path):
        """stop_on_first_failure=false should be loaded correctly."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt

e2e:
  stop_on_first_failure: false
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.e2e.stop_on_first_failure is False

    def test_stop_on_first_failure_not_specified_defaults_false(self, tmp_path):
        """stop_on_first_failure should default to False when not in YAML."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt

e2e:
  enabled: true
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.e2e.stop_on_first_failure is False


class TestE2EFlakeConfig:
    """Tests for e2e flake detection configuration."""

    def test_flake_threshold_defaults_to_20(self):
        """flake_threshold should default to 20 (flip rate percentage)."""
        config = Config()
        assert config.e2e.flake_threshold == 20

    def test_flake_window_runs_defaults_to_10(self):
        """flake_window_runs should default to 10."""
        config = Config()
        assert config.e2e.flake_window_runs == 10

    def test_flake_config_from_yaml(self, tmp_path):
        """Flake settings should be loaded from YAML."""
        config_content = """
worktrees:
  base: /tmp/worktrees
  repo_root: /tmp/repo

e2e:
  flake_threshold: 5
  flake_window_runs: 20
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.e2e.flake_threshold == 5
        assert config.e2e.flake_window_runs == 20


class TestTriageConfig:
    """Tests for triage issue configuration."""

    def test_triage_config_defaults(self):
        """TriageConfig should have sensible defaults."""
        config = Config()

        assert config.triage.inherit_labels == []
        assert config.triage.explicit_labels == []
        assert config.triage.milestone_strategy.inherit_from_issues == "latest"
        assert config.triage.milestone_strategy.explicit is None
        assert config.triage.priority is None

    def test_triage_config_from_yaml(self, tmp_path):
        """Test loading triage config from YAML."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt

triage:
  inherit_labels:
    - "io-e2e-test-data"
    - "team:backend"
  explicit_labels:
    - "needs-batch-review"
  milestone_strategy:
    inherit_from_issues: earliest
  priority: "P1"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.triage.inherit_labels == ["io-e2e-test-data", "team:backend"]
        assert config.triage.explicit_labels == ["needs-batch-review"]
        assert config.triage.milestone_strategy.inherit_from_issues == "earliest"
        assert config.triage.priority == "P1"

    def test_triage_config_explicit_milestone(self, tmp_path):
        """Test explicit milestone overrides inherit strategy."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt

triage:
  milestone_strategy:
    explicit: "v2.0"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.triage.milestone_strategy.explicit == "v2.0"
        # inherit_from_issues still has default but explicit takes precedence in planner
        assert config.triage.milestone_strategy.inherit_from_issues == "latest"

    def test_triage_config_comma_separated_labels(self, tmp_path):
        """Test that comma-separated label strings are parsed."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt

triage:
  inherit_labels: "label1, label2, label3"
  explicit_labels: "explicit1"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.triage.inherit_labels == ["label1", "label2", "label3"]
        assert config.triage.explicit_labels == ["explicit1"]

    def test_triage_config_empty_section_uses_defaults(self, tmp_path):
        """Test that empty triage section uses defaults."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt

triage: {}
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.triage.inherit_labels == []
        assert config.triage.explicit_labels == []
        assert config.triage.milestone_strategy.inherit_from_issues == "latest"
        assert config.triage.priority is None

    def test_triage_config_included_in_to_event_dict(self):
        """triage config should be included in to_event_dict output."""
        config = Config()
        config.triage.inherit_labels.append("test-label")
        config.triage.explicit_labels.append("explicit-label")

        result = config.to_event_dict()

        assert "triage" in result
        assert result["triage"]["inherit_labels"] == ["test-label"]
        assert result["triage"]["explicit_labels"] == ["explicit-label"]
        assert result["triage"]["milestone_strategy"]["inherit_from_issues"] == "latest"


class TestConfigSectionErrors:
    """Test clear error messages for invalid config sections."""

    def test_string_section_gives_clear_error(self, tmp_path):
        """When a section is a string instead of dict, error is clear."""
        from issue_orchestrator.infra.config import ConfigSectionError

        config_content = """
repo: owner/repo-name
agents: {}
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        with pytest.raises(ConfigSectionError) as exc_info:
            Config.load(config_file)

        error_msg = str(exc_info.value)
        assert "Invalid config section 'repo'" in error_msg
        assert "Got string: 'owner/repo-name'" in error_msg
        assert "Expected a mapping" in error_msg

    def test_list_section_gives_clear_error(self, tmp_path):
        """When a section is a list instead of dict, error is clear."""
        from issue_orchestrator.infra.config import ConfigSectionError

        config_content = """
agents:
  - item1
  - item2
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        with pytest.raises(ConfigSectionError) as exc_info:
            Config.load(config_file)

        error_msg = str(exc_info.value)
        assert "Invalid config section 'agents'" in error_msg
        assert "Got a list" in error_msg

    def test_none_section_treated_as_empty_dict(self, tmp_path):
        """Section with only comments (None in YAML) is treated as empty."""
        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt

repo:
  # Just a comment, creates None
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        # Should not raise - None becomes {}
        config = Config.load(config_file)
        assert config.repo is None  # No repo name set

    def test_nested_section_error_gives_context(self, tmp_path):
        """Error in nested section shows full path."""
        from issue_orchestrator.infra.config import ConfigSectionError

        config_content = """
agents:
  agent:test:
    prompt: /tmp/prompt.txt

execution:
  concurrency: "not a dict"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        with pytest.raises(ConfigSectionError) as exc_info:
            Config.load(config_file)

        error_msg = str(exc_info.value)
        assert "concurrency" in error_msg
        assert "Got string" in error_msg


class TestEnvVarSubstitution:
    """Tests for ${VAR} environment variable substitution in config."""

    def test_expands_env_var_in_string(self, tmp_path, monkeypatch):
        """${VAR} in config value is replaced with env var value."""
        monkeypatch.setenv("TEST_CLAIMANT_ID", "prod-west-1")

        config_content = """
claims:
  enabled: true
  claimant_id: "${TEST_CLAIMANT_ID}"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.claims.claimant_id == "prod-west-1"

    def test_expands_multiple_env_vars_in_string(self, tmp_path, monkeypatch):
        """Multiple ${VAR} references in one string are all expanded."""
        monkeypatch.setenv("ENV_NAME", "prod")
        monkeypatch.setenv("REGION", "west")

        config_content = """
claims:
  enabled: true
  claimant_id: "${ENV_NAME}-${REGION}-orchestrator"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.claims.claimant_id == "prod-west-orchestrator"

    def test_expands_env_var_in_nested_config(self, tmp_path, monkeypatch):
        """${VAR} works in deeply nested config values."""
        monkeypatch.setenv("GITHUB_TOKEN_VAR", "MY_GITHUB_TOKEN")

        config_content = """
repo:
  github:
    token_env: "${GITHUB_TOKEN_VAR}"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.github_token_env == "MY_GITHUB_TOKEN"

    def test_expands_env_var_in_list(self, tmp_path, monkeypatch):
        """${VAR} works in list items."""
        monkeypatch.setenv("EXCLUDE_LABEL", "wip")

        config_content = """
filtering:
  exclude_labels:
    - "${EXCLUDE_LABEL}"
    - "draft"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert "wip" in config.filtering.exclude_labels
        assert "draft" in config.filtering.exclude_labels

    def test_error_on_missing_env_var(self, tmp_path):
        """Missing env var raises ConfigEnvVarError with clear message."""
        from issue_orchestrator.infra.config import ConfigEnvVarError

        config_content = """
claims:
  enabled: true
  claimant_id: "${NONEXISTENT_VAR_12345}"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        with pytest.raises(ConfigEnvVarError) as exc_info:
            Config.load(config_file)

        error_msg = str(exc_info.value)
        assert "NONEXISTENT_VAR_12345" in error_msg
        assert "not set" in error_msg

    def test_error_message_includes_config_path(self, tmp_path):
        """Error message shows where in config the missing var was referenced."""
        from issue_orchestrator.infra.config import ConfigEnvVarError

        config_content = """
claims:
  claimant_id: "${MISSING_VAR}"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        with pytest.raises(ConfigEnvVarError) as exc_info:
            Config.load(config_file)

        error_msg = str(exc_info.value)
        assert "claims.claimant_id" in error_msg

    def test_literal_string_without_env_var_unchanged(self, tmp_path):
        """Strings without ${VAR} are unchanged."""
        config_content = """
claims:
  enabled: true
  claimant_id: "literal-string-value"
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.claims.claimant_id == "literal-string-value"

    def test_numbers_and_booleans_unchanged(self, tmp_path):
        """Non-string values (numbers, booleans) pass through unchanged."""
        config_content = """
claims:
  enabled: true
  lease_seconds: 900
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.claims.enabled is True
        assert config.claims.lease_seconds == 900


class TestConfigSerialization:
    """Tests for Config.to_dict() and Config.save() methods."""

    def test_to_dict_basic(self, tmp_path):
        """Test to_dict returns basic config structure."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("test prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()

        config_content = f"""
repo:
  name: owner/repo

worktrees:
  base: {worktree_base}

agents:
  agent:test:
    prompt: {prompt_file}
    model: sonnet

execution:
  concurrency:
    max_concurrent_sessions: 5
    session_timeout_minutes: 30
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        result = config.to_dict()

        assert result["repo"]["name"] == "owner/repo"
        assert "agents" in result
        assert "agent:test" in result["agents"]
        assert result["execution"]["concurrency"]["max_concurrent_sessions"] == 5
        assert result["execution"]["concurrency"]["session_timeout_minutes"] == 30

    def test_to_dict_e2e_settings(self, tmp_path):
        """Test to_dict includes E2E settings when non-default."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("test prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()

        config_content = f"""
repo:
  name: owner/repo

worktrees:
  base: {worktree_base}

agents:
  agent:test:
    prompt: {prompt_file}

e2e:
  enabled: true
  auto_run_interval_minutes: 60
  stop_on_first_failure: true
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        result = config.to_dict()

        assert "e2e" in result
        assert result["e2e"]["enabled"] is True
        assert result["e2e"]["auto_run_interval_minutes"] == 60
        assert result["e2e"]["stop_on_first_failure"] is True

    def test_to_dict_omits_defaults(self, tmp_path):
        """Test to_dict omits default values to keep output minimal."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("test prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()

        config_content = f"""
repo:
  name: owner/repo

worktrees:
  base: {worktree_base}

agents:
  agent:test:
    prompt: {prompt_file}
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        result = config.to_dict()

        # E2E should not be present since all defaults
        assert "e2e" not in result

        # Labels should not be present since all defaults
        assert "labels" not in result

    def test_save_writes_yaml(self, tmp_path):
        """Test save() writes valid YAML to file."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("test prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()

        config_content = f"""
repo:
  name: owner/repo

worktrees:
  base: {worktree_base}

agents:
  agent:test:
    prompt: {prompt_file}

execution:
  concurrency:
    max_concurrent_sessions: 7
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        # Change a setting
        config.max_concurrent_sessions = 10

        # Save to new file
        output_file = tmp_path / "output.yaml"
        config.save(output_file)

        # Verify file exists and contains expected content
        assert output_file.exists()
        content = output_file.read_text()
        assert "max_concurrent_sessions: 10" in content
        assert "owner/repo" in content

    def test_save_uses_config_path_by_default(self, tmp_path):
        """Test save() uses config_path when no path specified."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("test prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()

        config_content = f"""
repo:
  name: owner/repo

worktrees:
  base: {worktree_base}

agents:
  agent:test:
    prompt: {prompt_file}
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        config.max_concurrent_sessions = 15

        # Save without specifying path
        result_path = config.save()

        assert result_path == config.config_path
        content = config_file.read_text()
        assert "max_concurrent_sessions: 15" in content

    def test_save_raises_without_path(self):
        """Test save() raises ValueError when no path is available."""
        config = Config()

        with pytest.raises(ValueError, match="No path specified"):
            config.save()

    def test_to_dict_roundtrip(self, tmp_path):
        """Test that to_dict output can be loaded back."""
        import yaml

        prompt_file = tmp_path / "prompt.txt"
        prompt_file.write_text("test prompt")
        worktree_base = tmp_path / "worktrees"
        worktree_base.mkdir()

        config_content = f"""
repo:
  name: owner/repo

worktrees:
  base: {worktree_base}

agents:
  agent:test:
    prompt: {prompt_file}
    model: haiku
    timeout_minutes: 60

execution:
  concurrency:
    max_concurrent_sessions: 8
    session_timeout_minutes: 90

e2e:
  enabled: true
  auto_run_interval_minutes: 45
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        result_dict = config.to_dict()

        # Write dict back to YAML
        output_file = tmp_path / "roundtrip.yaml"
        with open(output_file, "w") as f:
            yaml.dump(result_dict, f)

        # Load from the new file
        config2 = Config.load(output_file)

        # Key settings should match
        assert config2.repo == config.repo
        assert config2.max_concurrent_sessions == config.max_concurrent_sessions
        assert config2.session_timeout_minutes == config.session_timeout_minutes
        assert config2.e2e.enabled == config.e2e.enabled
        assert config2.e2e.auto_run_interval_minutes == config.e2e.auto_run_interval_minutes


class TestHooksConfig:
    """Tests for hooks configuration parsing."""

    def test_hooks_config_defaults(self, tmp_path):
        """Test default hooks config values."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        # Default values
        assert config.hooks.safety_check.interval_days == 7
        assert config.hooks.safety_check.dangerous_allow_failure is False

    def test_hooks_config_custom_interval(self, tmp_path):
        """Test custom safety check interval."""
        config_content = """
worktrees:
  base: /tmp

hooks:
  safety_check:
    interval_days: 14

agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.hooks.safety_check.interval_days == 14
        assert config.hooks.safety_check.dangerous_allow_failure is False

    def test_hooks_config_disabled(self, tmp_path):
        """Test safety check disabled with interval_days=0."""
        config_content = """
worktrees:
  base: /tmp

hooks:
  safety_check:
    interval_days: 0

agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.hooks.safety_check.interval_days == 0

    def test_hooks_config_dangerous_allow_failure(self, tmp_path):
        """Test dangerous_allow_failure setting."""
        config_content = """
worktrees:
  base: /tmp

hooks:
  safety_check:
    dangerous_allow_failure: true

agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)

        assert config.hooks.safety_check.dangerous_allow_failure is True

    def test_hooks_config_in_to_event_dict(self, tmp_path):
        """Test hooks config is included in to_event_dict()."""
        config_content = """
worktrees:
  base: /tmp

hooks:
  safety_check:
    interval_days: 30
    dangerous_allow_failure: true

agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        event_dict = config.to_event_dict()

        assert "hooks" in event_dict
        assert event_dict["hooks"]["safety_check"]["interval_days"] == 30
        assert event_dict["hooks"]["safety_check"]["dangerous_allow_failure"] is True

    def test_hooks_config_to_dict_non_default(self, tmp_path):
        """Test to_dict() includes hooks when non-default."""
        config_content = """
worktrees:
  base: /tmp

hooks:
  safety_check:
    interval_days: 14
    dangerous_allow_failure: true

agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        result = config.to_dict()

        assert "hooks" in result
        assert result["hooks"]["safety_check"]["interval_days"] == 14
        assert result["hooks"]["safety_check"]["dangerous_allow_failure"] is True

    def test_hooks_config_to_dict_default_values(self, tmp_path):
        """Test to_dict() omits hooks when all values are default."""
        config_content = """
worktrees:
  base: /tmp

agents:
  agent:test:
    prompt: /tmp/prompt.txt
"""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text(config_content)

        config = Config.load(config_file)
        result = config.to_dict()

        # Hooks section should not be present when using defaults
        assert "hooks" not in result
