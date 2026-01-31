"""Unit tests for config validators.

Tests each validator in isolation with mock config objects.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch
import pytest

from issue_orchestrator.infra.validators import (
    AgentValidator,
    IsolationValidator,
    ReviewWorkflowValidator,
    TemplateValidator,
    UnknownFieldsValidator,
    WorktreeValidator,
)


class TestWorktreeValidator:
    """Tests for WorktreeValidator."""

    def _make_config(self, tmp_path, worktree_base=None, recreate_mode="delete"):
        """Create a mock config with worktree settings."""
        config = MagicMock()
        config.worktree_base = worktree_base or tmp_path
        config.worktree_branch_on_recreate = recreate_mode
        config.worktree_remediation_pr_collision = "new_branch"
        return config

    def test_valid_config(self, tmp_path):
        """Verify no errors for valid worktree config."""
        config = self._make_config(tmp_path, worktree_base=tmp_path)
        errors = WorktreeValidator().validate(config)
        assert errors == []

    def test_relative_path_error(self, tmp_path):
        """Verify error for relative worktree base."""
        config = self._make_config(tmp_path, worktree_base=Path("relative/path"))
        errors = WorktreeValidator().validate(config)
        assert len(errors) == 1
        assert "must be absolute path" in errors[0]

    def test_nonexistent_path_error(self, tmp_path):
        """Verify error for nonexistent worktree base."""
        config = self._make_config(tmp_path, worktree_base=tmp_path / "nonexistent")
        errors = WorktreeValidator().validate(config)
        assert len(errors) == 1
        assert "does not exist" in errors[0]

    def test_file_not_dir_error(self, tmp_path):
        """Verify error when worktree base is a file, not directory."""
        file_path = tmp_path / "file.txt"
        file_path.touch()
        config = self._make_config(tmp_path, worktree_base=file_path)
        errors = WorktreeValidator().validate(config)
        assert len(errors) == 1
        assert "not a directory" in errors[0]

    def test_invalid_recreate_mode(self, tmp_path):
        """Verify error for invalid worktree_branch_on_recreate."""
        config = self._make_config(tmp_path, recreate_mode="invalid_mode")
        errors = WorktreeValidator().validate(config)
        assert len(errors) == 1
        assert "worktree_branch_on_recreate" in errors[0]

    def test_valid_recreate_modes(self, tmp_path):
        """Verify valid recreate modes are accepted."""
        for mode in ["delete", "create_new_branch"]:
            config = self._make_config(tmp_path, recreate_mode=mode)
            errors = WorktreeValidator().validate(config)
            assert errors == [], f"Mode '{mode}' should be valid"


class TestIsolationValidator:
    """Tests for IsolationValidator."""

    def _make_config(self, mode):
        """Create a mock config with isolation settings."""
        config = MagicMock()
        config.isolation = MagicMock()
        config.isolation.mode = mode
        return config

    def test_standard_mode_valid(self):
        """Verify standard isolation mode is valid."""
        config = self._make_config("standard")
        errors = IsolationValidator().validate(config)
        assert errors == []

    def test_hardened_mode_valid(self):
        """Verify hardened isolation mode is valid."""
        config = self._make_config("hardened")
        errors = IsolationValidator().validate(config)
        assert errors == []

    def test_invalid_mode_error(self):
        """Verify error for unknown isolation mode."""
        config = self._make_config("paranoid")
        errors = IsolationValidator().validate(config)
        assert len(errors) == 1
        assert "isolation.mode" in errors[0]


class TestReviewWorkflowValidator:
    """Tests for ReviewWorkflowValidator."""

    def _make_config(
        self,
        review_enabled=False,
        code_review_agent=None,
        triage_review_agent=None,
        agents=None,
    ):
        """Create a mock config with review settings."""
        config = MagicMock()
        config.review_enabled = review_enabled
        config.code_review_agent = code_review_agent
        config.triage_review_agent = triage_review_agent
        config.agents = agents or {}
        return config

    def test_reviews_disabled_valid(self):
        """Verify no errors when reviews are disabled."""
        config = self._make_config(review_enabled=False)
        errors = ReviewWorkflowValidator().validate(config)
        assert errors == []

    def test_reviews_enabled_no_reviewer_error(self):
        """Verify error when reviews enabled but no reviewer set."""
        config = self._make_config(review_enabled=True)
        errors = ReviewWorkflowValidator().validate(config)
        assert len(errors) == 1
        assert "no default reviewer" in errors[0]

    def test_reviews_enabled_invalid_reviewer_error(self):
        """Verify error when reviewer doesn't exist in agents."""
        config = self._make_config(
            review_enabled=True,
            code_review_agent="agent:reviewer",
            agents={"agent:developer": MagicMock()},
        )
        errors = ReviewWorkflowValidator().validate(config)
        assert len(errors) == 1
        assert "not found in agents" in errors[0]

    def test_reviews_enabled_valid_reviewer(self):
        """Verify no error when reviewer exists in agents."""
        config = self._make_config(
            review_enabled=True,
            code_review_agent="agent:reviewer",
            agents={"agent:reviewer": MagicMock()},
        )
        errors = ReviewWorkflowValidator().validate(config)
        assert errors == []

    def test_invalid_triage_agent_error(self):
        """Verify error when triage agent doesn't exist."""
        config = self._make_config(
            triage_review_agent="agent:triage",
            agents={"agent:developer": MagicMock()},
        )
        errors = ReviewWorkflowValidator().validate(config)
        assert len(errors) == 1
        assert "triage_review_agent" in errors[0]


class TestTemplateValidator:
    """Tests for TemplateValidator."""

    def _make_config(self, agents):
        """Create a mock config with agents."""
        config = MagicMock()
        config.agents = agents
        return config

    def _make_agent(self, initial_prompt="", command=""):
        """Create a mock agent config."""
        agent = MagicMock()
        agent.initial_prompt = initial_prompt
        agent.command = command
        return agent

    def test_valid_initial_prompt_vars(self):
        """Verify valid template variables in initial_prompt."""
        agent = self._make_agent(
            initial_prompt="Issue #{issue_number}: {issue_title} in {worktree}"
        )
        config = self._make_config({"agent:dev": agent})
        errors = TemplateValidator().validate(config)
        assert errors == []

    def test_invalid_initial_prompt_var_error(self):
        """Verify error for unknown variable in initial_prompt."""
        agent = self._make_agent(initial_prompt="Fix {unknown_var}")
        config = self._make_config({"agent:dev": agent})
        errors = TemplateValidator().validate(config)
        assert len(errors) == 1
        assert "unknown_var" in errors[0]
        assert "initial_prompt" in errors[0]

    def test_valid_command_vars(self):
        """Verify valid template variables in command."""
        agent = self._make_agent(command="claude -p {initial_prompt} --model {model}")
        config = self._make_config({"agent:dev": agent})
        errors = TemplateValidator().validate(config)
        assert errors == []

    def test_invalid_command_var_error(self):
        """Verify error for unknown variable in command."""
        agent = self._make_agent(command="run {bad_variable}")
        config = self._make_config({"agent:dev": agent})
        errors = TemplateValidator().validate(config)
        assert len(errors) == 1
        assert "bad_variable" in errors[0]
        assert "command" in errors[0]

    def test_multiple_invalid_vars(self):
        """Verify multiple invalid variables are reported."""
        agent = self._make_agent(
            initial_prompt="{foo} and {bar}",
            command="{baz}",
        )
        config = self._make_config({"agent:dev": agent})
        errors = TemplateValidator().validate(config)
        assert len(errors) == 2


class TestUnknownFieldsValidator:
    """Tests for UnknownFieldsValidator."""

    def _make_config(self, raw_data=None, raw_agents=None, strict=False):
        """Create a mock config with raw YAML data."""
        config = MagicMock()
        config.raw_data = raw_data or {}
        config.raw_agents = raw_agents or {}
        config.config_strict = strict
        return config

    def test_no_unknown_fields_valid(self):
        """Verify no errors when all fields are known."""
        config = self._make_config(
            raw_data={"repo": {}, "agents": {}},  # Known top-level fields
            raw_agents={},
        )

        with patch(
            "issue_orchestrator.infra.config.ALLOWED_TOP_LEVEL_FIELDS",
            {"repo", "agents"},
        ):
            errors = UnknownFieldsValidator().validate(config)
        assert errors == []

    def test_unknown_top_level_field_strict(self):
        """Verify error for unknown top-level field in strict mode."""
        config = self._make_config(
            raw_data={"repo": {}, "unknown_field": "value"},
            strict=True,
        )
        errors = UnknownFieldsValidator().validate(config)
        assert len(errors) == 1
        assert "unknown_field" in errors[0]

    def test_unknown_top_level_field_non_strict(self):
        """Verify warning (no error) for unknown field in non-strict mode."""
        config = self._make_config(
            raw_data={"repo": {}, "unknown_field": "value"},
            strict=False,
        )
        errors = UnknownFieldsValidator().validate(config)
        # Non-strict mode logs warning but doesn't return errors
        assert errors == []

    def test_unknown_agent_field_strict(self):
        """Verify error for unknown agent field in strict mode."""
        config = self._make_config(
            raw_data={},
            raw_agents={"agent:dev": {"unknown_agent_field": "value"}},
            strict=True,
        )
        errors = UnknownFieldsValidator().validate(config)
        assert len(errors) == 1
        assert "agent:dev" in errors[0]


class TestAgentValidator:
    """Tests for AgentValidator."""

    def _make_config(
        self,
        agents=None,
        default_agent=None,
        raw_agents=None,
    ):
        """Create a mock config with agent settings."""
        config = MagicMock()
        config.agents = agents or {}
        config.default_agent = default_agent
        config.raw_agents = raw_agents or {}
        return config

    def _make_agent(
        self,
        prompt_path=None,
        provider=None,
        model="sonnet",
        reviewer=None,
        prompt_exists=True,
    ):
        """Create a mock agent config."""
        agent = MagicMock()
        if prompt_path is not None:
            # Use real Path object
            agent.prompt_path = prompt_path
        else:
            # Use mock that can have exists() mocked
            mock_path = MagicMock()
            mock_path.exists.return_value = prompt_exists
            agent.prompt_path = mock_path
        agent.provider = provider
        agent.model = model
        agent.reviewer = reviewer
        return agent

    def test_no_agents_error(self):
        """Verify error when no agents configured."""
        config = self._make_config(agents={})
        with patch("issue_orchestrator.agent_runner.is_valid_provider", return_value=True), \
             patch("issue_orchestrator.agent_runner.list_providers", return_value=["claude-code"]):
            errors = AgentValidator().validate(config)
        assert len(errors) == 1
        assert "No agents configured" in errors[0]

    def test_valid_agent(self, tmp_path):
        """Verify no errors for valid agent config."""
        prompt_file = tmp_path / "prompt.txt"
        prompt_file.touch()
        agent = self._make_agent(prompt_path=prompt_file, provider="claude-code")
        config = self._make_config(agents={"agent:dev": agent})

        with patch("issue_orchestrator.agent_runner.is_valid_provider", return_value=True), \
             patch("issue_orchestrator.agent_runner.list_providers", return_value=["claude-code"]):
            errors = AgentValidator().validate(config)
        assert errors == []

    def test_missing_prompt_file_error(self):
        """Verify error when prompt file doesn't exist."""
        agent = self._make_agent(provider="claude-code", prompt_exists=False)
        config = self._make_config(agents={"agent:dev": agent})

        with patch("issue_orchestrator.agent_runner.is_valid_provider", return_value=True), \
             patch("issue_orchestrator.agent_runner.list_providers", return_value=["claude-code"]):
            errors = AgentValidator().validate(config)
        assert any("prompt file not found" in e for e in errors)

    def test_unknown_model_error(self):
        """Verify error for unknown Claude model."""
        agent = self._make_agent(model="gpt-4", provider="claude-code")
        config = self._make_config(agents={"agent:dev": agent})

        with patch("issue_orchestrator.agent_runner.is_valid_provider", return_value=True), \
             patch("issue_orchestrator.agent_runner.list_providers", return_value=["claude-code"]):
            errors = AgentValidator().validate(config)
        assert any("unknown model" in e for e in errors)

    def test_invalid_reviewer_reference_error(self):
        """Verify error when reviewer references non-existent agent."""
        agent = self._make_agent(provider="claude-code", reviewer="agent:nonexistent")
        config = self._make_config(agents={"agent:dev": agent})

        with patch("issue_orchestrator.agent_runner.is_valid_provider", return_value=True), \
             patch("issue_orchestrator.agent_runner.list_providers", return_value=["claude-code"]):
            errors = AgentValidator().validate(config)
        assert any("reviewer" in e and "not found" in e for e in errors)

    def test_no_provider_and_no_command_error(self):
        """Verify error when agent has no provider and no custom command."""
        agent = self._make_agent(provider=None)
        config = self._make_config(
            agents={"agent:dev": agent},
            raw_agents={"agent:dev": {}},  # No command override
        )

        with patch("issue_orchestrator.agent_runner.is_valid_provider", return_value=True), \
             patch("issue_orchestrator.agent_runner.list_providers", return_value=["claude-code"]):
            errors = AgentValidator().validate(config)
        assert any("no provider specified" in e for e in errors)

    def test_custom_command_bypasses_provider_check(self):
        """Verify no error when agent has custom command instead of provider."""
        agent = self._make_agent(provider=None, model="haiku")  # Valid model
        config = self._make_config(
            agents={"agent:dev": agent},
            raw_agents={"agent:dev": {"command": "custom-agent {prompt}"}},
        )

        with patch("issue_orchestrator.agent_runner.is_valid_provider", return_value=True), \
             patch("issue_orchestrator.agent_runner.list_providers", return_value=["claude-code"]):
            errors = AgentValidator().validate(config)
        # Should not have "no provider specified" error
        assert not any("no provider specified" in e for e in errors)
