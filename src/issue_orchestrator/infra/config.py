"""Configuration loading and management."""

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional, TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from ..domain.issue_filter import IssueLabelFilter

from ..domain.models import AgentConfig, CommentHeadings

# Config directory structure
CONFIG_DIR = ".issue-orchestrator/config"
DEFAULT_CONFIG_NAME = "default.yaml"

# Valid top-level config fields (for unknown field validation)
ALLOWED_TOP_LEVEL_FIELDS = {
    'repo', 'agents', 'concurrency', 'labels', 'ui_mode', 'web_port',
    'control_api_port', 'config', 'worktree_base',
    'github_token', 'github_token_env', 'github_api_url',
    'github_http_timeout_seconds', 'github_cache_ttl_seconds', 'github_required_scopes', 'github_allowed_scopes',
    'filtering',  # Issue filtering options (label, milestone, exclude_labels, etc.)
    'e2e_pr_labels', 'e2e', 'review', 'cleanup', 'validation', 'triage',
    'isolation', 'setup_worktree', 'milestone_sort',
    'milestone_sort_config', 'foundation_milestone',
    'state_file', 'pre_push_hook', 'enforce_hooks', 'queue_refresh_seconds',
    'terminal_adapter', 'session_no_output_seconds', 'session_no_output_tail_lines',
    'session_no_output_max_bytes', 'session_no_output_repeat_seconds',
    'gh_write_verify_timeout_seconds', 'gh_write_verify_initial_delay_ms',
    'gh_write_verify_max_delay_ms', 'gh_write_verify_backoff', 'gh_write_verify_jitter_ms',
    'gh_rate_limit_startup', 'gh_rate_limit_every_calls', 'gh_rate_limit_warn_fraction',
    'gh_rate_limit_warn_remaining', 'gh_audit_enabled', 'gh_audit_events', 'gh_audit_file',
    'comment_headings', 'dangerous', 'stale_escalation_ticks',
    'tmuxp',  # Optional path to custom tmuxp config for tmux layout
    'tmux_bindings',  # List of tmux bind-key commands to apply
    'tmux_session_mode',  # shared (one session) or per_session
}

# Valid per-agent config fields (worktree_base and repo_root removed - now top-level only)
ALLOWED_AGENT_FIELDS = {
    'prompt', 'model', 'timeout_minutes',
    'permission_mode', 'skip_review', 'reviewer', 'command',
    'meta_agent', 'initial_prompt', 'ai_system',
}


def repo_root_from_config_path(config_path: Path) -> Path:
    """Get the repo root from a config file path.

    Configs live at <repo>/.issue-orchestrator/config/<name>.yaml
    So repo root is 3 levels up from the config file.

    This is the SINGLE SOURCE OF TRUTH for this calculation.
    """
    return config_path.parent.parent.parent.resolve()


def resolve_relative_path(path: str | Path, repo_root: Path) -> Path:
    """Resolve a path relative to repo root if not absolute.

    This is the standard way to resolve any user-provided path in config.
    """
    p = Path(path)
    if p.is_absolute():
        return p.resolve()
    return (repo_root / p).resolve()


def get_config_dir(repo_root: Path) -> Path:
    """Get the config directory for a repo."""
    return repo_root / CONFIG_DIR


def list_configs(repo_root: Path) -> list[str]:
    """List available config files in a repo's config directory.

    Returns list of config filenames (e.g., ['default.yaml', 'test.yaml']).
    Returns empty list if config dir doesn't exist or has no yaml files.
    """
    config_dir = get_config_dir(repo_root)
    if not config_dir.exists():
        return []

    configs = sorted(
        f.name for f in config_dir.glob("*.yaml")
        if f.is_file()
    )
    # Put default.yaml first if it exists
    if DEFAULT_CONFIG_NAME in configs:
        configs.remove(DEFAULT_CONFIG_NAME)
        configs.insert(0, DEFAULT_CONFIG_NAME)
    return configs


def get_config_path(repo_root: Path, config_name: str = DEFAULT_CONFIG_NAME) -> Path:
    """Get the full path to a config file."""
    return get_config_dir(repo_root) / config_name


def config_exists(repo_root: Path, config_name: str = DEFAULT_CONFIG_NAME) -> bool:
    """Check if a config file exists."""
    return get_config_path(repo_root, config_name).exists()


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
    allow_unsupported_agents: bool = False  # Allow agents without hook support


@dataclass
class ValidationConfig:
    """Validation configuration - single command runs everywhere.

    The same validation command runs:
    - On agent-done: gives agent immediate feedback to fix issues
    - On pre-push: cached by SHA, instant pass if already validated

    This ensures agents can't "pass" a quick check only to fail later.
    """
    cmd: Optional[str] = None  # Command to run (e.g., "make validate")
    timeout_seconds: int = 300  # Default 5 minutes


@dataclass
class IsolationConfig:
    """Agent isolation configuration."""
    mode: str = "standard"  # "standard" or "hardened"


@dataclass
class FilteringConfig:
    """Issue filtering configuration.

    Controls which issues the orchestrator will process.
    """
    label: Optional[str] = None  # Only process issues with this label
    milestone: Optional[str] = None  # Only process issues in this milestone
    milestones: list[str] = field(default_factory=list)  # Process issues in any of these milestones
    issue: Optional[int] = None  # Only process this specific issue number
    exclude_labels: list[str] = field(default_factory=list)  # Exclude issues with any of these labels
    fetch_limit: int = 100  # Max issues to fetch per API call
    max_to_start: int = 0  # Stop after starting this many issues (0 = unlimited)

    def get_milestones(self) -> list[str]:
        """Return list of milestone filters (supports both single and list)."""
        if self.milestones:
            return list(self.milestones)
        if self.milestone:
            return [self.milestone]
        return []


@dataclass
class MilestoneStrategyConfig:
    """Milestone assignment strategy for triage issues."""
    inherit_from_issues: Optional[str] = "latest"  # "earliest" | "latest" | None
    explicit: Optional[str] = None  # Explicit milestone name


@dataclass
class TriageConfig:
    """Triage issue configuration.

    Controls how labels and milestones are assigned to orchestrator-created
    triage issues.
    """
    # Labels to inherit from source issues (if any source issue has the label)
    inherit_labels: list[str] = field(default_factory=list)

    # Labels always applied to triage issues
    explicit_labels: list[str] = field(default_factory=list)

    # Milestone assignment strategy
    milestone_strategy: MilestoneStrategyConfig = field(default_factory=MilestoneStrategyConfig)

    # Optional explicit priority label
    priority: Optional[str] = None


@dataclass
class E2EConfig:
    """E2E async test runner settings.

    Controls local async E2E test execution with results persisted to SQLite.
    """
    enabled: bool = False  # Whether E2E runner is active
    auto_run_interval_minutes: int = 30  # Min interval between auto runs (0 = disable auto)
    pytest_args: list[str] = field(default_factory=lambda: ["tests/e2e", "-v"])
    allow_retry_once: bool = True  # Retry failing tests once to reduce flakiness
    quarantine_file: str = "tests/e2e/quarantine.txt"  # Path to quarantine list
    survive_restart: bool = True  # Let worker finish if orchestrator restarts


def _parse_e2e_config(data: dict) -> E2EConfig:
    """Parse e2e section from YAML data."""
    pytest_args = data.get("pytest_args") or ["tests/e2e", "-v"]
    if isinstance(pytest_args, str):
        # Support space-separated string
        pytest_args = pytest_args.split()

    return E2EConfig(
        enabled=data.get("enabled", False),
        auto_run_interval_minutes=data.get("auto_run_interval_minutes", 30),
        pytest_args=list(pytest_args),
        allow_retry_once=data.get("allow_retry_once", True),
        quarantine_file=data.get("quarantine_file", "tests/e2e/quarantine.txt"),
        survive_restart=data.get("survive_restart", True),
    )


def _parse_triage_config(data: dict) -> TriageConfig:
    """Parse triage section from YAML data."""
    # Parse lists (support comma-separated strings)
    inherit_labels = data.get("inherit_labels") or []
    if isinstance(inherit_labels, str):
        inherit_labels = [lbl.strip() for lbl in inherit_labels.split(",") if lbl.strip()]

    explicit_labels = data.get("explicit_labels") or []
    if isinstance(explicit_labels, str):
        explicit_labels = [lbl.strip() for lbl in explicit_labels.split(",") if lbl.strip()]

    # Parse milestone_strategy
    ms_data = data.get("milestone_strategy", {})
    milestone_strategy = MilestoneStrategyConfig(
        inherit_from_issues=ms_data.get("inherit_from_issues", "latest"),
        explicit=ms_data.get("explicit"),
    )

    return TriageConfig(
        inherit_labels=list(inherit_labels),
        explicit_labels=list(explicit_labels),
        milestone_strategy=milestone_strategy,
        priority=data.get("priority"),
    )


def _parse_filtering_config(data: dict) -> FilteringConfig:
    """Parse filtering section from YAML data."""
    # Parse milestones (list or comma-separated string)
    raw_milestones = data.get("milestones") or []
    if isinstance(raw_milestones, str):
        raw_milestones = [m.strip() for m in raw_milestones.split(",") if m.strip()]
    if not isinstance(raw_milestones, list):
        raise ValueError("filtering.milestones must be a list or comma-separated string")
    milestones = [str(m).strip() for m in raw_milestones if str(m).strip()]

    # Parse exclude_labels (list or comma-separated string)
    raw_exclude = data.get("exclude_labels") or []
    if isinstance(raw_exclude, str):
        raw_exclude = [lbl.strip() for lbl in raw_exclude.split(",") if lbl.strip()]
    if not isinstance(raw_exclude, list):
        raise ValueError("filtering.exclude_labels must be a list or comma-separated string")
    exclude_labels = [str(lbl).strip() for lbl in raw_exclude if str(lbl).strip()]

    return FilteringConfig(
        label=data.get("label"),
        milestone=data.get("milestone"),
        milestones=milestones,
        issue=data.get("issue"),
        exclude_labels=exclude_labels,
        fetch_limit=data.get("fetch_limit", 100),
        max_to_start=data.get("max_to_start", 0),
    )


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
    label_validation_failed: str = "validation-failed"  # Applied when validation fails
    label_prefix: Optional[str] = None  # Optional prefix for all labels (e.g., "bot")

    # Paths
    state_file: Path = Path(".issue-orchestrator/state.json")
    repo_root: Path = field(default_factory=Path.cwd)  # Root of the git repository
    repo_root_from_yaml: bool = False  # Internal: YAML explicitly set repo_root
    worktree_base: Path = Path(".issue-orchestrator/worktrees")  # Base directory for worktrees

    # Config validation
    config_strict: bool = False  # If True, unknown fields cause validation errors; if False, warnings only

    # GitHub settings
    repo: Optional[str] = None  # owner/repo, or None to auto-detect
    github_token: Optional[str] = None  # Explicit GitHub token (prefer env)
    github_token_env: Optional[str] = None  # Env var name for token (overrides defaults)
    github_api_url: str = "https://api.github.com"
    github_http_timeout_seconds: float = 20.0
    github_cache_ttl_seconds: int = 300  # Cache TTL for GitHub adapter responses
    github_required_scopes: list[str] = field(default_factory=list)
    github_allowed_scopes: list[str] = field(default_factory=list)

    # Issue filtering
    filtering: FilteringConfig = field(default_factory=FilteringConfig)

    # E2E test configuration
    e2e_pr_labels: list[str] = field(default_factory=list)  # Labels to apply to PRs created during e2e tests

    # Comment headings for structured worker comments
    comment_headings: CommentHeadings = field(default_factory=CommentHeadings)

    # Logging
    log_retention_days: int = 7  # Days to keep rotated log files

    # UI mode: "web" (default, browser dashboard) or "tmux" (terminal dashboard)
    ui_mode: str = "web"
    web_port: int = 8080  # Port for web dashboard
    control_api_port: int = 19080  # Port for control API (always available, 0 = disabled)
    queue_refresh_seconds: int = 600  # How often web UI refetches queue from GitHub (0 = manual only)
    session_no_output_seconds: int = 120  # Emit session_no_output after this many seconds idle
    session_no_output_tail_lines: int = 50  # Max tail lines to include in session_no_output
    session_no_output_max_bytes: int = 10000  # Max bytes of tail content
    session_no_output_repeat_seconds: int = 120  # Minimum gap between session_no_output events

    # Session detection - be lenient to avoid false terminations
    # These protect against session detection failures during startup
    session_grace_period_seconds: int = 120  # Don't terminate sessions younger than this
    session_log_activity_seconds: int = 120  # If log modified within this window, session is alive

    gh_write_verify_timeout_seconds: int = 20
    gh_write_verify_initial_delay_ms: int = 250
    gh_write_verify_max_delay_ms: int = 2000
    gh_write_verify_backoff: float = 1.5
    gh_write_verify_jitter_ms: int = 0
    gh_rate_limit_startup: bool = True  # Log GH rate limits at startup
    gh_rate_limit_every_calls: int = 500  # Check GH rate limits every N calls (0 = disabled)
    gh_rate_limit_warn_fraction: float = 0.1  # Warn when remaining below fraction of limit
    gh_rate_limit_warn_remaining: int = 100  # Warn when remaining below this count
    gh_audit_enabled: bool = False  # Enable GH audit reporting
    gh_audit_events: bool = False  # Emit GH audit events to event stream
    gh_audit_file: Optional[str] = None  # Path for GH audit report (supports {pid})

    # Terminal adapter (optional - overrides ui_mode if set)
    # Can be "builtin:tmux" or a full class path for custom adapters
    terminal_adapter: Optional[str] = None

    # Tmux session mode: "shared" (single orchestrator session) or "per_session"
    tmux_session_mode: str = "shared"

    # Custom tmuxp config file for tmux layout (optional)
    # If specified, loads this tmuxp config instead of the default
    # Supports mouse mode, double-click zoom, custom layouts
    tmuxp_config: Optional[Path] = None

    # Tmux key bindings to apply after session creation
    # These are raw tmux bind-key commands (without the "tmux" prefix)
    # Default enables double-click to zoom panes
    tmux_bindings: list[str] = field(default_factory=lambda: [
        "bind-key -T root DoubleClick1Pane resize-pane -Z -t =",
    ])

    # Milestone sorting strategy - built-in: "due_date", "number", "pattern", "name"
    # Or provide a custom class path like "mymodule.MyStrategy"
    milestone_sort: str = "due_date"
    # Config passed to strategy via **kwargs (e.g., pattern="M(\\d+)" for PatternStrategy)
    milestone_sort_config: dict = field(default_factory=dict)
    # Foundation milestone - dependencies must be same milestone OR in foundation
    foundation_milestone: str = "M0"

    # Cleanup configuration - when to close AI session tabs and remove worktrees
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)

    # Enforcement options
    enforce_hooks: bool = True  # Install pre-push hooks (runs project validation + orchestrator checks)
    pre_push_hook: Optional[Path] = None  # Custom pre-push hook path (uses bundled if None)

    # Worktree setup commands (run after worktree creation, e.g., npm install)
    setup_worktree: list[str] = field(default_factory=list)

    # Code review workflow (optional) - per-PR review after agent creates PR
    review_enabled: bool = False  # Explicit toggle for code review
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

    # Validation configuration - single command runs on agent-done and pre-push
    validation: ValidationConfig = field(default_factory=ValidationConfig)

    # Isolation configuration - how agents are sandboxed
    isolation: IsolationConfig = field(default_factory=IsolationConfig)

    # Triage issue configuration - label/milestone inheritance
    triage: TriageConfig = field(default_factory=TriageConfig)

    # E2E async test runner configuration
    e2e: E2EConfig = field(default_factory=E2EConfig)

    # Stale in-progress escalation threshold (0 = disabled)
    # If an issue has stale in-progress for K consecutive ticks, emit escalation event
    stale_escalation_ticks: int = 0

    # Path to the config file (set during load)
    config_path: Optional[Path] = None

    # Raw YAML data for unknown field validation (set during load)
    _raw_data: dict = field(default_factory=dict)
    _raw_agents: dict = field(default_factory=dict)

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

    def get_label_validation_failed(self) -> str:
        """Get the validation-failed label with prefix if configured."""
        return self.prefixed_label(self.label_validation_failed)

    def is_validation_enabled(self) -> bool:
        """Check if validation is enabled (cmd is set)."""
        return self.validation.cmd is not None

    def get_filter_milestones(self) -> list[str]:
        """Return a list of milestone filters."""
        return self.filtering.get_milestones()

    def get_issue_filter(self) -> "IssueLabelFilter":
        """Get the issue label filter configured for this config.

        Returns an IssueLabelFilter instance that can filter issues based on
        exclude_labels configuration.
        """
        from ..domain.issue_filter import IssueLabelFilter
        return IssueLabelFilter.from_config(exclude_labels=self.filtering.exclude_labels)

    def get_reviewer_for_agent(self, agent_label: str) -> Optional[str]:
        """Get the effective reviewer for an agent.

        Returns the per-agent reviewer if set, otherwise the default reviewer
        (code_review_agent). Returns None if no reviewer is configured.
        """
        if agent_label not in self.agents:
            return self.code_review_agent
        agent = self.agents[agent_label]
        # Per-agent reviewer takes precedence over default
        return agent.reviewer or self.code_review_agent

    def to_event_dict(self) -> dict:
        """Convert config to a dict for event emission.

        Returns a serializable dict with the merged configuration
        (YAML + command line overrides) for debugging.
        """
        return {
            "repo": self.repo,
            "github_token_env": self.github_token_env,
            "github_api_url": self.github_api_url,
            "github_http_timeout_seconds": self.github_http_timeout_seconds,
            "github_cache_ttl_seconds": self.github_cache_ttl_seconds,
            "github_required_scopes": list(self.github_required_scopes),
            "github_allowed_scopes": list(self.github_allowed_scopes),
            "repo_root": str(self.repo_root),
            "config_path": str(self.config_path) if self.config_path else None,
            "filtering": {
                "label": self.filtering.label,
                "milestone": self.filtering.milestone,
                "milestones": list(self.filtering.milestones),
                "issue": self.filtering.issue,
                "exclude_labels": list(self.filtering.exclude_labels),
                "fetch_limit": self.filtering.fetch_limit,
                "max_to_start": self.filtering.max_to_start,
            },
            "foundation_milestone": self.foundation_milestone,
            "e2e_pr_labels": self.e2e_pr_labels,
            "max_concurrent_sessions": self.max_concurrent_sessions,
            "session_timeout_minutes": self.session_timeout_minutes,
            "queue_refresh_seconds": self.queue_refresh_seconds,
            "session_no_output_seconds": self.session_no_output_seconds,
            "session_no_output_tail_lines": self.session_no_output_tail_lines,
            "session_no_output_max_bytes": self.session_no_output_max_bytes,
            "session_no_output_repeat_seconds": self.session_no_output_repeat_seconds,
            "gh_write_verify_timeout_seconds": self.gh_write_verify_timeout_seconds,
            "gh_write_verify_initial_delay_ms": self.gh_write_verify_initial_delay_ms,
            "gh_write_verify_max_delay_ms": self.gh_write_verify_max_delay_ms,
            "gh_write_verify_backoff": self.gh_write_verify_backoff,
            "gh_write_verify_jitter_ms": self.gh_write_verify_jitter_ms,
            "gh_rate_limit_startup": self.gh_rate_limit_startup,
            "gh_rate_limit_every_calls": self.gh_rate_limit_every_calls,
            "gh_rate_limit_warn_fraction": self.gh_rate_limit_warn_fraction,
            "gh_rate_limit_warn_remaining": self.gh_rate_limit_warn_remaining,
            "gh_audit_enabled": self.gh_audit_enabled,
            "gh_audit_events": self.gh_audit_events,
            "gh_audit_file": self.gh_audit_file,
            "ui_mode": self.ui_mode,
            "terminal_adapter": self.terminal_adapter,
            "tmux_session_mode": self.tmux_session_mode,
            "tmuxp_config": str(self.tmuxp_config) if self.tmuxp_config else None,
            "tmux_bindings": self.tmux_bindings,
            "agents": {
                label: {
                    "prompt_path": str(cfg.prompt_path),
                    "model": cfg.model,
                    "timeout_minutes": cfg.timeout_minutes,
                    "meta_agent": cfg.meta_agent,
                }
                for label, cfg in self.agents.items()
            },
            "worktree_base": str(self.worktree_base),
            "labels": {
                "in_progress": self.get_label_in_progress(),
                "blocked": self.get_label_blocked(),
                "needs_human": self.get_label_needs_human(),
                "needs_rework": self.get_label_needs_rework(),
                "validation_failed": self.get_label_validation_failed(),
                "prefix": self.label_prefix,
            },
            "validation": {
                "enabled": self.is_validation_enabled(),
                "cmd": self.validation.cmd,
                "timeout_seconds": self.validation.timeout_seconds,
            },
            "code_review": {
                "agent": self.code_review_agent,
                "label": self.code_review_label,
                "reviewed_label": self.code_reviewed_label,
            },
            "triage_review": {
                "agent": self.triage_review_agent,
                "threshold": self.triage_review_threshold,
                "on_failure": self.triage_review_on_failure,
            },
            "cleanup": {
                "with_triage": {
                    "close_ai_session_tabs": self.cleanup.with_triage.close_ai_session_tabs,
                    "remove_worktrees": self.cleanup.with_triage.remove_worktrees,
                },
                "without_triage": {
                    "wait_for_code_review": self.cleanup.without_triage.wait_for_code_review,
                    "close_ai_session_tabs": self.cleanup.without_triage.close_ai_session_tabs,
                    "remove_worktrees": self.cleanup.without_triage.remove_worktrees,
                },
            },
            "dangerous": {
                "allow_unsupported_agents": self.dangerous.allow_unsupported_agents,
            },
            "stale_escalation_ticks": self.stale_escalation_ticks,
            "triage": {
                "inherit_labels": list(self.triage.inherit_labels),
                "explicit_labels": list(self.triage.explicit_labels),
                "milestone_strategy": {
                    "inherit_from_issues": self.triage.milestone_strategy.inherit_from_issues,
                    "explicit": self.triage.milestone_strategy.explicit,
                },
                "priority": self.triage.priority,
            },
        }

    @classmethod
    def load(cls, config_path: Path, overrides: Optional[list[str]] = None) -> "Config":
        """Load configuration from YAML file."""
        if not config_path.exists():
            raise FileNotFoundError(f"Config file not found: {config_path}")

        with open(config_path) as f:
            data = yaml.safe_load(f)
        if data is None:
            data = {}
        _apply_yaml_overrides(data, overrides or [])

        config = cls()
        config.config_path = config_path.resolve()

        # Determine repo root using centralized helper
        repo_root = repo_root_from_config_path(config_path)

        # Default repo_root to detected repo root
        # (can be overridden by YAML settings)
        config.repo_root = repo_root
        if data.get("repo_root"):
            config.repo_root = resolve_relative_path(data["repo_root"], repo_root)
            config.repo_root_from_yaml = True

        # Parse top-level worktree_base (applies to all agents)
        # Default: parent directory of repo (worktrees become siblings like {repo}-33)
        worktree_base_raw = data.get("worktree_base")
        if worktree_base_raw is None:
            config.worktree_base = repo_root.parent
        else:
            config.worktree_base = resolve_relative_path(worktree_base_raw, repo_root)

        # Validate worktree_base is usable (create if needed, fail fast if not)
        try:
            config.worktree_base.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            raise ValueError(
                f"worktree_base '{config.worktree_base}' cannot be created: {e}. "
                f"Specify an absolute path in your config under worktree_base"
            )

        # Parse config section (strict mode, etc.)
        config_section = data.get("config", {})
        config.config_strict = config_section.get("strict", False)

        # Store raw data for unknown field validation
        config._raw_data = data
        config._raw_agents = data.get("agents", {})

        # Parse agents
        # All relative paths in agent configs are resolved relative to repo_root

        for label, agent_data in data.get("agents", {}).items():
            # Resolve prompt path relative to repo_root (for validation)
            # Keep original relative path for command templates (agents read from worktree)
            prompt_relative = agent_data["prompt"]
            prompt_path = resolve_relative_path(prompt_relative, repo_root)

            agent_kwargs = {
                "prompt_path": prompt_path,
                "prompt_relative": prompt_relative,
                "model": agent_data.get("model", "sonnet"),
                "timeout_minutes": agent_data.get("timeout_minutes", 45),
                "permission_mode": agent_data.get("permission_mode", "default"),
                "skip_review": agent_data.get("skip_review", False),
                "reviewer": agent_data.get("reviewer"),  # Per-agent reviewer override
                "meta_agent": agent_data.get("meta_agent"),
                "ai_system": agent_data.get("ai_system"),  # Explicit AI system override
            }
            if "command" in agent_data:
                agent_kwargs["command"] = agent_data["command"]
            if "initial_prompt" in agent_data:
                agent_kwargs["initial_prompt"] = agent_data["initial_prompt"]
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
        config.label_validation_failed = labels.get("validation_failed", "validation-failed")
        config.label_prefix = labels.get("prefix")

        # GitHub settings
        config.repo = data.get("repo")
        config.github_token = data.get("github_token")
        config.github_token_env = data.get("github_token_env")
        config.github_api_url = data.get("github_api_url", "https://api.github.com")
        config.github_http_timeout_seconds = data.get("github_http_timeout_seconds", 20.0)
        config.github_cache_ttl_seconds = data.get("github_cache_ttl_seconds", 300)
        required_scopes = data.get("github_required_scopes", []) or []
        allowed_scopes = data.get("github_allowed_scopes", []) or []
        if isinstance(required_scopes, str):
            required_scopes = [s.strip() for s in required_scopes.split(",") if s.strip()]
        if isinstance(allowed_scopes, str):
            allowed_scopes = [s.strip() for s in allowed_scopes.split(",") if s.strip()]
        config.github_required_scopes = list(required_scopes)
        config.github_allowed_scopes = list(allowed_scopes)
        config.e2e_pr_labels = data.get("e2e_pr_labels", [])

        # Parse filtering section
        filtering_data = data.get("filtering", {})
        config.filtering = _parse_filtering_config(filtering_data)

        # UI mode
        config.ui_mode = data.get("ui_mode", "web")
        config.web_port = data.get("web_port", 8080)
        config.control_api_port = data.get("control_api_port", 19080)
        config.queue_refresh_seconds = data.get("queue_refresh_seconds", 600)
        config.session_no_output_seconds = data.get("session_no_output_seconds", 120)
        config.session_no_output_tail_lines = data.get("session_no_output_tail_lines", 50)
        config.session_no_output_max_bytes = data.get("session_no_output_max_bytes", 10000)
        config.session_no_output_repeat_seconds = data.get("session_no_output_repeat_seconds", 120)
        config.gh_write_verify_timeout_seconds = data.get("gh_write_verify_timeout_seconds", 20)
        config.gh_write_verify_initial_delay_ms = data.get("gh_write_verify_initial_delay_ms", 250)
        config.gh_write_verify_max_delay_ms = data.get("gh_write_verify_max_delay_ms", 2000)
        config.gh_write_verify_backoff = data.get("gh_write_verify_backoff", 1.5)
        config.gh_write_verify_jitter_ms = data.get("gh_write_verify_jitter_ms", 0)
        config.gh_rate_limit_startup = data.get("gh_rate_limit_startup", True)
        config.gh_rate_limit_every_calls = data.get("gh_rate_limit_every_calls", 500)
        config.gh_rate_limit_warn_fraction = data.get("gh_rate_limit_warn_fraction", 0.1)
        config.gh_rate_limit_warn_remaining = data.get("gh_rate_limit_warn_remaining", 100)
        config.gh_audit_enabled = data.get("gh_audit_enabled", False)
        config.gh_audit_events = data.get("gh_audit_events", False)
        config.gh_audit_file = data.get("gh_audit_file")

        # Terminal adapter (overrides ui_mode if set)
        config.terminal_adapter = data.get("terminal_adapter")
        if data.get("tmux_session_mode"):
            config.tmux_session_mode = str(data["tmux_session_mode"])

        # Custom tmuxp config for tmux layout
        if data.get("tmuxp"):
            config.tmuxp_config = resolve_relative_path(data["tmuxp"], repo_root)

        # Tmux key bindings (default: double-click to zoom)
        if "tmux_bindings" in data:
            bindings = data["tmux_bindings"]
            if bindings is None:
                config.tmux_bindings = []  # Explicitly disable bindings
            elif isinstance(bindings, list):
                config.tmux_bindings = bindings
            else:
                config.tmux_bindings = [str(bindings)]

        # Milestone sorting strategy
        config.milestone_sort = data.get("milestone_sort", "due_date")
        config.milestone_sort_config = data.get("milestone_sort_config", {})
        config.foundation_milestone = data.get("foundation_milestone", "M0")

        # Enforcement options
        config.enforce_hooks = data.get("enforce_hooks", True)
        if data.get("pre_push_hook"):
            config.pre_push_hook = resolve_relative_path(data["pre_push_hook"], repo_root)

        # Worktree setup commands
        config.setup_worktree = data.get("setup_worktree", [])

        # Review workflow
        review_config = data.get("review", {})

        # Code review (per-PR, immediate)
        # "enabled" explicitly toggles code review on/off
        config.review_enabled = review_config.get("enabled", False)
        # "default" is the preferred key for the default reviewer, "code_review_agent" is legacy
        config.code_review_agent = (
            review_config.get("default") or
            review_config.get("code_review_agent")
        )
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
                allow_unsupported_agents=dangerous_data.get("allow_unsupported_agents", False),
            )

        # Parse validation config - single command runs everywhere
        validation_data = data.get("validation", {})
        if validation_data:
            config.validation = ValidationConfig(
                cmd=validation_data.get("cmd"),
                timeout_seconds=validation_data.get("timeout_seconds", 300),
            )

        # Parse isolation config
        isolation_data = data.get("isolation", {})
        if isolation_data:
            config.isolation = IsolationConfig(
                mode=isolation_data.get("mode", "standard"),
            )

        # Stale in-progress escalation threshold (0 = disabled)
        config.stale_escalation_ticks = data.get("stale_escalation_ticks", 0)

        # Parse triage config
        triage_data = data.get("triage", {})
        if triage_data:
            config.triage = _parse_triage_config(triage_data)

        # Parse e2e config
        e2e_data = data.get("e2e", {})
        if e2e_data:
            config.e2e = _parse_e2e_config(e2e_data)

        return config

    @classmethod
    def find_and_load(
        cls,
        start_path: Optional[Path] = None,
        config_name: str = DEFAULT_CONFIG_NAME,
        overrides: Optional[list[str]] = None,
    ) -> "Config":
        """Find config file in current or parent directories and load it.

        Args:
            start_path: Starting path for search (defaults to cwd)
            config_name: Name of config file to load (default: default.yaml)
            overrides: CLI overrides in path=value format
        """
        config_file = find_config_file(start_path, config_name)
        if not config_file:
            raise FileNotFoundError(
                f"No config found in {CONFIG_DIR}/ directory. "
                "Run the setup wizard to create a configuration."
            )

        config = cls.load(config_file, overrides=overrides)
        # Set repo_root using centralized helper if not already set from YAML
        if not config.repo_root_from_yaml:
            config.repo_root = repo_root_from_config_path(config_file)
        return config

    def validate(self) -> list[str]:
        """Validate configuration and return list of errors.

        Call this at startup to catch configuration problems early.
        Returns empty list if valid, list of error messages otherwise.
        """
        errors = []

        # Validate agents
        if not self.agents:
            errors.append("No agents configured. Add at least one agent under 'agents:' in config.")

        # Validate worktree_base (now top-level)
        if not self.worktree_base.is_absolute():
            errors.append(
                f"worktree_base must be absolute path, got: {self.worktree_base}"
            )
        elif not self.worktree_base.exists():
            errors.append(
                f"worktree_base does not exist: {self.worktree_base}"
            )
        elif not self.worktree_base.is_dir():
            errors.append(
                f"worktree_base is not a directory: {self.worktree_base}"
            )

        for label, agent in self.agents.items():
            # Validate prompt file exists
            if not agent.prompt_path.exists():
                errors.append(
                    f"Agent '{label}': prompt file not found: {agent.prompt_path}"
                )

            # Validate model is known
            known_models = {"haiku", "sonnet", "opus"}
            if agent.model not in known_models:
                errors.append(
                    f"Agent '{label}': unknown model '{agent.model}'. Known: {known_models}"
                )

        # Validate review workflow
        if self.review_enabled:
            # If reviews are enabled, default reviewer is required
            if not self.code_review_agent:
                errors.append(
                    "review.enabled is true but no default reviewer set. "
                    "Add 'review: default: agent:reviewer' to config."
                )
            elif self.code_review_agent not in self.agents:
                errors.append(
                    f"review.default '{self.code_review_agent}' not found in agents. "
                    f"Available: {list(self.agents.keys())}"
                )

        if self.triage_review_agent and self.triage_review_agent not in self.agents:
            errors.append(
                f"triage_review_agent '{self.triage_review_agent}' not found in agents. "
                f"Available: {list(self.agents.keys())}"
            )

        # Validate per-agent reviewers reference valid agents
        for label, agent in self.agents.items():
            if agent.reviewer and agent.reviewer not in self.agents:
                errors.append(
                    f"Agent '{label}': reviewer '{agent.reviewer}' not found in agents. "
                    f"Available: {list(self.agents.keys())}"
                )

        # Validate isolation mode
        valid_isolation_modes = {"standard", "hardened"}
        if self.isolation.mode not in valid_isolation_modes:
            errors.append(
                f"isolation.mode must be one of {valid_isolation_modes}, got: '{self.isolation.mode}'"
            )

        # Validate unknown fields (errors or warnings depending on config_strict)
        unknown_fields = self.validate_unknown_fields()
        if unknown_fields:
            import logging
            logger = logging.getLogger(__name__)
            for field_path, _ in unknown_fields:
                msg = f"Unknown config field: '{field_path}'"
                if self.config_strict:
                    errors.append(msg)
                else:
                    logger.warning(msg)

        # Validate template variables in initial_prompt and command
        invalid_templates = self.validate_template_variables()
        for agent_label, field_name, bad_vars in invalid_templates:
            vars_str = ", ".join(sorted(bad_vars))
            errors.append(
                f"Agent '{agent_label}': invalid template variable(s) in {field_name}: {{{vars_str}}}. "
                f"Valid: issue_number, issue_title, prompt, worktree, model, permission_mode, "
                f"claude_args, pr_number" + (", initial_prompt" if field_name == "command" else "")
            )

        return errors

    def validate_unknown_fields(self) -> list[tuple[str, str]]:
        """Check for unknown fields in the raw YAML data.

        Returns list of (field_path, level) tuples where:
        - field_path is like "repo_root" or "agents.agent:web.worktree_base"
        - level is "top" or "agent"
        """
        unknown = []

        # Check top-level fields
        for key in self._raw_data.keys():
            if key not in ALLOWED_TOP_LEVEL_FIELDS:
                unknown.append((key, "top"))

        # Check per-agent fields
        for agent_name, agent_data in self._raw_agents.items():
            if isinstance(agent_data, dict):
                for key in agent_data.keys():
                    if key not in ALLOWED_AGENT_FIELDS:
                        unknown.append((f"agents.{agent_name}.{key}", "agent"))

        return unknown

    def validate_template_variables(self) -> list[tuple[str, str, set[str]]]:
        """Check for invalid template variables in initial_prompt and command.

        Returns list of (agent_label, field_name, invalid_vars) tuples.
        """
        import re

        # Valid variables for initial_prompt (before command rendering)
        VALID_INITIAL_PROMPT_VARS = {
            "issue_number",
            "issue_title",
            "prompt",
            "worktree",
            "model",
            "permission_mode",
            "claude_args",
            "pr_number",  # Only valid for review agents, but we allow it here
        }

        # Valid variables for command (after initial_prompt is rendered)
        VALID_COMMAND_VARS = VALID_INITIAL_PROMPT_VARS | {"initial_prompt"}

        # Regex to find {variable_name} patterns (excluding {{ escaped braces }})
        VAR_PATTERN = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")

        invalid = []

        for label, agent in self.agents.items():
            # Check initial_prompt
            found_vars = set(VAR_PATTERN.findall(agent.initial_prompt))
            bad_vars = found_vars - VALID_INITIAL_PROMPT_VARS
            if bad_vars:
                invalid.append((label, "initial_prompt", bad_vars))

            # Check command
            found_vars = set(VAR_PATTERN.findall(agent.command))
            bad_vars = found_vars - VALID_COMMAND_VARS
            if bad_vars:
                invalid.append((label, "command", bad_vars))

        return invalid

    def validate_or_raise(self) -> None:
        """Validate configuration, raising ValueError if invalid."""
        errors = self.validate()
        if errors:
            raise ValueError(
                "Configuration errors:\n  - " + "\n  - ".join(errors)
            )


def find_config_file(
    start_path: Optional[Path] = None,
    config_name: str = DEFAULT_CONFIG_NAME,
) -> Optional[Path]:
    """Find the config file by searching up the directory tree.

    This is the single source of truth for config file lookup.
    Only looks in .issue-orchestrator/config/ directory.

    Args:
        start_path: Starting path for search (defaults to cwd)
        config_name: Name of config file to find (default: default.yaml)

    Returns:
        Path to config file, or None if not found
    """
    search_path = start_path or Path.cwd()

    for path in [search_path, *search_path.parents]:
        config_file = get_config_path(path, config_name)
        if config_file.exists():
            return config_file

    return None


def _apply_yaml_overrides(data: dict, overrides: list[str]) -> None:
    """Apply CLI overrides (path=value) to YAML data in-place."""
    for override in overrides:
        if "=" not in override:
            raise ValueError(f"Invalid override (expected key=value): {override}")
        path, raw_value = override.split("=", 1)
        if not path:
            raise ValueError(f"Invalid override (empty key): {override}")
        try:
            value = yaml.safe_load(raw_value)
        except Exception:
            value = raw_value
        cursor = data
        keys = path.split(".")
        for key in keys[:-1]:
            if key not in cursor or not isinstance(cursor[key], dict):
                cursor[key] = {}
            cursor = cursor[key]
        cursor[keys[-1]] = value


def load_validation_config(
    start_path: Optional[Path] = None,
) -> dict:
    """Load validation configuration from the config file.

    This is a lightweight function for use by validation hooks (agent_done,
    prepush_check) that need only the validation config, not the full Config.

    Args:
        start_path: Starting path for config search (defaults to cwd)

    Returns:
        Dict with validation config:
        {
            "cmd": "make validate",  # or None if not configured
            "timeout_seconds": 300,
        }
    """
    config_path = find_config_file(start_path)
    if not config_path:
        return {"cmd": None, "timeout_seconds": 300}

    try:
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}

        validation = config.get("validation", {})
        return {
            "cmd": validation.get("cmd"),
            "timeout_seconds": validation.get("timeout_seconds", 300),
        }
    except Exception:
        return {"cmd": None, "timeout_seconds": 300}
