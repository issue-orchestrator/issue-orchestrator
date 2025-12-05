"""Configuration loading and management."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .models import AgentConfig, CommentHeadings


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
    label_prefix: Optional[str] = None  # Optional prefix for all labels (e.g., "bot")

    # Paths
    state_file: Path = Path(".issue-orchestrator/state.json")
    repo_root: Path = field(default_factory=Path.cwd)  # Root of the git repository

    # GitHub settings
    repo: Optional[str] = None  # owner/repo, or None to auto-detect
    filter_label: Optional[str] = None  # Only consider issues with this label (e.g., "test-data")
    filter_milestone: Optional[str] = None  # Only consider issues in this milestone

    # Comment headings for structured worker comments
    comment_headings: CommentHeadings = field(default_factory=CommentHeadings)

    def prefixed_label(self, label: str) -> str:
        """Return label with prefix if configured.

        Args:
            label: The base label name

        Returns:
            The label with prefix if configured, otherwise the original label
        """
        if self.label_prefix:
            return f"{self.label_prefix}:{label}"
        return label

    def get_label_in_progress(self) -> str:
        """Get the in-progress label with prefix if configured."""
        return self.prefixed_label(self.label_in_progress)

    def get_label_blocked(self) -> str:
        """Get the blocked label with prefix if configured."""
        return self.prefixed_label(self.label_blocked)

    def get_label_needs_human(self) -> str:
        """Get the needs-human label with prefix if configured."""
        return self.prefixed_label(self.label_needs_human)

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
            agent_kwargs = {
                "prompt_path": Path(agent_data["prompt"]),
                "worktree_base": Path(agent_data.get("worktree_base", "../")),
                "model": agent_data.get("model", "sonnet"),
                "timeout_minutes": agent_data.get("timeout_minutes", 45),
            }
            if "command" in agent_data:
                agent_kwargs["command"] = agent_data["command"]
            if "repo_root" in agent_data:
                agent_kwargs["repo_root"] = Path(agent_data["repo_root"])
            config.agents[label] = AgentConfig(**agent_kwargs)

        # Parse concurrency
        concurrency = data.get("concurrency", {})
        config.max_sessions = concurrency.get("max_sessions", 3)
        config.session_timeout_minutes = concurrency.get("session_timeout_minutes", 45)

        # Parse labels
        labels = data.get("labels", {})
        config.label_in_progress = labels.get("in_progress", "in-progress")
        config.label_blocked = labels.get("blocked", "blocked")
        config.label_needs_human = labels.get("needs_human", "needs-human")
        config.label_prefix = labels.get("prefix")

        # GitHub settings
        config.repo = data.get("repo")
        config.filter_label = data.get("filter_label")
        config.filter_milestone = data.get("filter_milestone")

        # Parse comment headings
        headings_data = data.get("comment_headings", {})
        if headings_data:
            config.comment_headings = CommentHeadings(
                implementation=headings_data.get("implementation", "## Implementation"),
                problems=headings_data.get("problems", "## Problems Encountered"),
                pr_link=headings_data.get("pr_link", "## Pull Request"),
                blocked=headings_data.get("blocked", "## Blocked"),
                needs_human=headings_data.get("needs_human", "## Needs Human Input"),
            )

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
