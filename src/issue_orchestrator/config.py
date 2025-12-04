"""Configuration loading and management."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .models import AgentConfig


@dataclass
class Config:
    """Orchestrator configuration."""

    # Agent configurations keyed by label (e.g., "agent:web")
    agents: dict[str, AgentConfig] = field(default_factory=dict)

    # Concurrency settings
    max_sessions: int = 3
    session_timeout_minutes: int = 45

    # Label names (customizable)
    label_in_progress: str = "in-progress"
    label_blocked: str = "blocked"
    label_needs_human: str = "needs-human"

    # Paths
    state_file: Path = Path(".issue-orchestrator/state.json")
    repo_root: Path = field(default_factory=Path.cwd)  # Root of the git repository

    # GitHub settings
    repo: Optional[str] = None  # owner/repo, or None to auto-detect
    filter_label: Optional[str] = None  # Only consider issues with this label (e.g., "test-data")

    @classmethod
    def load(cls, config_path: Path) -> "Config":
        """Load configuration from YAML file."""
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        with open(config_path) as f:
            data = yaml.safe_load(f)

        config = cls()

        # Parse agents
        for label, agent_data in data.get("agents", {}).items():
            config.agents[label] = AgentConfig(
                prompt_path=Path(agent_data["prompt"]),
                worktree_base=Path(agent_data.get("worktree_base", "../")),
                model=agent_data.get("model", "sonnet"),
                timeout_minutes=agent_data.get("timeout_minutes", 45),
                command=agent_data.get(
                    "command",
                    "claude --dangerously-skip-permissions --model {model} --append-system-prompt 'Read {prompt} for your instructions. You are working on issue #{issue_number}: {issue_title}'",
                ),
            )

        # Parse concurrency
        concurrency = data.get("concurrency", {})
        config.max_sessions = concurrency.get("max_sessions", 3)
        config.session_timeout_minutes = concurrency.get("session_timeout_minutes", 45)

        # Parse labels
        labels = data.get("labels", {})
        config.label_in_progress = labels.get("in_progress", "in-progress")
        config.label_blocked = labels.get("blocked", "blocked")
        config.label_needs_human = labels.get("needs_human", "needs-human")

        # GitHub settings
        config.repo = data.get("repo")
        config.filter_label = data.get("filter_label")

        return config

    @classmethod
    def find_and_load(cls, start_path: Optional[Path] = None) -> "Config":
        """Find config file in current or parent directories and load it."""
        search_path = start_path or Path.cwd()

        for path in [search_path, *search_path.parents]:
            config_file = path / ".issue-orchestrator.yaml"
            if config_file.exists():
                config = cls.load(config_file)
                config.repo_root = path.resolve()
                return config

            # Also check .issue-orchestrator/config.yaml
            config_file = path / ".issue-orchestrator" / "config.yaml"
            if config_file.exists():
                config = cls.load(config_file)
                config.repo_root = path.resolve()
                return config

        raise FileNotFoundError(
            "No .issue-orchestrator.yaml found in current or parent directories"
        )
