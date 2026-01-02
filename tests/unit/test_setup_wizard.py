"""Unit tests for the setup wizard."""

import pytest
from pathlib import Path
from unittest.mock import patch, Mock

from issue_orchestrator.entrypoints.cli_tools.setup_wizard import (
    create_starter_prompt,
    create_triage_review_prompt,
    detect_repo,
    check_prerequisites,
    fetch_github_labels,
    find_existing_config,
    scan_existing_repo,
    wizard_new_project,
    wizard_existing_project,
    run_wizard,
    Prompter,
    ConsolePrompter,
    DetectedState,
)


class MockPrompter:
    """Mock prompter for testing wizard flows."""

    def __init__(self, answers: list):
        """Initialize with a list of answers to return in order.

        Args:
            answers: List of values to return for input/yes_no/choice calls.
                    For yes_no, use True/False.
                    For choice, use the choice string.
                    For input, use the string value.
        """
        self.answers = list(answers)
        self.answer_index = 0
        self.printed: list[str] = []
        self.questions_asked: list[str] = []

    def _get_answer(self, question: str):
        """Get the next answer from the queue."""
        self.questions_asked.append(question)
        if self.answer_index >= len(self.answers):
            raise IndexError(f"No more answers available for question: {question}")
        answer = self.answers[self.answer_index]
        self.answer_index += 1
        return answer

    def print(self, message: str) -> None:
        self.printed.append(message)

    def input(self, question: str, default: str = "") -> str:
        answer = self._get_answer(question)
        return answer if answer != "" else default

    def yes_no(self, question: str, default: bool = True) -> bool:
        answer = self._get_answer(question)
        if isinstance(answer, bool):
            return answer
        # Allow string answers
        return answer.lower() in ("y", "yes", "true")

    def choice(self, question: str, choices: list[str], allow_custom: bool = False) -> str:
        return self._get_answer(question)


class TestCreateStarterPrompt:
    """Test the create_starter_prompt function."""

    def test_creates_prompt_file(self, tmp_path):
        """Test that starter prompt is created with correct content."""
        prompt_path = tmp_path / "prompts" / "backend.md"

        create_starter_prompt("agent:backend", prompt_path)

        assert prompt_path.exists()
        content = prompt_path.read_text()
        assert "Backend Agent Prompt" in content
        assert "{issue_number}" in content
        assert "{issue_title}" in content
        assert "agent-done" in content

    def test_creates_parent_directories(self, tmp_path):
        """Test that parent directories are created."""
        prompt_path = tmp_path / "deep" / "nested" / "prompt.md"

        create_starter_prompt("agent:test", prompt_path)

        assert prompt_path.exists()
        assert prompt_path.parent.exists()

    def test_extracts_agent_short_name(self, tmp_path):
        """Test that agent short name is extracted correctly."""
        prompt_path = tmp_path / "prompt.md"

        create_starter_prompt("agent:frontend-ui", prompt_path)

        content = prompt_path.read_text()
        assert "Frontend-Ui Agent Prompt" in content


class TestCreateTriageReviewPrompt:
    """Test the create_triage_review_prompt function."""

    def test_creates_triage_prompt_with_labels(self, tmp_path):
        """Test that triage prompt is created with label values substituted."""
        prompt_path = tmp_path / "triage.md"

        create_triage_review_prompt(prompt_path, "needs-triage-review", "triage-reviewed")

        assert prompt_path.exists()
        content = prompt_path.read_text()

        # Check labels are substituted (not placeholders)
        assert "needs-triage-review" in content
        assert "triage-reviewed" in content
        assert "{review_label}" not in content
        assert "{reviewed_label}" not in content

    def test_includes_gh_commands_with_labels(self, tmp_path):
        """Test that gh commands include the actual labels."""
        prompt_path = tmp_path / "triage.md"

        create_triage_review_prompt(prompt_path, "my-review-label", "my-reviewed-label")

        content = prompt_path.read_text()

        # Check gh commands have correct labels
        assert 'gh pr list --label "my-review-label"' in content
        assert '--remove-label "my-review-label"' in content
        assert '--add-label "my-reviewed-label"' in content

    def test_preserves_template_variables(self, tmp_path):
        """Test that issue_number and issue_title placeholders are preserved."""
        prompt_path = tmp_path / "triage.md"

        create_triage_review_prompt(prompt_path, "review", "reviewed")

        content = prompt_path.read_text()

        # These should remain as template variables for runtime substitution
        assert "{issue_number}" in content
        assert "{issue_title}" in content

    def test_includes_batch_review_workflow(self, tmp_path):
        """Test that batch review workflow is included."""
        prompt_path = tmp_path / "triage.md"

        create_triage_review_prompt(prompt_path, "review", "reviewed")

        content = prompt_path.read_text()

        assert "Batch Review" in content
        assert "Triage Audit Report" in content
        assert "PRs Audited" in content
        assert "Patterns Observed" in content

    def test_includes_single_issue_review(self, tmp_path):
        """Test that single issue review workflow is included."""
        prompt_path = tmp_path / "cto.md"

        create_triage_review_prompt(prompt_path, "review", "reviewed")

        content = prompt_path.read_text()

        assert "Single Issue Review" in content

    def test_includes_agent_done_completion(self, tmp_path):
        """Test that agent-done completion instructions are included."""
        prompt_path = tmp_path / "cto.md"

        create_triage_review_prompt(prompt_path, "review", "reviewed")

        content = prompt_path.read_text()

        assert "agent-done completed" in content
        assert "--implementation" in content
        assert "--problems" in content

    def test_creates_parent_directories(self, tmp_path):
        """Test that parent directories are created."""
        prompt_path = tmp_path / "deep" / "nested" / "cto.md"

        create_triage_review_prompt(prompt_path, "review", "reviewed")

        assert prompt_path.exists()


class TestDetectRepo:
    """Test the detect_repo function."""

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.run_git")
    def test_detect_https_url(self, mock_run_git):
        """Test detecting repo from HTTPS URL."""
        mock_run_git.return_value = (True, "https://github.com/owner/repo.git")

        repo = detect_repo()

        assert repo == "owner/repo"

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.run_git")
    def test_detect_ssh_url(self, mock_run_git):
        """Test detecting repo from SSH URL."""
        mock_run_git.return_value = (True, "git@github.com:owner/repo.git")

        repo = detect_repo()

        assert repo == "owner/repo"

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.run_git")
    def test_detect_https_without_git_suffix(self, mock_run_git):
        """Test detecting repo from HTTPS URL without .git suffix."""
        mock_run_git.return_value = (True, "https://github.com/owner/repo")

        repo = detect_repo()

        assert repo == "owner/repo"

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.run_git")
    def test_returns_none_on_failure(self, mock_run_git):
        """Test that None is returned when git command fails."""
        mock_run_git.return_value = (False, "")

        repo = detect_repo()

        assert repo is None

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.run_git")
    def test_returns_none_for_non_github(self, mock_run_git):
        """Test that None is returned for non-GitHub remotes."""
        mock_run_git.return_value = (True, "https://gitlab.com/owner/repo.git")

        repo = detect_repo()

        assert repo is None


class TestCheckPrerequisites:
    """Test the check_prerequisites function."""

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.run_git")
    @patch("subprocess.run")
    @patch("issue_orchestrator.adapters.github.http_client.resolve_github_token")
    def test_all_prerequisites_met(self, mock_token, mock_subprocess, mock_git):
        """Test when all prerequisites are met."""
        mock_git.return_value = (True, "git version 2.40.0")
        mock_token.return_value = "token"
        mock_subprocess.return_value = Mock(returncode=0)

        checks = check_prerequisites()

        assert checks["git"] is True
        assert checks["github_auth"] is True
        assert checks["claude"] is True

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.run_git")
    @patch("subprocess.run")
    @patch("issue_orchestrator.adapters.github.http_client.resolve_github_token")
    def test_missing_git(self, mock_token, mock_subprocess, mock_git):
        """Test when git is missing."""
        mock_git.return_value = (False, "")
        mock_token.return_value = "token"
        mock_subprocess.return_value = Mock(returncode=0)

        checks = check_prerequisites()

        assert checks["git"] is False
        assert checks["github_auth"] is True

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.run_git")
    @patch("subprocess.run")
    @patch("issue_orchestrator.adapters.github.http_client.resolve_github_token")
    def test_github_not_authenticated(self, mock_token, mock_subprocess, mock_git):
        """Test when GitHub token is missing."""
        mock_git.return_value = (True, "git version 2.40.0")
        mock_token.side_effect = RuntimeError("missing token")
        mock_subprocess.return_value = Mock(returncode=0)

        checks = check_prerequisites()

        assert checks["github_auth"] is False


class TestFetchGithubLabels:
    """Test the fetch_github_labels function."""

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard._github_adapter")
    def test_fetches_labels(self, mock_client_factory):
        """Test successful label fetching."""
        mock_client = Mock()
        mock_client.list_labels.return_value = [{"name": "bug"}, {"name": "agent:web"}]
        mock_client_factory.return_value = mock_client

        labels = fetch_github_labels("owner/repo")

        assert labels == ["bug", "agent:web"]

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard._github_adapter")
    def test_returns_empty_on_failure(self, mock_client_factory):
        """Test that empty list is returned on failure."""
        mock_client_factory.side_effect = RuntimeError("boom")

        labels = fetch_github_labels("owner/repo")

        assert labels == []

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard._github_adapter")
    def test_returns_empty_on_invalid_payload(self, mock_client_factory):
        """Test that empty list is returned for invalid payload."""
        mock_client = Mock()
        mock_client.list_labels.return_value = ["not-a-dict"]
        mock_client_factory.return_value = mock_client

        labels = fetch_github_labels("owner/repo")

        assert labels == []


class TestFindExistingConfig:
    """Test the find_existing_config function."""

    def test_finds_config_in_current_dir(self, tmp_path):
        """Test finding config in current directory."""
        config_file = tmp_path / ".issue-orchestrator.yaml"
        config_file.write_text("repo: owner/repo\nagents: {}")

        path, config = find_existing_config(tmp_path)

        assert path == config_file
        assert config["repo"] == "owner/repo"

    def test_finds_config_in_hidden_dir(self, tmp_path):
        """Test finding config in .issue-orchestrator directory."""
        hidden_dir = tmp_path / ".issue-orchestrator"
        hidden_dir.mkdir()
        config_file = hidden_dir / "config.yaml"
        config_file.write_text("repo: owner/repo")

        path, config = find_existing_config(tmp_path)

        assert path == config_file

    def test_returns_none_when_not_found(self, tmp_path):
        """Test that None is returned when config not found."""
        path, config = find_existing_config(tmp_path)

        assert path is None
        assert config is None

    def test_prefers_root_over_hidden(self, tmp_path):
        """Test that root config is preferred over hidden directory."""
        # Create both configs
        root_config = tmp_path / ".issue-orchestrator.yaml"
        root_config.write_text("repo: root/repo")

        hidden_dir = tmp_path / ".issue-orchestrator"
        hidden_dir.mkdir()
        hidden_config = hidden_dir / "config.yaml"
        hidden_config.write_text("repo: hidden/repo")

        path, config = find_existing_config(tmp_path)

        assert path == root_config
        assert config["repo"] == "root/repo"


class TestScanExistingRepo:
    """Test the scan_existing_repo function."""

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.find_prompt_candidates")
    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.find_existing_config")
    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.fetch_github_labels")
    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.detect_repo")
    def test_scans_repo(self, mock_repo, mock_labels, mock_config, mock_prompts, tmp_path):
        """Test scanning an existing repo."""
        mock_repo.return_value = "owner/repo"
        mock_labels.return_value = ["bug", "agent:web", "agent:backend"]
        mock_config.return_value = (None, None)
        mock_prompts.return_value = []

        state = scan_existing_repo(tmp_path)

        assert state.repo == "owner/repo"
        assert state.agent_labels == ["agent:web", "agent:backend"]
        assert len(state.github_labels) == 3

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.find_prompt_candidates")
    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.find_existing_config")
    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.fetch_github_labels")
    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.detect_repo")
    def test_handles_no_repo(self, mock_repo, mock_labels, mock_config, mock_prompts, tmp_path):
        """Test scanning when repo detection fails."""
        mock_repo.return_value = None
        mock_config.return_value = (None, None)
        mock_prompts.return_value = []

        state = scan_existing_repo(tmp_path)

        assert state.repo is None
        assert state.github_labels == []
        assert state.agent_labels == []


class TestWizardNewProject:
    """Test the wizard_new_project function."""

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.detect_repo")
    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard._github_adapter")
    def test_creates_basic_config(self, mock_client_factory, mock_detect_repo):
        """Test creating a basic config with one agent."""
        mock_detect_repo.return_value = "owner/repo"
        mock_client_factory.return_value = Mock()

        prompter = MockPrompter([
            "owner/repo",           # repo (accept detected)
            "agent:backend",        # first agent label
            ".prompts/backend.md",  # prompt path (accept default)
            "45",                   # timeout
            "claude",               # agent type
            "sonnet",               # model choice
            "default",              # permission mode
            "",                     # empty to finish agents
            True,                   # create labels on GitHub
            "3",                    # max concurrent sessions
            "due_date",             # milestone sort strategy
            "../",                  # worktree base
            "web",                  # ui mode
            "8080",                 # web port
            "",                     # label prefix (none)
            False,                  # enable PR review labeling
        ])

        config = wizard_new_project(prompter)

        assert config["repo"] == "owner/repo"
        assert "agent:backend" in config["agents"]
        assert config["agents"]["agent:backend"]["prompt"] == ".prompts/backend.md"
        assert config["agents"]["agent:backend"]["model"] == "sonnet"
        assert config["agents"]["agent:backend"]["timeout_minutes"] == 45
        assert config["concurrency"]["max_concurrent_sessions"] == 3
        assert config["ui_mode"] == "web"

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.detect_repo")
    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard._github_adapter")
    def test_adds_agent_prefix_when_missing(self, mock_client_factory, mock_detect_repo):
        """Test that agent: prefix is added when user confirms."""
        mock_detect_repo.return_value = None
        mock_client_factory.return_value = Mock()

        prompter = MockPrompter([
            "owner/repo",           # repo (no detection)
            "backend",              # agent label without prefix
            True,                   # yes to add prefix
            ".prompts/backend.md",
            "60",                   # timeout
            "claude",               # agent type
            "opus",                 # model
            "default",              # permission mode
            "",                     # finish agents
            False,                  # don't create labels
            "2",                    # max concurrent
            "due_date",             # milestone sort strategy
            "../",
            "tmux",                 # ui mode (tmux doesn't need port)
            "",                     # label prefix (none)
            False,                  # no review workflow
        ])

        config = wizard_new_project(prompter)

        assert "agent:backend" in config["agents"]
        assert "backend" not in config["agents"]

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.detect_repo")
    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard._github_adapter")
    def test_multiple_agents(self, mock_client_factory, mock_detect_repo):
        """Test creating config with multiple agents."""
        mock_detect_repo.return_value = "owner/repo"
        mock_client_factory.return_value = Mock()

        prompter = MockPrompter([
            "owner/repo",
            # First agent
            "agent:frontend",
            ".prompts/frontend.md",
            "30",                   # timeout
            "claude",               # agent type
            "sonnet",               # model
            "default",              # permission mode
            # Second agent
            "agent:backend",
            ".prompts/backend.md",
            "60",                   # timeout
            "claude",               # agent type
            "opus",                 # model
            "bypassPermissions",    # permission mode (different for variety)
            True,                   # confirm bypassPermissions
            # Finish
            "",
            True,                   # create labels
            "5",                    # max concurrent
            "due_date",             # milestone sort strategy
            "../",
            "web",
            "9000",                 # custom port
            "",                     # label prefix (none)
            False,                  # no review
        ])

        config = wizard_new_project(prompter)

        assert len(config["agents"]) == 2
        assert "agent:frontend" in config["agents"]
        assert "agent:backend" in config["agents"]
        assert config["agents"]["agent:frontend"]["model"] == "sonnet"
        assert config["agents"]["agent:backend"]["model"] == "opus"

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.detect_repo")
    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard._github_adapter")
    def test_custom_agent_command(self, mock_client_factory, mock_detect_repo):
        """Test creating config with a custom agent command."""
        mock_detect_repo.return_value = "owner/repo"
        mock_client_factory.return_value = Mock()

        prompter = MockPrompter([
            "owner/repo",
            "agent:custom",
            ".prompts/custom.md",
            "30",                   # timeout
            "custom",               # agent type (custom command)
            "my-agent --issue {issue_number} --prompt {prompt}",  # custom command
            "",                     # finish agents
            False,                  # don't create labels
            "3",
            "due_date",             # milestone sort strategy
            "../",
            "web",
            "8080",
            "",                     # label prefix (none)
            False,                  # no review
        ])

        config = wizard_new_project(prompter)

        assert "agent:custom" in config["agents"]
        agent_cfg = config["agents"]["agent:custom"]
        assert agent_cfg["command"] == "my-agent --issue {issue_number} --prompt {prompt}"
        # Custom agents don't get permission_mode since it's Claude-specific
        assert "permission_mode" not in agent_cfg

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.detect_repo")
    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard._github_adapter")
    def test_review_workflow_enabled(self, mock_client_factory, mock_detect_repo):
        """Test enabling two-stage review workflow."""
        mock_detect_repo.return_value = "owner/repo"
        mock_client_factory.return_value = Mock()

        prompter = MockPrompter([
            "owner/repo",
            "agent:backend",
            ".prompts/backend.md",
            "45",                   # timeout
            "claude",               # agent type
            "sonnet",               # model
            "default",              # permission mode
            "",                     # finish agents
            False,                  # don't create labels
            "3",
            "due_date",             # milestone sort strategy
            "../",
            "web",
            "8080",
            "",                     # label prefix (none)
            True,                   # enable Stage 1: per-PR code review
            "agent:reviewer",       # code review agent
            "needs-code-review",    # code review label
            "code-reviewed",        # code reviewed label
            True,                   # enable Stage 2: triage batch review
            "agent:triage",            # triage review agent
            "triage-reviewed",         # triage reviewed label
            "5",                    # threshold
        ])

        config = wizard_new_project(prompter)

        # Stage 1: Code Review
        assert config["code_review_agent"] == "agent:reviewer"
        assert config["code_review_label"] == "needs-code-review"
        assert config["code_reviewed_label"] == "code-reviewed"

        # Stage 2: Triage Batch Review
        assert config["triage_review_agent"] == "agent:triage"
        assert config["triage_reviewed_label"] == "triage-reviewed"
        assert config["triage_review_threshold"] == 5

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.detect_repo")
    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard._github_adapter")
    def test_requires_at_least_one_agent(self, mock_client_factory, mock_detect_repo):
        """Test that wizard requires at least one agent."""
        mock_detect_repo.return_value = "owner/repo"
        mock_client_factory.return_value = Mock()

        prompter = MockPrompter([
            "owner/repo",
            "",                     # try to finish with no agents
            "agent:backend",        # now add one
            ".prompts/backend.md",
            "45",                   # timeout
            "claude",               # agent type
            "sonnet",               # model
            "default",              # permission mode
            "",                     # finish
            False,                  # don't create labels
            "3",
            "due_date",             # milestone sort strategy
            "../",
            "web",
            "8080",
            "",                     # label prefix (none)
            False,                  # no review
        ])

        config = wizard_new_project(prompter)

        # Should have exactly one agent (after forcing user to add one)
        assert len(config["agents"]) == 1
        # Check that "You need at least one agent!" was printed
        assert any("at least one agent" in msg for msg in prompter.printed)


class TestWizardExistingProject:
    """Test the wizard_existing_project function."""

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard._github_adapter")
    def test_basic_existing_project(self, mock_client_factory):
        """Test onboarding an existing project."""
        mock_client_factory.return_value = Mock()

        state = DetectedState(
            repo="owner/repo",
            github_labels=["bug", "agent:web"],
            agent_labels=["agent:web"],
            existing_config=None,
            config_path=None,
            prompt_candidates=[],
        )

        prompter = MockPrompter([
            True,                   # add agent:web to config
            ".prompts/web.md",      # prompt path
            "45",                   # timeout
            "claude",               # agent type
            "sonnet",               # model
            "default",              # permission mode
            "3",                    # max concurrent
            "due_date",             # milestone sort strategy
            "../",                  # worktree base
            "web",                  # ui mode
            "8080",                 # port
            "",                     # label prefix (none)
            False,                  # no review workflow
        ])

        config, _ = wizard_existing_project(state, prompter)

        assert config["repo"] == "owner/repo"
        assert "agent:web" in config["agents"]
        assert config["concurrency"]["max_concurrent_sessions"] == 3

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard._github_adapter")
    def test_preserves_existing_config(self, mock_client_factory):
        """Test that existing config is preserved when updating."""
        mock_client_factory.return_value = Mock()

        state = DetectedState(
            repo="owner/repo",
            github_labels=["agent:web", "agent:backend"],
            agent_labels=["agent:web", "agent:backend"],
            existing_config={
                "repo": "owner/repo",
                "agents": {
                    "agent:web": {
                        "prompt": ".prompts/web.md",
                        "model": "sonnet",
                        "timeout_minutes": 45,
                    }
                },
                "concurrency": {"max_concurrent_sessions": 3},
                "ui_mode": "tmux",
            },
            config_path=Path(".issue-orchestrator.yaml"),
            prompt_candidates=[],
        )

        prompter = MockPrompter([
            True,                   # update existing config
            # agent:backend is not in config, so wizard asks about it
            True,                   # add agent:backend
            ".prompts/backend.md",  # prompt path
            "60",                   # timeout
            "claude",               # agent type
            "opus",                 # model
            "default",              # permission mode
            # No more missing agents
            # agent:web is in config but let's say it's in github_labels too (no missing labels)
            # Concurrency already configured - won't ask
            # Milestone sort not configured - will ask
            "due_date",             # milestone sort strategy
            # Worktrees needed for backend
            "../",
            # UI mode already configured - won't ask
            # Label prefix not configured
            "",                     # label prefix (none)
            # Review not configured
            False,                  # no review
        ])

        config, _ = wizard_existing_project(state, prompter)

        # Original config preserved
        assert config["agents"]["agent:web"]["prompt"] == ".prompts/web.md"
        assert config["agents"]["agent:web"]["model"] == "sonnet"
        # New agent added
        assert "agent:backend" in config["agents"]
        assert config["agents"]["agent:backend"]["model"] == "opus"
        # Existing settings preserved
        assert config["ui_mode"] == "tmux"

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard._github_adapter")
    def test_creates_missing_github_labels(self, mock_client_factory):
        """Test creating missing labels on GitHub."""
        mock_client = Mock()
        mock_client_factory.return_value = mock_client

        state = DetectedState(
            repo="owner/repo",
            github_labels=[],  # No labels on GitHub
            agent_labels=[],
            existing_config={
                "repo": "owner/repo",
                "agents": {
                    "agent:web": {"prompt": ".prompts/web.md", "model": "sonnet", "timeout_minutes": 45},
                },
                "concurrency": {"max_concurrent_sessions": 3},
            },
            config_path=Path(".issue-orchestrator.yaml"),
            prompt_candidates=[],
        )

        prompter = MockPrompter([
            True,                   # update existing config
            # No unconfigured agents
            # agent:web is configured but missing from GitHub
            True,                   # create missing labels
            # Milestone sort missing
            "due_date",             # milestone sort strategy
            # Worktree missing
            "../",
            # UI mode missing
            "web",
            "8080",
            # Label prefix
            "",                     # label prefix (none)
            # Review
            False,
        ])

        config, _ = wizard_existing_project(state, prompter)

        assert mock_client.create_label.called

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard._github_adapter")
    def test_fresh_config_when_declined(self, mock_client_factory):
        """Test starting fresh when declining to update existing config."""
        mock_client_factory.return_value = Mock()

        state = DetectedState(
            repo="owner/repo",
            github_labels=["agent:web"],
            agent_labels=["agent:web"],
            existing_config={
                "repo": "owner/repo",
                "agents": {"agent:old": {"prompt": ".prompts/old.md", "model": "haiku", "timeout_minutes": 30}},
            },
            config_path=Path(".issue-orchestrator.yaml"),
            prompt_candidates=[],
        )

        prompter = MockPrompter([
            False,                  # DON'T update existing config - start fresh
            # Now asks about agent:web since we started fresh
            True,                   # add agent:web
            ".prompts/web.md",
            "45",                   # timeout
            "claude",               # agent type
            "sonnet",               # model
            "default",              # permission mode
            # Concurrency (fresh config needs this)
            "2",
            # Milestone sort (fresh config needs this)
            "due_date",             # milestone sort strategy
            # Worktree
            "../",
            # UI mode (fresh)
            "tmux",
            # Label prefix
            "",                     # label prefix (none)
            # Review
            False,
        ])

        config, _ = wizard_existing_project(state, prompter)

        # Old agent should NOT be in config
        assert "agent:old" not in config["agents"]
        # New agent should be
        assert "agent:web" in config["agents"]

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard._github_adapter")
    def test_prompts_for_repo_when_not_detected(self, mock_client_factory):
        """Test that repo is prompted when not in state or config."""
        mock_client_factory.return_value = Mock()

        state = DetectedState(
            repo=None,  # Not detected
            github_labels=[],
            agent_labels=[],
            existing_config=None,
            config_path=None,
            prompt_candidates=[],
        )

        prompter = MockPrompter([
            "manual/repo",          # manual repo entry
            # No agents to configure, so no worktree prompt
            "3",                    # concurrency
            "due_date",             # milestone sort strategy
            "web",                  # ui mode
            "8080",                 # port (since web mode)
            "",                     # label prefix (none)
            False,                  # no review
        ])

        config, _ = wizard_existing_project(state, prompter)

        assert config["repo"] == "manual/repo"


class TestRunWizard:
    """Test the run_wizard function."""

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.check_prerequisites")
    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.scan_existing_repo")
    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.write_config")
    @patch("os.chdir")
    def test_new_project_flow(self, mock_chdir, mock_write, mock_scan, mock_prereqs, tmp_path):
        """Test the full wizard flow for a new project."""
        mock_prereqs.return_value = {"git": True, "github_auth": True, "claude": True}
        mock_scan.return_value = DetectedState(repo="owner/repo")

        # Create target directory
        target = tmp_path / "myproject"
        target.mkdir()

        prompter = MockPrompter([
            # Mode choice (no directory prompt since we pass target_path)
            "New project - set up from scratch",
            # wizard_new_project answers
            "owner/repo",
            "agent:backend",
            ".prompts/backend.md",
            "45",                   # timeout
            "claude",               # agent type
            "sonnet",               # model
            "default",              # permission mode
            "",                     # finish agents
            False,                  # don't create agent labels on GitHub
            "3",
            "due_date",             # milestone sort strategy
            "../",
            "web",
            "8080",
            "",                     # label prefix (none)
            False,                  # no review workflow
            # Post-wizard
            True,                   # save config
            ".issue-orchestrator.yaml",  # output path
            True,                   # overwrite existing (asked since os.chdir is mocked and we're in repo root)
            True,                   # create prompt file
            False,                  # don't create missing GitHub labels
        ])

        with patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.detect_repo", return_value="owner/repo"):
            with patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard._github_adapter", return_value=Mock()):
                run_wizard(target_path=target, prompter=prompter)

        # Verify config was written
        assert mock_write.called

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.check_prerequisites")
    @patch("os.chdir")
    def test_aborts_when_config_not_saved(self, mock_chdir, mock_prereqs, tmp_path):
        """Test that wizard aborts when user doesn't save config."""
        mock_prereqs.return_value = {"git": True, "github_auth": True, "claude": True}

        target = tmp_path / "myproject"
        target.mkdir()

        prompter = MockPrompter([
            # No directory prompt since we pass target_path
            "New project - set up from scratch",
            "owner/repo",
            "agent:backend",
            ".prompts/backend.md",
            "45",                   # timeout
            "claude",               # agent type
            "sonnet",               # model
            "default",              # permission mode
            "",
            False,                  # don't create agent labels
            "3",
            "due_date",             # milestone sort strategy
            "../",
            "web",
            "8080",
            "",                     # label prefix (none)
            False,                  # no review workflow
            False,                  # DON'T save config (exits here)
        ])

        with patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.detect_repo", return_value="owner/repo"):
            with patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard._github_adapter", return_value=Mock()):
                with pytest.raises(SystemExit):
                    run_wizard(target_path=target, prompter=prompter)

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.check_prerequisites")
    @patch("os.chdir")
    def test_warns_on_missing_prerequisites(self, mock_chdir, mock_prereqs, tmp_path):
        """Test that wizard warns when prerequisites are missing."""
        mock_prereqs.return_value = {
            "git": True,
            "github_auth": False,  # Not authenticated
            "claude": False,   # Not installed
        }

        target = tmp_path / "myproject"
        target.mkdir()

        prompter = MockPrompter([
            # No directory prompt since we pass target_path
            False,                  # Don't continue without prereqs
        ])

        with pytest.raises(SystemExit):
            run_wizard(target_path=target, prompter=prompter)

        # Check that warning was printed
        assert any("prerequisites" in msg.lower() or "missing" in msg.lower()
                  for msg in prompter.printed)

    @patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.check_prerequisites")
    @patch("os.chdir")
    def test_continues_despite_missing_prerequisites(self, mock_chdir, mock_prereqs, tmp_path):
        """Test that wizard can continue despite missing prerequisites."""
        mock_prereqs.return_value = {
            "git": True,
            "github_auth": False,
            "claude": False,
        }

        target = tmp_path / "myproject"
        target.mkdir()

        prompter = MockPrompter([
            # No directory prompt since we pass target_path
            True,                   # Continue anyway
            "New project - set up from scratch",
            "owner/repo",
            "agent:backend",
            ".prompts/backend.md",
            "45",                   # timeout
            "claude",               # agent type
            "sonnet",               # model
            "default",              # permission mode
            "",
            False,                  # don't create agent labels
            "3",
            "due_date",             # milestone sort strategy
            "../",
            "web",
            "8080",
            "",                     # label prefix (none)
            False,                  # no review workflow
            False,                  # Don't save - exits here
        ])

        with patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard.detect_repo", return_value=None):
            with patch("issue_orchestrator.entrypoints.cli_tools.setup_wizard._github_adapter", return_value=Mock()):
                with pytest.raises(SystemExit):
                    run_wizard(target_path=target, prompter=prompter)
