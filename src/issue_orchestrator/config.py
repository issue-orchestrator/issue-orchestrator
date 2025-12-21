"""Configuration loading and management."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from .models import AgentConfig, CommentHeadings


@dataclass
class CleanupWithTriage:
    """Cleanup settings when triage review is enabled."""
    close_ai_session_tabs: bool = True
    remove_worktrees: bool = False


@dataclass
class CleanupWithoutTriage:
    """Cleanup settings when triage review is NOT enabled."""
    wait_for_code_review: bool = True  # True = after code review, False = on completion
    close_ai_session_tabs: bool = True
    remove_worktrees: bool = False


@dataclass
class CleanupConfig:
    """Cleanup configuration - when to close tabs and remove worktrees."""
    with_triage: CleanupWithTriage = field(default_factory=CleanupWithTriage)
    without_triage: CleanupWithoutTriage = field(default_factory=CleanupWithoutTriage)


@dataclass
class DangerousConfig:
    """Dangerous configuration options that bypass safety guardrails.

    These options should only be used for testing or in environments
    where you understand the risks.
    """
    skip_verification: bool = False  # Skip hook verification on startup
    allow_unsupported_agents: bool = False  # Allow agents without hook support


@dataclass
class Config:
    """Orchestrator configuration."""

    # Agent configurations keyed by label (e.g., "agent:web")
    agents: dict[str, AgentConfig] = field(default_factory=dict)

    # Concurrency settings
    max_concurrent_sessions: int = 3
    session_timeout_minutes: int = 45

    # Label names (customizable)
    label_in_progress: str = "in-progress"
    label_blocked: str = "blocked"
    label_needs_human: str = "needs-human"
    label_needs_rework: str = "needs-rework"  # Applied to PR when reviewer requests changes
    label_prefix: Optional[str] = None  # Optional prefix for all labels (e.g., "bot")

    # Paths
    state_file: Path = Path(".issue-orchestrator/state.json")
    repo_root: Path = field(default_factory=Path.cwd)  # Root of the git repository

    # GitHub settings
    repo: Optional[str] = None  # owner/repo, or None to auto-detect
    filter_label: Optional[str] = None  # Only consider issues with this label (e.g., "test-data")
    filter_milestone: Optional[str] = None  # Only consider issues in this milestone
    filter_issue: Optional[int] = None  # Only process this specific issue number
    issue_fetch_limit: int = 100  # Max issues to fetch per API call (gh default is 30)

    # Comment headings for structured worker comments
    comment_headings: CommentHeadings = field(default_factory=CommentHeadings)

    # UI mode: "web" (default, browser dashboard), "tmux", "iterm2" (Mac iTerm2 tabs)
    ui_mode: str = "web"
    web_port: int = 8080  # Port for web dashboard
    queue_refresh_seconds: int = 600  # How often web UI refetches queue from GitHub (0 = manual only)

    # Terminal adapter (optional - overrides ui_mode if set)
    # Can be "builtin:tmux", "builtin:iterm2", or a full class path
    terminal_adapter: Optional[str] = None

    # Session limits
    max_issues_to_start: int = 0  # Max issues to start processing (0 = unlimited)

    # Milestone sorting strategy - built-in: "due_date", "number", "pattern", "name"
    # Or provide a custom class path like "mymodule.MyStrategy"
    milestone_sort: str = "due_date"
    # Config passed to strategy via **kwargs (e.g., pattern="M(\\d+)" for PatternStrategy)
    milestone_sort_config: dict = field(default_factory=dict)

    # Cleanup configuration - when to close AI session tabs and remove worktrees
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)

    # Legacy tab cleanup behavior (deprecated - use cleanup section instead)
    close_completed_tabs: bool = True   # Auto-close tabs for successful completions (has PR)
    close_failed_tabs: bool = False     # Auto-close tabs for failed sessions (leave open to investigate)

    # Enforcement options
    enforce_hooks: bool = True  # Install pre-push hooks to enforce structured comments
    pre_push_hook: Optional[Path] = None  # Custom pre-push hook path (uses bundled if None)

    # Worktree setup commands (run after worktree creation, e.g., npm install)
    setup_worktree: list[str] = field(default_factory=list)

    # Code review workflow (optional) - per-PR review after agent creates PR
    code_review_agent: Optional[str] = None  # Agent that reviews PRs (e.g., "agent:reviewer")
    code_review_label: Optional[str] = None  # Label on PRs needing review (e.g., "needs-code-review")
    code_reviewed_label: Optional[str] = None  # Label after review passes (e.g., "code-reviewed")

    # Triage/batch review workflow (optional) - pattern review across multiple PRs
    triage_review_agent: Optional[str] = None  # Agent that does batch reviews (e.g., "agent:triage")
    triage_review_label: Optional[str] = None  # Label for PRs awaiting triage review (uses code_reviewed_label if not set)
    triage_reviewed_label: Optional[str] = None  # Label after triage review (e.g., "triage-reviewed")
    triage_review_threshold: int = 0  # Trigger triage review after N PRs (0 = manual only)
    triage_review_on_failure: bool = True  # Trigger triage to investigate when sessions fail

    # Rework cycle limit (when reviewer requests changes)
    max_rework_cycles: int = 2  # Max times to re-queue work agent before escalating to needs-human

    # Dangerous options (use with caution)
    dangerous: DangerousConfig = field(default_factory=DangerousConfig)

    # Path to the config file (set during load)
    config_path: Optional[Path] = None

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

    def get_label_needs_rework(self) -> str:
        """Get the needs-rework label with prefix if configured."""
        return self.prefixed_label(self.label_needs_rework)

    @classmethod
    def load(cls, config_path: Path) -> "Config":
        """Load configuration from YAML file."""
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        with open(config_path) as f:
            data = yaml.safe_load(f)

        config = cls()
        config.config_path = config_path.resolve()
        # Default repo_root to config file's parent directory
        # (can be overridden in find_and_load or by YAML settings)
        config.repo_root = config_path.parent.resolve()

        # Parse agents
        for label, agent_data in data.get("agents", {}).items():
            # Resolve prompt path relative to repo_root (not worktree) so prompts work
            # even when the branch doesn't have the prompt file
            prompt_path = Path(agent_data["prompt"])
            if not prompt_path.is_absolute():
                # Get repo_root for this agent (or use config.repo_root which we set below)
                # If repo_root is relative, resolve it relative to config file location
                if "repo_root" in agent_data:
                    agent_repo_root = Path(agent_data["repo_root"])
                    if not agent_repo_root.is_absolute():
                        agent_repo_root = (config_path.parent / agent_repo_root).resolve()
                else:
                    agent_repo_root = config_path.parent
                prompt_path = (agent_repo_root / prompt_path).resolve()

            # Resolve worktree_base - MUST be absolute for reliable operation
            # Relative paths are resolved relative to config file location
            worktree_base_raw = agent_data.get("worktree_base")
            if worktree_base_raw is None:
                # Default to sibling directory of repo
                worktree_base = (config_path.parent.parent / "worktrees").resolve()
            else:
                worktree_base = Path(worktree_base_raw)
                if not worktree_base.is_absolute():
                    worktree_base = (config_path.parent / worktree_base).resolve()

            # Validate worktree_base is usable (create if needed, fail fast if not)
            try:
                worktree_base.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                raise ValueError(
                    f"Agent '{label}': worktree_base '{worktree_base}' cannot be created: {e}. "
                    f"Specify an absolute path in your config under agents.{label}.worktree_base"
                )

            agent_kwargs = {
                "prompt_path": prompt_path,
                "worktree_base": worktree_base,
                "model": agent_data.get("model", "sonnet"),
                "timeout_minutes": agent_data.get("timeout_minutes", 45),
                "permission_mode": agent_data.get("permission_mode", "default"),
                "skip_review": agent_data.get("skip_review", False),
            }
            if "command" in agent_data:
                agent_kwargs["command"] = agent_data["command"]
            if "repo_root" in agent_data:
                # Resolve repo_root relative to config file if not absolute
                repo_root_path = Path(agent_data["repo_root"])
                if not repo_root_path.is_absolute():
                    repo_root_path = (config_path.parent / repo_root_path).resolve()
                agent_kwargs["repo_root"] = repo_root_path
            config.agents[label] = AgentConfig(**agent_kwargs)

        # Parse concurrency
        concurrency = data.get("concurrency", {})
        config.max_concurrent_sessions = concurrency.get("max_concurrent_sessions", 3)
        config.session_timeout_minutes = concurrency.get("session_timeout_minutes", 45)

        # Parse labels
        labels = data.get("labels", {})
        config.label_in_progress = labels.get("in_progress", "in-progress")
        config.label_blocked = labels.get("blocked", "blocked")
        config.label_needs_human = labels.get("needs_human", "needs-human")
        config.label_needs_rework = labels.get("needs_rework", "needs-rework")
        config.label_prefix = labels.get("prefix")

        # GitHub settings
        config.repo = data.get("repo")
        config.filter_label = data.get("filter_label")
        config.filter_milestone = data.get("filter_milestone")
        config.issue_fetch_limit = data.get("issue_fetch_limit", 100)

        # UI mode
        config.ui_mode = data.get("ui_mode", "web")
        config.web_port = data.get("web_port", 8080)
        config.queue_refresh_seconds = data.get("queue_refresh_seconds", 600)

        # Terminal adapter (overrides ui_mode if set)
        config.terminal_adapter = data.get("terminal_adapter")

        # Session limits
        config.max_issues_to_start = data.get("max_issues_to_start", 0)

        # Milestone sorting strategy
        config.milestone_sort = data.get("milestone_sort", "due_date")
        config.milestone_sort_config = data.get("milestone_sort_config", {})

        # Tab cleanup behavior
        config.close_completed_tabs = data.get("close_completed_tabs", True)
        config.close_failed_tabs = data.get("close_failed_tabs", False)

        # Enforcement options
        config.enforce_hooks = data.get("enforce_hooks", True)
        if data.get("pre_push_hook"):
            config.pre_push_hook = Path(data["pre_push_hook"])

        # Worktree setup commands
        config.setup_worktree = data.get("setup_worktree", [])

        # Review workflow
        review_config = data.get("review", {})

        # Code review (per-PR, immediate)
        config.code_review_agent = review_config.get("code_review_agent")
        config.code_review_label = review_config.get("code_review_label", "needs-code-review")
        config.code_reviewed_label = review_config.get("code_reviewed_label", "code-reviewed")

        # Triage review (batch) - supports both triage_* and cto_* keys for backwards compat
        config.triage_review_agent = review_config.get("triage_review_agent") or review_config.get("cto_review_agent")
        config.triage_review_label = review_config.get("triage_review_label") or review_config.get("cto_review_label")
        config.triage_reviewed_label = review_config.get("triage_reviewed_label") or review_config.get("cto_reviewed_label", "triage-reviewed")
        config.triage_review_threshold = review_config.get("triage_review_threshold") or review_config.get("cto_review_threshold", 0)
        config.triage_review_on_failure = review_config.get("triage_review_on_failure", review_config.get("cto_review_on_failure", True))

        # Rework cycle limit
        config.max_rework_cycles = review_config.get("max_rework_cycles", 2)

        # Backwards compatibility: map old fields to new
        if "agent" in review_config and not config.triage_review_agent:
            config.triage_review_agent = review_config["agent"]
        if "label" in review_config and not config.code_review_label:
            config.code_review_label = review_config["label"]
        if "threshold" in review_config and config.triage_review_threshold == 0:
            config.triage_review_threshold = review_config["threshold"]

        # Parse cleanup config - supports both triage and cto keys for backwards compat
        cleanup_data = data.get("cleanup", {})
        if cleanup_data:
            with_triage_data = cleanup_data.get("with_triage", {}) or cleanup_data.get("with_cto", {})
            without_triage_data = cleanup_data.get("without_triage", {}) or cleanup_data.get("without_cto", {})

            config.cleanup = CleanupConfig(
                with_triage=CleanupWithTriage(
                    close_ai_session_tabs=with_triage_data.get("close_ai_session_tabs", True),
                    remove_worktrees=with_triage_data.get("remove_worktrees", False),
                ),
                without_triage=CleanupWithoutTriage(
                    wait_for_code_review=without_triage_data.get("wait_for_code_review", True),
                    close_ai_session_tabs=without_triage_data.get("close_ai_session_tabs", True),
                    remove_worktrees=without_triage_data.get("remove_worktrees", False),
                ),
            )

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

        # Parse dangerous config
        dangerous_data = data.get("dangerous", {})
        if dangerous_data:
            config.dangerous = DangerousConfig(
                skip_verification=dangerous_data.get("skip_verification", False),
                allow_unsupported_agents=dangerous_data.get("allow_unsupported_agents", False),
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

    def validate(self) -> list[str]:
        """Validate configuration and return list of errors.

        Call this at startup to catch configuration problems early.
        Returns empty list if valid, list of error messages otherwise.
        """
        errors = []

        # Validate agents
        if not self.agents:
            errors.append("No agents configured. Add at least one agent under 'agents:' in config.")

        for label, agent in self.agents.items():
            # Validate prompt file exists
            if not agent.prompt_path.exists():
                errors.append(
                    f"Agent '{label}': prompt file not found: {agent.prompt_path}"
                )

            # Validate worktree_base is absolute (should be resolved by load())
            if not agent.worktree_base.is_absolute():
                errors.append(
                    f"Agent '{label}': worktree_base must be absolute path, got: {agent.worktree_base}"
                )

            # Validate worktree_base exists and is writable
            if not agent.worktree_base.exists():
                errors.append(
                    f"Agent '{label}': worktree_base does not exist: {agent.worktree_base}"
                )
            elif not agent.worktree_base.is_dir():
                errors.append(
                    f"Agent '{label}': worktree_base is not a directory: {agent.worktree_base}"
                )

            # Validate model is known
            known_models = {"haiku", "sonnet", "opus"}
            if agent.model not in known_models:
                errors.append(
                    f"Agent '{label}': unknown model '{agent.model}'. Known: {known_models}"
                )

        # Validate review workflow references valid agents
        if self.code_review_agent and self.code_review_agent not in self.agents:
            errors.append(
                f"code_review_agent '{self.code_review_agent}' not found in agents. "
                f"Available: {list(self.agents.keys())}"
            )

        if self.triage_review_agent and self.triage_review_agent not in self.agents:
            errors.append(
                f"triage_review_agent '{self.triage_review_agent}' not found in agents. "
                f"Available: {list(self.agents.keys())}"
            )

        return errors

    def validate_or_raise(self) -> None:
        """Validate configuration, raising ValueError if invalid."""
        errors = self.validate()
        if errors:
            raise ValueError(
                "Configuration errors:\n  - " + "\n  - ".join(errors)
            )
