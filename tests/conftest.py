"""Shared fixtures and configuration for tests."""

import pytest
from pathlib import Path
from unittest.mock import MagicMock
from issue_orchestrator.models import AgentConfig, Issue
from issue_orchestrator.config import Config


@pytest.fixture
def sample_agent_config(tmp_path):
    """Create a sample agent config for testing."""
    prompt_file = tmp_path / "prompt.txt"
    prompt_file.write_text("Test prompt")

    return AgentConfig(
        prompt_path=prompt_file,
        worktree_base=tmp_path,
        model="sonnet",
        timeout_minutes=45,
    )


@pytest.fixture
def sample_config(sample_agent_config):
    """Create a sample Config object for testing."""
    config = Config()
    config.agents["agent:web"] = sample_agent_config
    config.max_concurrent_sessions = 3
    config.session_timeout_minutes = 45
    config.ui_mode = "tmux"  # Avoid iTerm2 detection during tests
    return config


@pytest.fixture
def sample_issues():
    """Create sample issues for testing."""
    return [
        Issue(
            number=1,
            title="High priority task",
            labels=["priority:high", "agent:web"],
            body="This is a high priority issue",
        ),
        Issue(
            number=2,
            title="Medium priority task",
            labels=["priority:medium", "agent:web"],
            body="This is a medium priority issue",
        ),
        Issue(
            number=3,
            title="Low priority task",
            labels=["priority:low", "agent:mobile"],
            body="This is a low priority issue",
        ),
        Issue(
            number=4,
            title="Blocked issue",
            labels=["blocked", "agent:web"],
            body="This issue is blocked by #1",
        ),
        Issue(
            number=5,
            title="In-progress issue",
            labels=["in-progress", "agent:web"],
            body="Currently being worked on",
        ),
    ]


@pytest.fixture
def sample_issue_with_dependencies():
    """Create issues with various dependency mentions for testing."""
    return [
        Issue(
            number=101,
            title="First issue",
            labels=["priority:high"],
            body="This is the first issue",
        ),
        Issue(
            number=102,
            title="Depends on first",
            labels=["priority:medium"],
            body="This is blocked by #101",
        ),
        Issue(
            number=103,
            title="Multiple dependencies",
            labels=["priority:low"],
            body="Blocked by #101 and depends on #102",
        ),
        Issue(
            number=104,
            title="After implementation",
            labels=["priority:medium"],
            body="This should be done after #101",
        ),
        Issue(
            number=105,
            title="Requires other work",
            labels=["priority:high"],
            body="Requires #101 and #102 to be completed",
        ),
        Issue(
            number=106,
            title="Waiting for someone",
            labels=["priority:low"],
            body="Waiting for #104 to complete before starting",
        ),
    ]


@pytest.fixture
def mock_github_api():
    """Create a mock GitHub API object."""
    mock = MagicMock()
    mock.get_issues.return_value = []
    mock.add_label.return_value = None
    mock.remove_label.return_value = None
    return mock


@pytest.fixture
def mock_config_yaml(tmp_path):
    """Create a temporary config YAML file."""
    config_content = """
agents:
  agent:web:
    prompt: /path/to/web_prompt.txt
    worktree_base: /path/to/worktrees
    model: sonnet
    timeout_minutes: 45
  agent:mobile:
    prompt: /path/to/mobile_prompt.txt
    worktree_base: /path/to/worktrees
    model: sonnet
    timeout_minutes: 60

concurrency:
  max_sessions: 3
  session_timeout_minutes: 45

labels:
  in_progress: in-progress
  blocked: blocked
  needs_human: needs-human

repo: owner/repo
"""
    config_file = tmp_path / ".issue-orchestrator.yaml"
    config_file.write_text(config_content)
    return config_file
