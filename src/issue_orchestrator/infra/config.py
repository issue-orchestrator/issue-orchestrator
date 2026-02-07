"""Configuration loading and management."""

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from ..domain.issue_filter import IssueLabelFilter

from ..domain.models import AgentConfig, CommentHeadings

# Config directory structure
CONFIG_DIR = ".issue-orchestrator/config"
DEFAULT_CONFIG_NAME = "default.yaml"

# Valid top-level config fields — derived from _TOP_LEVEL_SECTION_KEYS (further down)
# plus "repo" and "default_agent" which are parsed separately.
# Assigned after _TOP_LEVEL_SECTION_KEYS is defined to avoid maintaining two lists.
ALLOWED_TOP_LEVEL_FIELDS: frozenset[str]  # set below _TOP_LEVEL_SECTION_KEYS

# Valid per-agent config fields (worktree_base and repo_root removed - now top-level only)
ALLOWED_AGENT_FIELDS = {
    'prompt', 'provider', 'model', 'timeout_minutes',
    'permission_mode', 'skip_review', 'reviewer', 'command',
    'meta_agent', 'initial_prompt', 'ai_system', 'provider_args', 'retry_prompt_template',
}

# Pattern for ${VAR} environment variable references
_ENV_VAR_PATTERN = re.compile(r"\$\{([^}]+)\}")


class ConfigEnvVarError(Exception):
    """Raised when an environment variable referenced in config is not set."""

    pass


def _expand_env_vars(value: Any, path: str = "") -> Any:
    """Recursively expand ${VAR} environment variable references in config values.

    Args:
        value: The config value to expand (can be dict, list, or scalar).
        path: Dot-separated path to current location (for error messages).

    Returns:
        The value with all ${VAR} references expanded.

    Raises:
        ConfigEnvVarError: If a referenced environment variable is not set.
    """
    if isinstance(value, dict):
        return {
            k: _expand_env_vars(v, f"{path}.{k}" if path else k)
            for k, v in value.items()
        }
    elif isinstance(value, list):
        return [
            _expand_env_vars(item, f"{path}[{i}]")
            for i, item in enumerate(value)
        ]
    elif isinstance(value, str):
        # Find all ${VAR} references and expand them
        def replace_env_var(match: re.Match[str]) -> str:
            var_name = match.group(1)
            env_value = os.environ.get(var_name)
            if env_value is None:
                location = f" (in {path})" if path else ""
                raise ConfigEnvVarError(
                    f"Environment variable '{var_name}' is not set{location}"
                )
            return env_value

        return _ENV_VAR_PATTERN.sub(replace_env_var, value)
    else:
        # Numbers, booleans, None - return as-is
        return value


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


class ConfigSectionError(ValueError):
    """Raised when a config section has an invalid type."""

    pass


def _get_section(data: dict, key: str, config_path: Path) -> dict:
    """Get a config section, validating it's a dict.

    YAML quirk: `section:` with only comments or nothing becomes None.
    This helper provides clear error messages for this common mistake.

    Args:
        data: The parsed YAML data
        key: The section key to retrieve
        config_path: Path to config file (for error messages)

    Returns:
        The section as a dict, or empty dict if not present

    Raises:
        ConfigSectionError: If section exists but isn't a dict
    """
    value = data.get(key)
    if value is None:
        return {}
    if isinstance(value, dict):
        return value

    # Provide helpful error message based on what we got
    type_name = type(value).__name__
    if isinstance(value, str):
        hint = (
            f"  Got string: '{value}'\n"
            f"  Expected a mapping like:\n"
            f"    {key}:\n"
            f"      some_option: value"
        )
    elif isinstance(value, (list, tuple)):
        hint = (
            f"  Got a list, but '{key}' should be a mapping.\n"
            f"  Expected:\n"
            f"    {key}:\n"
            f"      some_option: value"
        )
    else:
        hint = f"  Got {type_name}: {value!r}"

    raise ConfigSectionError(
        f"Invalid config section '{key}' in {config_path}\n"
        f"{hint}\n\n"
        f"If you meant to leave '{key}' empty, either:\n"
        f"  - Remove the '{key}:' line entirely, or\n"
        f"  - Use '{key}: {{}}' for an explicit empty mapping"
    )


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
class AiGateConfig:
    """AI gate configuration for periodic hook enforcement testing.

    Exercises AI-level hooks/execpolicy to verify blocking works.
    """
    interval_days: int = 7  # Run AI gate test every N days (0 = disabled)
    dangerous_allow_failure: bool = False  # If True, warn only; if False, block on failure


@dataclass
class HooksConfig:
    """Hook management configuration."""
    ai_gate: AiGateConfig = field(default_factory=AiGateConfig)


@dataclass
class CoverageGuardrailConfig:
    """Per-file coverage guardrail for files touched in a change."""
    enabled: bool = False
    min_percent: Optional[float] = None
    apply_to: str = "changed"  # "changed" or "all"
    scope: list[str] = field(default_factory=list)
    coverage_type: str = "line"  # "line" or "branch"
    exclude: list[str] = field(default_factory=list)


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
    pre_push_dirty_check: str = "tracked"  # "tracked" | "unstaged" | "off"
    coverage_guardrail: CoverageGuardrailConfig = field(default_factory=CoverageGuardrailConfig)


@dataclass
class RetryConfig:
    """Validation retry configuration.

    When an agent completes but validation fails, the orchestrator can
    retry with error context injected into the prompt.
    """
    max_validation_retries: int = 3  # Max times to retry after validation failure
    validation_error_file: str = "validation-errors.txt"  # Filename in session output dir
    # Default retry prompt template path (relative to repo root).
    # Agents can override this with their own retry_prompt_template.
    # If None, uses built-in default template.
    retry_prompt_template: Optional[str] = None


@dataclass
class ProviderShortRetryConfig:
    """Short retry settings for provider resilience."""

    max_attempts: int = 4
    initial_backoff_seconds: int = 5
    max_backoff_seconds: int = 60
    jitter: bool = True


@dataclass
class ProviderCircuitBreakerConfig:
    """Circuit breaker settings for provider resilience."""

    cooldown_seconds: int = 1800
    max_cooldowns: int = 6
    label: str = "blocked:provider-unavailable"


@dataclass
class ProviderResilienceConfig:
    """Provider resilience configuration."""

    short_retry: ProviderShortRetryConfig = field(default_factory=ProviderShortRetryConfig)
    circuit_breaker: ProviderCircuitBreakerConfig = field(default_factory=ProviderCircuitBreakerConfig)


@dataclass
class DefaultAgentConfig:
    """Default agent configuration inherited by all agents.

    When agents don't specify their own provider/model/args, they inherit
    from default_agent. If an agent has no provider and no default is set,
    config validation fails fast.

    Example YAML:
        default_agent:
          provider: claude-code
          model: sonnet
          provider_args:
            permission_mode: bypassPermissions
    """
    provider: Optional[str] = None  # "claude-code", "codex", etc.
    model: Optional[str] = None  # Model to use (sonnet, o3, etc.)
    provider_args: dict = field(default_factory=dict)  # Provider-specific args


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
class SchedulingConfig:
    """Scheduling configuration."""
    default_priority_tier: int = 1  # P1 (medium) when no [P?-nnn] prefix


@dataclass
class E2EConfig:
    """E2E async test runner settings.

    Controls local async E2E test execution with results persisted to SQLite.

    Role determines whether this orchestrator runs E2E tests:
    - "auto": Determine role automatically (default)
    - "executor": Run tests and publish results
    - "reader": Only display results (don't run tests)
    - "disabled": E2E functionality completely off

    In "auto" mode:
    - orchestrator-1 (or single instance) is executor
    - Other instances become readers

    For multi-machine setups, use explicit role with env var:
        role: ${E2E_ROLE}  # Set E2E_ROLE=executor on designated machine
    """
    enabled: bool = False  # Whether E2E runner is active
    role: str = "auto"  # auto | executor | reader | disabled
    auto_run_interval_minutes: int = 30  # Min interval between auto runs (0 = disable auto)
    pytest_args: list[str] = field(default_factory=lambda: ["tests/e2e", "-v"])
    allow_retry_once: bool = True  # Retry failing tests once to reduce flakiness
    quarantine_file: str = "tests/e2e/quarantine.txt"  # Path to quarantine list
    survive_restart: bool = True  # Let worker finish if orchestrator restarts
    stop_on_first_failure: bool = False  # If True, stop on first test failure (-x flag)
    auto_quarantine: bool = True  # Auto-add failing tests to quarantine list
    auto_create_issues: bool = True  # Auto-create GitHub issues for failures
    issue_agent_label: str = "agent:backend"  # Agent label for failure issues
    flake_threshold: int = 20  # Flip rate percentage (0-100) to flag test as flaky
    flake_window_runs: int = 10  # Number of recent runs to check for flakiness


@dataclass
class SqliteBackupConfig:
    """SQLite backup configuration."""

    enabled: bool = True
    cadence_hours: int = 24  # Minimum hours between backups
    check_interval_minutes: int = 60  # How often to check for due backups
    retention_daily: int = 14  # Number of daily backups to keep
    retention_weekly: int = 8  # Number of weekly backups to keep
    enforce_on_startup: bool = True  # Force backup on startup if cadence elapsed


@dataclass
class TimelineConfig:
    """Timeline retention configuration."""

    max_records: int = 5000


@dataclass
class ClaimsConfig:
    """Claims/lease configuration for multi-orchestrator coordination.

    When enabled, orchestrators coordinate via GitHub issue comments to ensure
    only one orchestrator works on each issue at a time. Uses a convergence
    protocol with tie-breaking for distributed consensus.

    Single-orchestrator deployments should leave this disabled (the default).
    """
    enabled: bool = False  # Whether claims system is active
    claimant_id: Optional[str] = None  # Unique ID for this orchestrator instance

    # Lease timing (seconds)
    lease_seconds: int = 900  # 15 min default lease duration
    renew_before_expiry_seconds: int = 300  # Renew when 5 min remaining

    # Convergence protocol settings
    convergence_timeout_seconds: float = 5.0  # Max time to wait for convergence
    convergence_poll_min_ms: int = 250  # Min poll interval (randomized)
    convergence_poll_max_ms: int = 500  # Max poll interval (randomized)
    convergence_required_wins: int = 2  # Consecutive wins needed


@dataclass
class GoalPilotConfig:
    """Configuration for Goal Pilot AI."""

    enabled: bool = False
    agent: Optional[str] = None
    approval_policy: str = "journeys_only"  # journeys_only | gatekeeper | batch
    approval_batch_size: int = 10
    approval_batch_window_minutes: int = 60


def _parse_e2e_config(data: dict) -> E2EConfig:
    """Parse e2e section from YAML data."""
    pytest_args = data.get("pytest_args") or ["tests/e2e", "-v"]
    if isinstance(pytest_args, str):
        # Support space-separated string
        pytest_args = pytest_args.split()

    # Validate role
    role = data.get("role", "auto")
    if role not in ("auto", "executor", "reader", "disabled"):
        role = "auto"

    return E2EConfig(
        enabled=data.get("enabled", False),
        role=role,
        auto_run_interval_minutes=data.get("auto_run_interval_minutes", 30),
        pytest_args=list(pytest_args),
        allow_retry_once=data.get("allow_retry_once", True),
        quarantine_file=data.get("quarantine_file", "tests/e2e/quarantine.txt"),
        survive_restart=data.get("survive_restart", True),
        stop_on_first_failure=data.get("stop_on_first_failure", False),
        auto_quarantine=data.get("auto_quarantine", True),
        auto_create_issues=data.get("auto_create_issues", True),
        issue_agent_label=data.get("issue_agent_label", "agent:backend"),
        flake_threshold=data.get("flake_threshold", 20),
        flake_window_runs=data.get("flake_window_runs", 10),
    )


def _parse_sqlite_backup_config(data: dict) -> SqliteBackupConfig:
    """Parse sqlite_backup section from YAML data."""
    return SqliteBackupConfig(
        enabled=data.get("enabled", True),
        cadence_hours=data.get("cadence_hours", 24),
        check_interval_minutes=data.get("check_interval_minutes", 60),
        retention_daily=data.get("retention_daily", 14),
        retention_weekly=data.get("retention_weekly", 8),
        enforce_on_startup=data.get("enforce_on_startup", True),
    )


def _parse_timeline_config(data: dict) -> TimelineConfig:
    """Parse timeline section from YAML data."""
    return TimelineConfig(
        max_records=data.get("max_records", 5000),
    )


def _parse_claims_config(data: dict) -> ClaimsConfig:
    """Parse claims section from YAML data."""
    return ClaimsConfig(
        enabled=data.get("enabled", False),
        claimant_id=data.get("claimant_id"),
        lease_seconds=data.get("lease_seconds", 900),
        renew_before_expiry_seconds=data.get("renew_before_expiry_seconds", 300),
        convergence_timeout_seconds=data.get("convergence_timeout_seconds", 5.0),
        convergence_poll_min_ms=data.get("convergence_poll_min_ms", 250),
        convergence_poll_max_ms=data.get("convergence_poll_max_ms", 500),
        convergence_required_wins=data.get("convergence_required_wins", 2),
    )


def _parse_goal_pilot_config(data: dict) -> GoalPilotConfig:
    """Parse goal_pilot section from YAML data."""
    return GoalPilotConfig(
        enabled=data.get("enabled", False),
        agent=data.get("agent"),
        approval_policy=data.get("approval_policy", "journeys_only"),
        approval_batch_size=data.get("approval_batch_size", 10),
        approval_batch_window_minutes=data.get("approval_batch_window_minutes", 60),
    )


def _parse_hooks_config(data: dict) -> HooksConfig:
    """Parse hooks section from YAML data."""
    ai_gate_data = data.get("ai_gate", {})
    ai_gate = AiGateConfig(
        interval_days=ai_gate_data.get("interval_days", 7),
        dangerous_allow_failure=ai_gate_data.get("dangerous_allow_failure", False),
    )
    return HooksConfig(ai_gate=ai_gate)


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


def _parse_scheduling_config(data: dict) -> SchedulingConfig:
    """Parse scheduling section from YAML data."""
    return SchedulingConfig(
        default_priority_tier=int(data.get("default_priority_tier", 1)),
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


def _parse_provider_resilience_config(data: dict) -> ProviderResilienceConfig:
    """Parse provider resilience section from YAML data."""
    short_data = data.get("short_retry", {}) or {}
    circuit_data = data.get("circuit_breaker", {}) or {}

    short_retry = ProviderShortRetryConfig(
        max_attempts=int(short_data.get("max_attempts", 4)),
        initial_backoff_seconds=int(short_data.get("initial_backoff_seconds", 5)),
        max_backoff_seconds=int(short_data.get("max_backoff_seconds", 60)),
        jitter=bool(short_data.get("jitter", True)),
    )
    circuit_breaker = ProviderCircuitBreakerConfig(
        cooldown_seconds=int(circuit_data.get("cooldown_seconds", 1800)),
        max_cooldowns=int(circuit_data.get("max_cooldowns", 6)),
        label=str(circuit_data.get("label", "blocked:provider-unavailable")),
    )
    return ProviderResilienceConfig(
        short_retry=short_retry,
        circuit_breaker=circuit_breaker,
    )


def _parse_milestone_order(value: object) -> list[str]:
    """Parse milestones.order from YAML (list or comma-separated string)."""
    raw = value or []
    if isinstance(raw, str):
        raw = [m.strip() for m in raw.split(",") if m.strip()]
    if not isinstance(raw, list):
        raise ValueError("milestones.order must be a list or comma-separated string")
    return [str(m).strip() for m in raw if str(m).strip()]


def _apply_optional_sections(config: "Config", sections: dict) -> None:
    """Apply optional complex config sections."""
    if sections["triage"]:
        config.triage = _parse_triage_config(sections["triage"])
    if sections["scheduling"]:
        config.scheduling = _parse_scheduling_config(sections["scheduling"])
    if sections["e2e"]:
        config.e2e = _parse_e2e_config(sections["e2e"])
    if sections["timeline"]:
        config.timeline = _parse_timeline_config(sections["timeline"])
    if sections["sqlite_backup"]:
        config.sqlite_backup = _parse_sqlite_backup_config(sections["sqlite_backup"])
    if sections["goal_pilot"]:
        config.goal_pilot = _parse_goal_pilot_config(sections["goal_pilot"])
    if sections["claims"]:
        config.claims = _parse_claims_config(sections["claims"])
    if sections["hooks"]:
        config.hooks = _parse_hooks_config(sections["hooks"])
    if sections["provider_resilience"]:
        config.provider_resilience = _parse_provider_resilience_config(sections["provider_resilience"])


def _load_repo_section(config: "Config", repo_section: dict, github_section: dict) -> None:
    """Load repo and GitHub settings into config."""
    config.repo = repo_section.get("name")
    config.github_token = github_section.get("token")
    config.github_token_env = github_section.get("token_env")
    config.github_api_url = github_section.get("api_url", "https://api.github.com")
    config.github_http_timeout_seconds = github_section.get("http_timeout_seconds", 20.0)
    config.github_cache_ttl_seconds = github_section.get("cache_ttl_seconds", 300)
    required_scopes = github_section.get("required_scopes", []) or []
    allowed_scopes = github_section.get("allowed_scopes", []) or []
    if isinstance(required_scopes, str):
        required_scopes = [s.strip() for s in required_scopes.split(",") if s.strip()]
    if isinstance(allowed_scopes, str):
        allowed_scopes = [s.strip() for s in allowed_scopes.split(",") if s.strip()]
    config.github_required_scopes = list(required_scopes)
    config.github_allowed_scopes = list(allowed_scopes)


def _load_github_write_verify(config: "Config", github_section: dict) -> None:
    """Load GitHub write verification and rate limit settings."""
    write_verify = github_section.get("write_verify", {})
    config.gh_write_verify_timeout_seconds = write_verify.get("timeout_seconds", 20)
    config.gh_write_verify_initial_delay_ms = write_verify.get("initial_delay_ms", 250)
    config.gh_write_verify_max_delay_ms = write_verify.get("max_delay_ms", 2000)
    config.gh_write_verify_backoff = write_verify.get("backoff", 1.5)
    config.gh_write_verify_jitter_ms = write_verify.get("jitter_ms", 0)

    rate_limit = github_section.get("rate_limit", {})
    config.gh_rate_limit_startup = rate_limit.get("startup", True)
    config.gh_rate_limit_every_calls = rate_limit.get("every_calls", 500)
    config.gh_rate_limit_warn_fraction = rate_limit.get("warn_fraction", 0.1)
    config.gh_rate_limit_warn_remaining = rate_limit.get("warn_remaining", 100)

    audit = github_section.get("audit", {})
    config.gh_audit_enabled = audit.get("enabled", False)
    config.gh_audit_events = audit.get("events", False)
    config.gh_audit_file = audit.get("file")


def _load_labels_section(config: "Config", labels_section: dict) -> None:
    """Load label configuration."""
    config.label_in_progress = labels_section.get("in_progress", "in-progress")
    config.label_blocked = labels_section.get("blocked", "blocked")
    config.label_needs_human = labels_section.get("needs_human", "needs-human")
    config.label_needs_rework = labels_section.get("needs_rework", "needs-rework")
    config.label_validation_failed = labels_section.get("validation_failed", "validation-failed")
    config.label_prefix = labels_section.get("prefix")


def _load_review_section(config: "Config", review_section: dict) -> None:
    """Load review workflow configuration."""
    config.review_enabled = review_section.get("enabled", False)
    config.code_review_agent = review_section.get("default")
    config.code_review_label = review_section.get("code_review_label", "needs-code-review")
    config.code_reviewed_label = review_section.get("code_reviewed_label", "code-reviewed")
    config.triage_review_agent = review_section.get("triage_review_agent")
    config.triage_review_label = review_section.get("triage_review_label")
    config.triage_reviewed_label = review_section.get("triage_reviewed_label", "triage-reviewed")
    config.triage_failed_label = review_section.get("triage_failed_label", "triage-failed")
    config.triage_review_threshold = review_section.get("triage_review_threshold", 0)
    config.triage_review_on_failure = review_section.get("triage_review_on_failure", True)
    config.max_rework_cycles = review_section.get("max_rework_cycles", 10)
    config.reviewer_feedback_cache_minutes = review_section.get("reviewer_feedback_cache_minutes", 5)
    config.review_keep_current_approach_label = review_section.get(
        "keep_current_approach_label",
        "reviewer-keep-current-approach",
    )
    exchange_section = review_section.get("exchange", {})
    config.review_exchange_mode = exchange_section.get("mode", "via-draft-pr")
    probe_section = exchange_section.get("probe", {})
    if isinstance(probe_section, dict):
        config.review_exchange_probe_schedule = probe_section.get("schedule", "daily")
        config.review_exchange_probe_interval_days = probe_section.get("interval_days", 1)
    loop_section = exchange_section.get("loop", {})
    if isinstance(loop_section, dict):
        config.review_exchange_max_rounds = loop_section.get("max_rounds", 10)
        config.review_exchange_max_no_progress = loop_section.get("max_no_progress", 2)
        config.review_exchange_require_validation = loop_section.get("require_validation", True)
    # agent_pair removed; coder/reviewer derived at runtime


def _load_cleanup_section(config: "Config", cleanup_section: dict) -> None:
    """Load cleanup configuration."""
    if cleanup_section:
        with_triage_data = cleanup_section.get("with_triage", {})
        without_triage_data = cleanup_section.get("without_triage", {})

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


def _load_worktrees_section(
    config: "Config",
    worktrees_section: dict,
    repo_root: Path,
    config_path: Path,
) -> None:
    """Load worktree configuration."""
    worktree_base_raw = worktrees_section.get("base")
    if worktree_base_raw is None:
        config.worktree_base = repo_root.parent
    else:
        config.worktree_base = resolve_relative_path(worktree_base_raw, repo_root)

    base_branch_override_raw = worktrees_section.get("base_branch_override")
    if base_branch_override_raw is None:
        config.worktree_base_branch_override = None
    else:
        base_branch_override = str(base_branch_override_raw).strip()
        config.worktree_base_branch_override = base_branch_override or None

    # Validate worktree_base is usable
    try:
        config.worktree_base.mkdir(parents=True, exist_ok=True)
    except OSError as e:
        raise ValueError(
            f"worktrees.base '{config.worktree_base}' cannot be created: {e}. "
            "Specify an absolute path in your config under worktrees.base"
        )

    if "setup" in worktrees_section:
        config.setup_worktree = worktrees_section.get("setup", [])
    config.reuse_push_preflight = worktrees_section.get("reuse_push_preflight", True)
    config.allow_no_verify_dry_run_preflight = worktrees_section.get(
        "allow_no_verify_dry_run_preflight", True
    )
    config.worktree_branch_on_recreate = worktrees_section.get(
        "worktree_branch_on_recreate", "delete"
    )

    remediation_section = _get_section(worktrees_section, "remediation", config_path)
    config.worktree_remediation_pr_collision = remediation_section.get(
        "pr_collision", "new_branch"
    )
    config.worktree_remediation_push_rebase_retry = remediation_section.get(
        "push_rebase_retry", True
    )


def _load_execution_section(config: "Config", execution_section: dict, config_path: Path) -> None:
    """Load execution and terminal configuration."""
    # Concurrency
    concurrency = _get_section(execution_section, "concurrency", config_path)
    config.max_concurrent_sessions = concurrency.get("max_concurrent_sessions", 3)
    config.session_timeout_minutes = concurrency.get("session_timeout_minutes", 45)

    # Terminal adapter
    config.terminal_adapter = execution_section.get("terminal_adapter")

    # Isolation
    isolation_data = execution_section.get("isolation", {})
    if isolation_data:
        config.isolation = IsolationConfig(mode=isolation_data.get("mode", "standard"))


def _load_ui_section(config: "Config", ui_section: dict) -> None:
    """Load UI configuration."""
    config.ui_mode = ui_section.get("mode", "web")
    config.web_port = ui_section.get("web_port", 8080)
    config.control_api_port = ui_section.get("control_api_port", 19080)
    config.queue_refresh_seconds = ui_section.get("queue_refresh_seconds", 600)
    config.instances = ui_section.get("instances", 1)


def _load_observability_section(config: "Config", observability_section: dict) -> None:
    """Load observability configuration."""
    config.session_no_output_seconds = observability_section.get("session_no_output_seconds", 120)
    config.session_no_output_tail_lines = observability_section.get("session_no_output_tail_lines", 50)
    config.session_no_output_max_bytes = observability_section.get("session_no_output_max_bytes", 10000)
    config.session_no_output_repeat_seconds = observability_section.get("session_no_output_repeat_seconds", 120)
    config.session_output_retention_runs = observability_section.get("session_output_retention_runs", 7)
    config.stale_escalation_ticks = observability_section.get("stale_escalation_ticks", 0)

    # Comment headings
    headings_data = observability_section.get("comment_headings", {})
    if headings_data:
        config.comment_headings = CommentHeadings(
            implementation=headings_data.get("implementation", "## Implementation"),
            problems=headings_data.get("problems", "## Problems Encountered"),
            pr_link=headings_data.get("pr_link", "## Pull Request"),
            blocked=headings_data.get("blocked", "## Blocked"),
            needs_human=headings_data.get("needs_human", "## Needs Human Input"),
        )


def _load_security_section(config: "Config", security_section: dict, repo_root: Path) -> None:
    """Load security configuration."""
    config.enforce_hooks = security_section.get("enforce_hooks", True)
    if security_section.get("pre_push_hook"):
        config.pre_push_hook = resolve_relative_path(security_section["pre_push_hook"], repo_root)

    dangerous_data = security_section.get("dangerous", {})
    if dangerous_data:
        config.dangerous = DangerousConfig(
            allow_unsupported_agents=dangerous_data.get("allow_unsupported_agents", False),
        )


def _load_validation_section(config: "Config", validation_section: dict) -> None:
    """Load validation configuration."""
    if validation_section:
        coverage_data = validation_section.get("coverage_guardrail", {}) or {}
        config.validation = ValidationConfig(
            cmd=validation_section.get("cmd"),
            timeout_seconds=validation_section.get("timeout_seconds", 300),
            pre_push_dirty_check=validation_section.get("pre_push_dirty_check", "tracked"),
            coverage_guardrail=CoverageGuardrailConfig(
                enabled=coverage_data.get("enabled", False),
                min_percent=coverage_data.get("min_percent"),
                apply_to=coverage_data.get("apply_to", "changed"),
                scope=coverage_data.get("scope", []) or [],
                coverage_type=coverage_data.get("coverage_type", "line"),
                exclude=coverage_data.get("exclude", []) or [],
            ),
        )


def _load_retry_section(config: "Config", data: dict) -> None:
    """Load retry configuration."""
    retry_data = data.get("retry", {})
    if retry_data:
        config.retry = RetryConfig(
            max_validation_retries=retry_data.get("max_validation_retries", 3),
            validation_error_file=retry_data.get("validation_error_file", "validation-errors.txt"),
            retry_prompt_template=retry_data.get("retry_prompt_template"),
        )


def _parse_ai_systems_allowed(value: object) -> list[str]:
    """Normalize ai_systems.allowed values from config."""
    if not value:
        return []
    if isinstance(value, str):
        return [entry.strip() for entry in value.split(",") if entry.strip()]
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return []


def _load_agents_section(
    config: "Config",
    agents_section: dict,
    repo_root: Path,
) -> None:
    """Load agent configurations."""
    for label, agent_data in agents_section.items():
        prompt_relative = agent_data["prompt"]
        prompt_path = resolve_relative_path(prompt_relative, repo_root)

        # Inherit provider from default_agent if not specified
        provider = agent_data.get("provider")
        if provider is None and config.default_agent:
            provider = config.default_agent.provider

        # Inherit model from default_agent if not specified
        model = agent_data.get("model")
        if model is None and config.default_agent and config.default_agent.model:
            model = config.default_agent.model
        if model is None:
            model = "sonnet"  # Fallback default

        # Merge provider_args
        provider_args = {}
        if config.default_agent and config.default_agent.provider_args:
            provider_args.update(config.default_agent.provider_args)
        if agent_data.get("provider_args"):
            provider_args.update(agent_data["provider_args"])

        agent_kwargs = {
            "prompt_path": prompt_path,
            "prompt_relative": prompt_relative,
            "provider": provider,
            "model": model,
            "timeout_minutes": agent_data.get("timeout_minutes", 45),
            "provider_args": provider_args,
            "permission_mode": agent_data.get("permission_mode", "default"),
            "skip_review": agent_data.get("skip_review", False),
            "reviewer": agent_data.get("reviewer"),
            "meta_agent": agent_data.get("meta_agent"),
            "ai_system": agent_data.get("ai_system"),
            "retry_prompt_template": agent_data.get("retry_prompt_template"),
        }
        if "command" in agent_data:
            agent_kwargs["command"] = agent_data["command"]
        if "initial_prompt" in agent_data:
            agent_kwargs["initial_prompt"] = agent_data["initial_prompt"]
        config.agents[label] = AgentConfig(**agent_kwargs)


_TOP_LEVEL_SECTION_KEYS = (
    "agents", "labels", "review", "cleanup", "worktrees", "execution",
    "validation", "provider_resilience", "ui", "observability", "timeline", "security", "filtering",
    "triage", "scheduling", "e2e", "goal_pilot", "milestones", "state", "config", "claims", "hooks",
    "ai_systems",
    "triage", "scheduling", "e2e", "milestones", "state", "config", "claims", "hooks",
    "sqlite_backup",
)

# Derive ALLOWED_TOP_LEVEL_FIELDS from _TOP_LEVEL_SECTION_KEYS — single source of truth.
# "repo" and "default_agent" are parsed separately but are valid top-level keys.
ALLOWED_TOP_LEVEL_FIELDS = frozenset(_TOP_LEVEL_SECTION_KEYS) | {"repo", "default_agent"}


def _extract_config_sections(data: dict, config_path: Path) -> dict:  # noqa: C901 - extracts 17+ config sections via _get_section calls
    """Extract all sections from config data."""
    repo_section = _get_section(data, "repo", config_path)
    sections = {key: _get_section(data, key, config_path) for key in _TOP_LEVEL_SECTION_KEYS}
    sections["repo"] = repo_section
    sections["github"] = _get_section(repo_section, "github", config_path)
    return sections


@dataclass
class Config:
    """Orchestrator configuration."""

    # Agent configurations keyed by label (e.g., "agent:web")
    agents: dict[str, AgentConfig] = field(default_factory=dict)

    # Default agent configuration inherited by agents without explicit provider
    default_agent: Optional[DefaultAgentConfig] = None

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
    worktree_base_branch_override: Optional[str] = None  # Override base branch for worktree creation
    worktree_branch_on_recreate: str = "delete"  # delete or create_new_branch

    # Config validation
    config_strict: bool = False  # If True, unknown fields cause validation errors; if False, warnings only

    # AI systems allowlist (merged with built-in ai_systems.yaml)
    ai_systems_allowed: list[str] = field(default_factory=list)

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
    session_output_retention_runs: int = 7  # Runs to keep per worktree

    # UI mode: "web" (default, browser dashboard)
    ui_mode: str = "web"
    web_port: int = 8080  # Port for web dashboard
    control_api_port: int = 19080  # Port for control API (always available, 0 = disabled)
    queue_refresh_seconds: int = 600  # How often web UI refetches queue from GitHub (0 = manual only)

    # Multi-instance support (for multi-orchestrator coordination)
    instances: int = 1  # Number of orchestrator instances to spawn (CC manages this)
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
    # Can be "subprocess" or a full class path for custom adapters
    terminal_adapter: Optional[str] = None

    # Milestone sorting strategy - built-in: "due_date", "number", "pattern", "name"
    # Or provide a custom class path like "mymodule.MyStrategy"
    milestone_sort: str = "due_date"
    # Config passed to strategy via **kwargs (e.g., pattern="M(\\d+)" for PatternStrategy)
    milestone_sort_config: dict = field(default_factory=dict)
    # Optional explicit order for milestones (titles). Unlisted milestones follow milestone_sort.
    milestone_order: list[str] = field(default_factory=list)
    # Foundation milestone - dependencies must be same milestone OR in foundation
    foundation_milestone: str = "M0"

    # Cleanup configuration - when to close AI session tabs and remove worktrees
    cleanup: CleanupConfig = field(default_factory=CleanupConfig)

    # Enforcement options
    enforce_hooks: bool = True  # Install pre-push hooks (runs project validation + orchestrator checks)
    pre_push_hook: Optional[Path] = None  # Custom pre-push hook path (uses bundled if None)

    # Worktree setup commands (run after worktree creation, e.g., npm install)
    setup_worktree: list[str] = field(
        default_factory=lambda: ["make install-vscode-extensions"]
    )
    # Preflight a dry-run push when reusing worktrees to catch stale refs early.
    reuse_push_preflight: bool = True
    # Allow git push --dry-run --no-verify for reuse preflight (default on).
    allow_no_verify_dry_run_preflight: bool = True
    # Remediation strategies for publish-time failures.
    worktree_remediation_pr_collision: str = "new_branch"  # fail | reuse_open | new_branch
    worktree_remediation_push_rebase_retry: bool = True

    # Code review workflow (optional) - per-PR review after agent creates PR
    review_enabled: bool = False  # Explicit toggle for code review
    code_review_agent: Optional[str] = None  # Agent that reviews PRs (e.g., "agent:reviewer")
    code_review_label: Optional[str] = None  # Label on PRs needing review (e.g., "needs-code-review")
    code_reviewed_label: Optional[str] = None  # Label after review passes (e.g., "code-reviewed")

    # Triage/batch review workflow (optional) - pattern review across multiple PRs
    triage_review_agent: Optional[str] = None  # Agent that does batch reviews (e.g., "agent:triage")
    triage_review_label: Optional[str] = None  # Label for PRs awaiting triage review (uses code_reviewed_label if not set)
    triage_reviewed_label: Optional[str] = None  # Label after triage review (e.g., "triage-reviewed")
    triage_failed_label: Optional[str] = None  # Label when triage fails (e.g., "triage-failed")
    triage_review_threshold: int = 0  # Trigger triage review after N PRs (0 = manual only)
    triage_review_on_failure: bool = True  # Trigger triage to investigate when sessions fail

    # Rework cycle limit (when reviewer requests changes)
    max_rework_cycles: int = 10  # Max times to re-queue work agent before escalating to needs-human

    # Reviewer feedback cache: write feedback locally on review completion and use it
    # for rework sessions within this time window (avoids GitHub eventual consistency issues)
    # -1 = disabled, 0+ = minutes to trust local file over GitHub API
    reviewer_feedback_cache_minutes: int = 5  # Default: 5 minutes
    # Label to tell reviewer to keep the current approach
    review_keep_current_approach_label: str = "reviewer-keep-current-approach"

    # Review exchange mode (via-mcp, via-local-loop, or via-draft-pr review)
    review_exchange_mode: str = "via-draft-pr"
    review_exchange_probe_schedule: str = "daily"  # startup, daily, interval, manual
    review_exchange_probe_interval_days: int = 1
    review_exchange_max_rounds: int = 10
    review_exchange_max_no_progress: int = 2
    review_exchange_require_validation: bool = True

    # Dangerous options (use with caution)
    dangerous: DangerousConfig = field(default_factory=DangerousConfig)

    # Validation configuration - single command runs on agent-done and pre-push
    validation: ValidationConfig = field(default_factory=ValidationConfig)

    # Retry configuration - validation retry with error injection
    retry: RetryConfig = field(default_factory=RetryConfig)

    # Provider resilience configuration (retries + circuit breaker)
    provider_resilience: ProviderResilienceConfig = field(default_factory=ProviderResilienceConfig)

    # Isolation configuration - how agents are sandboxed
    isolation: IsolationConfig = field(default_factory=IsolationConfig)

    # Triage issue configuration - label/milestone inheritance
    triage: TriageConfig = field(default_factory=TriageConfig)

    # Scheduling configuration
    scheduling: SchedulingConfig = field(default_factory=SchedulingConfig)

    # E2E async test runner configuration
    e2e: E2EConfig = field(default_factory=E2EConfig)

    # Goal Pilot AI configuration
    goal_pilot: GoalPilotConfig = field(default_factory=GoalPilotConfig)
    # SQLite backup configuration
    sqlite_backup: SqliteBackupConfig = field(default_factory=SqliteBackupConfig)
    # Timeline retention configuration
    timeline: TimelineConfig = field(default_factory=TimelineConfig)

    # Goal Pilot AI configuration
    goal_pilot: GoalPilotConfig = field(default_factory=GoalPilotConfig)

    # Claims/lease configuration for multi-orchestrator coordination
    claims: ClaimsConfig = field(default_factory=ClaimsConfig)

    # Hooks configuration - AI gate tests for agent hooks/execpolicy
    hooks: HooksConfig = field(default_factory=HooksConfig)

    # Stale in-progress escalation threshold (0 = disabled)
    # If an issue has stale in-progress for K consecutive ticks, emit escalation event
    stale_escalation_ticks: int = 0

    # Path to the config file (set during load)
    config_path: Optional[Path] = None

    # Raw YAML data for unknown field validation (set during load)
    raw_data: dict = field(default_factory=dict)
    raw_agents: dict = field(default_factory=dict)

    @property
    def orchestrator_id(self) -> str:
        """Unique identifier for this orchestrator instance.

        Uses the directory name of repo_root as the identifier.
        This must be consistent across all components (orchestrator, web UI, control API)
        to ensure E2E test status and other cross-component data is correctly matched.
        """
        return self.repo_root.name

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

    def get_label_provider_unavailable(self) -> str:
        """Get the provider-unavailable blocked label with prefix if configured."""
        return self.prefixed_label(self.provider_resilience.circuit_breaker.label)

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

    def _serialization_exchange_dict(self) -> dict:
        exchange_dict: dict = {}
        if self.review_exchange_mode != "via-draft-pr":
            exchange_dict["mode"] = self.review_exchange_mode
        if self.review_exchange_probe_schedule != "daily" or self.review_exchange_probe_interval_days != 1:
            exchange_dict["probe"] = {
                "schedule": self.review_exchange_probe_schedule,
                "interval_days": self.review_exchange_probe_interval_days,
            }
        if (
            self.review_exchange_max_rounds != 10
            or self.review_exchange_max_no_progress != 2
            or self.review_exchange_require_validation is not True
        ):
            exchange_dict["loop"] = {
                "max_rounds": self.review_exchange_max_rounds,
                "max_no_progress": self.review_exchange_max_no_progress,
                "require_validation": self.review_exchange_require_validation,
            }
        return exchange_dict

    def _runtime_exchange_dict(self) -> dict[str, object]:
        exchange_dict: dict[str, object] = {"mode": self.review_exchange_mode}
        exchange_dict["probe"] = {
            "schedule": self.review_exchange_probe_schedule,
            "interval_days": self.review_exchange_probe_interval_days,
        }
        exchange_dict["loop"] = {
            "max_rounds": self.review_exchange_max_rounds,
            "max_no_progress": self.review_exchange_max_no_progress,
            "require_validation": self.review_exchange_require_validation,
        }
        return exchange_dict

    def get_label_review_keep_current_approach(self) -> str:
        """Get the reviewer keep-current-approach label with prefix if configured."""
        return self.prefixed_label(self.review_keep_current_approach_label)

    def to_event_dict(self) -> dict:
        """Convert config to a dict for event emission.

        Returns a serializable dict with the merged configuration
        (YAML + command line overrides) for debugging.
        """
        return {
            "repo": {
                "name": self.repo,
                "root": str(self.repo_root),
                "github": {
                    "token_env": self.github_token_env,
                    "api_url": self.github_api_url,
                    "http_timeout_seconds": self.github_http_timeout_seconds,
                    "cache_ttl_seconds": self.github_cache_ttl_seconds,
                    "required_scopes": list(self.github_required_scopes),
                    "allowed_scopes": list(self.github_allowed_scopes),
                    "write_verify": {
                        "timeout_seconds": self.gh_write_verify_timeout_seconds,
                        "initial_delay_ms": self.gh_write_verify_initial_delay_ms,
                        "max_delay_ms": self.gh_write_verify_max_delay_ms,
                        "backoff": self.gh_write_verify_backoff,
                        "jitter_ms": self.gh_write_verify_jitter_ms,
                    },
                    "rate_limit": {
                        "startup": self.gh_rate_limit_startup,
                        "every_calls": self.gh_rate_limit_every_calls,
                        "warn_fraction": self.gh_rate_limit_warn_fraction,
                        "warn_remaining": self.gh_rate_limit_warn_remaining,
                    },
                    "audit": {
                        "enabled": self.gh_audit_enabled,
                        "events": self.gh_audit_events,
                        "file": self.gh_audit_file,
                    },
                },
            },
            "config": {
                "path": str(self.config_path) if self.config_path else None,
                "strict": self.config_strict,
            },
            "state": {
                "file": str(self.state_file),
            },
            "worktrees": {
                "base": str(self.worktree_base),
                "base_branch_override": self.worktree_base_branch_override,
                "setup": list(self.setup_worktree),
                "reuse_push_preflight": self.reuse_push_preflight,
                "allow_no_verify_dry_run_preflight": self.allow_no_verify_dry_run_preflight,
                "worktree_branch_on_recreate": self.worktree_branch_on_recreate,
                "remediation": {
                    "pr_collision": self.worktree_remediation_pr_collision,
                    "push_rebase_retry": self.worktree_remediation_push_rebase_retry,
                },
            },
            "execution": {
                "concurrency": {
                    "max_concurrent_sessions": self.max_concurrent_sessions,
                    "session_timeout_minutes": self.session_timeout_minutes,
                },
                "terminal_adapter": self.terminal_adapter,
                "isolation": {
                    "mode": self.isolation.mode,
                },
            },
            "ui": {
                "mode": self.ui_mode,
                "web_port": self.web_port,
                "control_api_port": self.control_api_port,
                "queue_refresh_seconds": self.queue_refresh_seconds,
                "instances": self.instances,
            },
            "observability": {
                "session_no_output_seconds": self.session_no_output_seconds,
                "session_no_output_tail_lines": self.session_no_output_tail_lines,
                "session_no_output_max_bytes": self.session_no_output_max_bytes,
                "session_no_output_repeat_seconds": self.session_no_output_repeat_seconds,
                "session_output_retention_runs": self.session_output_retention_runs,
                "stale_escalation_ticks": self.stale_escalation_ticks,
                "comment_headings": {
                    "implementation": self.comment_headings.implementation,
                    "problems": self.comment_headings.problems,
                    "pr_link": self.comment_headings.pr_link,
                    "blocked": self.comment_headings.blocked,
                    "needs_human": self.comment_headings.needs_human,
                },
            },
            "security": {
                "enforce_hooks": self.enforce_hooks,
                "pre_push_hook": str(self.pre_push_hook) if self.pre_push_hook else None,
                "dangerous": {
                    "allow_unsupported_agents": self.dangerous.allow_unsupported_agents,
                },
            },
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
                "pre_push_dirty_check": self.validation.pre_push_dirty_check,
                "coverage_guardrail": {
                    "enabled": self.validation.coverage_guardrail.enabled,
                    "min_percent": self.validation.coverage_guardrail.min_percent,
                    "apply_to": self.validation.coverage_guardrail.apply_to,
                    "scope": self.validation.coverage_guardrail.scope,
                    "coverage_type": self.validation.coverage_guardrail.coverage_type,
                    "exclude": self.validation.coverage_guardrail.exclude,
                },
            },
            "review": {
                "enabled": self.review_enabled,
                "default": self.code_review_agent,
                "code_review_label": self.code_review_label,
                "code_reviewed_label": self.code_reviewed_label,
                "exchange": self._runtime_exchange_dict(),
                "triage_review": {
                    "agent": self.triage_review_agent,
                    "label": self.triage_review_label,
                    "reviewed_label": self.triage_reviewed_label,
                    "threshold": self.triage_review_threshold,
                    "on_failure": self.triage_review_on_failure,
                },
                "max_rework_cycles": self.max_rework_cycles,
                "reviewer_feedback_cache_minutes": self.reviewer_feedback_cache_minutes,
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
            "milestones": {
                "sort": self.milestone_sort,
                "sort_config": self.milestone_sort_config,
                "order": list(self.milestone_order),
                "foundation": self.foundation_milestone,
            },
            "filtering": {
                "label": self.filtering.label,
                "milestone": self.filtering.milestone,
                "milestones": list(self.filtering.milestones),
                "issue": self.filtering.issue,
                "exclude_labels": list(self.filtering.exclude_labels),
                "fetch_limit": self.filtering.fetch_limit,
                "max_to_start": self.filtering.max_to_start,
            },
            "triage": {
                "inherit_labels": list(self.triage.inherit_labels),
                "explicit_labels": list(self.triage.explicit_labels),
                "milestone_strategy": {
                    "inherit_from_issues": self.triage.milestone_strategy.inherit_from_issues,
                    "explicit": self.triage.milestone_strategy.explicit,
                },
                "priority": self.triage.priority,
            },
            "scheduling": {
                "default_priority_tier": self.scheduling.default_priority_tier,
            },
            "e2e": {
                "pr_labels": self.e2e_pr_labels,
                "enabled": self.e2e.enabled,
                "auto_run_interval_minutes": self.e2e.auto_run_interval_minutes,
                "pytest_args": list(self.e2e.pytest_args),
                "allow_retry_once": self.e2e.allow_retry_once,
                "quarantine_file": self.e2e.quarantine_file,
                "survive_restart": self.e2e.survive_restart,
                "auto_quarantine": self.e2e.auto_quarantine,
                "auto_create_issues": self.e2e.auto_create_issues,
                "issue_agent_label": self.e2e.issue_agent_label,
            },
            "sqlite_backup": {
                "enabled": self.sqlite_backup.enabled,
                "cadence_hours": self.sqlite_backup.cadence_hours,
                "check_interval_minutes": self.sqlite_backup.check_interval_minutes,
                "retention_daily": self.sqlite_backup.retention_daily,
                "retention_weekly": self.sqlite_backup.retention_weekly,
                "enforce_on_startup": self.sqlite_backup.enforce_on_startup,
            },
            "claims": {
                "enabled": self.claims.enabled,
                "claimant_id": self.claims.claimant_id,
                "lease_seconds": self.claims.lease_seconds,
                "renew_before_expiry_seconds": self.claims.renew_before_expiry_seconds,
                "convergence_timeout_seconds": self.claims.convergence_timeout_seconds,
                "convergence_poll_min_ms": self.claims.convergence_poll_min_ms,
                "convergence_poll_max_ms": self.claims.convergence_poll_max_ms,
                "convergence_required_wins": self.claims.convergence_required_wins,
            },
            "hooks": {
                    "ai_gate": {
                        "interval_days": self.hooks.ai_gate.interval_days,
                        "dangerous_allow_failure": self.hooks.ai_gate.dangerous_allow_failure,
                    },
            },
            "agents": {
                label: {
                    "prompt_path": str(cfg.prompt_path),
                    "model": cfg.model,
                    "timeout_minutes": cfg.timeout_minutes,
                    "meta_agent": cfg.meta_agent,
                }
                for label, cfg in self.agents.items()
            },
        }

    def to_dict(self) -> dict:  # noqa: C901, PLR0912 - serialization method handles many config fields
        """Convert config to a dict suitable for YAML serialization.

        Returns a dict that can be saved back to a YAML config file.
        This preserves the original YAML structure as closely as possible.
        """
        # Build agents section
        agents_dict = {}
        for label, agent in self.agents.items():
            agent_dict: dict = {
                "prompt": agent.prompt_relative,
            }
            if agent.provider:
                agent_dict["provider"] = agent.provider
            if agent.model and agent.model != "sonnet":
                agent_dict["model"] = agent.model
            if agent.timeout_minutes != 45:
                agent_dict["timeout_minutes"] = agent.timeout_minutes
            if agent.provider_args:
                agent_dict["provider_args"] = dict(agent.provider_args)
            if agent.permission_mode != "default":
                agent_dict["permission_mode"] = agent.permission_mode
            if agent.skip_review:
                agent_dict["skip_review"] = agent.skip_review
            if agent.reviewer:
                agent_dict["reviewer"] = agent.reviewer
            if agent.meta_agent:
                agent_dict["meta_agent"] = agent.meta_agent
            if agent.ai_system:
                agent_dict["ai_system"] = agent.ai_system
            if agent.retry_prompt_template:
                agent_dict["retry_prompt_template"] = agent.retry_prompt_template
            agents_dict[label] = agent_dict

        result: dict = {
            "repo": {
                "name": self.repo,
            },
            "agents": agents_dict,
        }

        # Add default_agent if set
        if self.default_agent:
            default_agent_dict: dict = {}
            if self.default_agent.provider:
                default_agent_dict["provider"] = self.default_agent.provider
            if self.default_agent.model:
                default_agent_dict["model"] = self.default_agent.model
            if self.default_agent.provider_args:
                default_agent_dict["provider_args"] = dict(self.default_agent.provider_args)
            if default_agent_dict:
                result["default_agent"] = default_agent_dict

        # Labels section
        labels_dict: dict = {}
        if self.label_in_progress != "in-progress":
            labels_dict["in_progress"] = self.label_in_progress
        if self.label_blocked != "blocked":
            labels_dict["blocked"] = self.label_blocked
        if self.label_needs_human != "needs-human":
            labels_dict["needs_human"] = self.label_needs_human
        if self.label_needs_rework != "needs-rework":
            labels_dict["needs_rework"] = self.label_needs_rework
        if self.label_validation_failed != "validation-failed":
            labels_dict["validation_failed"] = self.label_validation_failed
        if self.label_prefix:
            labels_dict["prefix"] = self.label_prefix
        if labels_dict:
            result["labels"] = labels_dict

        # Execution section
        execution_dict: dict = {
            "concurrency": {
                "max_concurrent_sessions": self.max_concurrent_sessions,
                "session_timeout_minutes": self.session_timeout_minutes,
            },
        }
        if self.terminal_adapter:
            execution_dict["terminal_adapter"] = self.terminal_adapter
        if self.isolation.mode != "standard":
            execution_dict["isolation"] = {"mode": self.isolation.mode}
        result["execution"] = execution_dict

        # Provider resilience section
        provider_dict: dict = {}
        short_dict: dict = {}
        short = self.provider_resilience.short_retry
        if short.max_attempts != 4:
            short_dict["max_attempts"] = short.max_attempts
        if short.initial_backoff_seconds != 5:
            short_dict["initial_backoff_seconds"] = short.initial_backoff_seconds
        if short.max_backoff_seconds != 60:
            short_dict["max_backoff_seconds"] = short.max_backoff_seconds
        if short.jitter is not True:
            short_dict["jitter"] = short.jitter
        if short_dict:
            provider_dict["short_retry"] = short_dict

        circuit = self.provider_resilience.circuit_breaker
        circuit_dict: dict = {}
        if circuit.cooldown_seconds != 1800:
            circuit_dict["cooldown_seconds"] = circuit.cooldown_seconds
        if circuit.max_cooldowns != 6:
            circuit_dict["max_cooldowns"] = circuit.max_cooldowns
        if circuit.label != "blocked:provider-unavailable":
            circuit_dict["label"] = circuit.label
        if circuit_dict:
            provider_dict["circuit_breaker"] = circuit_dict

        if provider_dict:
            result["provider_resilience"] = provider_dict

        # UI section
        ui_dict: dict = {}
        if self.ui_mode != "web":
            ui_dict["mode"] = self.ui_mode
        if self.web_port != 8080:
            ui_dict["web_port"] = self.web_port
        if self.control_api_port != 19080:
            ui_dict["control_api_port"] = self.control_api_port
        if self.queue_refresh_seconds != 600:
            ui_dict["queue_refresh_seconds"] = self.queue_refresh_seconds
        if self.instances != 1:
            ui_dict["instances"] = self.instances
        if ui_dict:
            result["ui"] = ui_dict

        # Observability section
        observability_dict: dict = {}
        if self.session_no_output_seconds != 120:
            observability_dict["session_no_output_seconds"] = self.session_no_output_seconds
        if self.stale_escalation_ticks != 0:
            observability_dict["stale_escalation_ticks"] = self.stale_escalation_ticks
        if self.session_output_retention_runs != 7:
            observability_dict["session_output_retention_runs"] = self.session_output_retention_runs
        if observability_dict:
            result["observability"] = observability_dict

        # Filtering section
        filtering_dict: dict = {}
        if self.filtering.label:
            filtering_dict["label"] = self.filtering.label
        if self.filtering.milestones:
            filtering_dict["milestones"] = list(self.filtering.milestones)
        elif self.filtering.milestone:
            filtering_dict["milestone"] = self.filtering.milestone
        if self.filtering.exclude_labels:
            filtering_dict["exclude_labels"] = list(self.filtering.exclude_labels)
        if self.filtering.fetch_limit != 100:
            filtering_dict["fetch_limit"] = self.filtering.fetch_limit
        if self.filtering.max_to_start != 0:
            filtering_dict["max_to_start"] = self.filtering.max_to_start
        if filtering_dict:
            result["filtering"] = filtering_dict

        # Scheduling section
        if self.scheduling.default_priority_tier != 1:
            result["scheduling"] = {
                "default_priority_tier": self.scheduling.default_priority_tier,
            }

        # Review section
        review_dict: dict = {}
        if self.review_enabled:
            review_dict["enabled"] = True
        if self.code_review_agent:
            review_dict["default"] = self.code_review_agent
        if self.code_review_label:
            review_dict["code_review_label"] = self.code_review_label
        if self.code_reviewed_label:
            review_dict["code_reviewed_label"] = self.code_reviewed_label
        if self.triage_review_agent:
            review_dict["triage_review_agent"] = self.triage_review_agent
        if self.triage_review_label:
            review_dict["triage_review_label"] = self.triage_review_label
        if self.triage_reviewed_label and self.triage_reviewed_label != "triage-reviewed":
            review_dict["triage_reviewed_label"] = self.triage_reviewed_label
        if self.triage_review_threshold != 0:
            review_dict["triage_review_threshold"] = self.triage_review_threshold
        if self.max_rework_cycles != 10:
            review_dict["max_rework_cycles"] = self.max_rework_cycles
        if self.review_keep_current_approach_label != "reviewer-keep-current-approach":
            review_dict["keep_current_approach_label"] = self.review_keep_current_approach_label
        exchange_dict = self._serialization_exchange_dict()
        if exchange_dict:
            review_dict["exchange"] = exchange_dict
        if review_dict:
            result["review"] = review_dict

        # Goal Pilot section
        goal_pilot_dict: dict = {}
        if self.goal_pilot.enabled:
            goal_pilot_dict["enabled"] = True
        if self.goal_pilot.agent:
            goal_pilot_dict["agent"] = self.goal_pilot.agent
        if self.goal_pilot.approval_policy != "journeys_only":
            goal_pilot_dict["approval_policy"] = self.goal_pilot.approval_policy
        if self.goal_pilot.approval_batch_size != 10:
            goal_pilot_dict["approval_batch_size"] = self.goal_pilot.approval_batch_size
        if self.goal_pilot.approval_batch_window_minutes != 60:
            goal_pilot_dict["approval_batch_window_minutes"] = self.goal_pilot.approval_batch_window_minutes
        if goal_pilot_dict:
            result["goal_pilot"] = goal_pilot_dict

        # Worktrees section
        worktrees_dict: dict = {}
        # Only include worktree_base if it was explicitly set (not the default)
        if self.worktree_base != self.repo_root.parent:
            worktrees_dict["base"] = str(self.worktree_base)
        if self.worktree_base_branch_override:
            worktrees_dict["base_branch_override"] = self.worktree_base_branch_override
        if self.setup_worktree:
            worktrees_dict["setup"] = list(self.setup_worktree)
        if self.worktree_branch_on_recreate != "delete":
            worktrees_dict["worktree_branch_on_recreate"] = self.worktree_branch_on_recreate
        if worktrees_dict:
            result["worktrees"] = worktrees_dict

        # E2E section
        e2e_dict: dict = {}
        if self.e2e.enabled:
            e2e_dict["enabled"] = True
        if self.e2e.role != "auto":
            e2e_dict["role"] = self.e2e.role
        if self.e2e.auto_run_interval_minutes != 30:
            e2e_dict["auto_run_interval_minutes"] = self.e2e.auto_run_interval_minutes
        if self.e2e.pytest_args != ["tests/e2e", "-v"]:
            e2e_dict["pytest_args"] = list(self.e2e.pytest_args)
        if not self.e2e.allow_retry_once:
            e2e_dict["allow_retry_once"] = False
        if self.e2e.quarantine_file != "tests/e2e/quarantine.txt":
            e2e_dict["quarantine_file"] = self.e2e.quarantine_file
        if self.e2e.stop_on_first_failure:
            e2e_dict["stop_on_first_failure"] = True
        if not self.e2e.auto_quarantine:
            e2e_dict["auto_quarantine"] = False
        if not self.e2e.auto_create_issues:
            e2e_dict["auto_create_issues"] = False
        if self.e2e.issue_agent_label != "agent:backend":
            e2e_dict["issue_agent_label"] = self.e2e.issue_agent_label
        if e2e_dict:
            result["e2e"] = e2e_dict

        # SQLite backup section
        if self.sqlite_backup != SqliteBackupConfig():
            result["sqlite_backup"] = {
                "enabled": self.sqlite_backup.enabled,
                "cadence_hours": self.sqlite_backup.cadence_hours,
                "check_interval_minutes": self.sqlite_backup.check_interval_minutes,
                "retention_daily": self.sqlite_backup.retention_daily,
                "retention_weekly": self.sqlite_backup.retention_weekly,
                "enforce_on_startup": self.sqlite_backup.enforce_on_startup,
            }

        # Timeline section
        if self.timeline != TimelineConfig():
            result["timeline"] = {
                "max_records": self.timeline.max_records,
            }

        # Validation section
        validation_dict: dict = {}
        if self.validation.cmd:
            validation_dict["cmd"] = self.validation.cmd
            if self.validation.timeout_seconds != 300:
                validation_dict["timeout_seconds"] = self.validation.timeout_seconds
        if self.validation.pre_push_dirty_check != "tracked":
            validation_dict["pre_push_dirty_check"] = self.validation.pre_push_dirty_check
        if self.validation.coverage_guardrail != CoverageGuardrailConfig():
            validation_dict["coverage_guardrail"] = {
                "enabled": self.validation.coverage_guardrail.enabled,
                "min_percent": self.validation.coverage_guardrail.min_percent,
                "apply_to": self.validation.coverage_guardrail.apply_to,
                "scope": list(self.validation.coverage_guardrail.scope),
                "coverage_type": self.validation.coverage_guardrail.coverage_type,
                "exclude": list(self.validation.coverage_guardrail.exclude),
            }
        if validation_dict:
            result["validation"] = validation_dict

        # Security section
        security_dict: dict = {}
        if not self.enforce_hooks:
            security_dict["enforce_hooks"] = False
        if self.dangerous.allow_unsupported_agents:
            security_dict["dangerous"] = {"allow_unsupported_agents": True}
        if security_dict:
            result["security"] = security_dict

        # Hooks section (only include if non-default)
        hooks_dict: dict = {}
        if self.hooks.ai_gate.interval_days != 7:
            hooks_dict.setdefault("ai_gate", {})["interval_days"] = self.hooks.ai_gate.interval_days
        if self.hooks.ai_gate.dangerous_allow_failure:
            hooks_dict.setdefault("ai_gate", {})["dangerous_allow_failure"] = True
        if hooks_dict:
            result["hooks"] = hooks_dict

        return result

    def save(self, path: Optional[Path] = None) -> Path:
        """Save configuration to a YAML file.

        Args:
            path: Path to save to. If None, uses self.config_path.

        Returns:
            The path the config was saved to.

        Raises:
            ValueError: If no path is specified and config_path is not set.
        """
        save_path = path or self.config_path
        if save_path is None:
            raise ValueError("No path specified and config_path is not set")

        config_dict = self.to_dict()

        # Add header comment
        header = (
            "# Issue Orchestrator Configuration\n"
            "# Generated/modified by settings page\n"
            "# See docs/user/configuration.md for full documentation\n\n"
        )

        with open(save_path, "w") as f:
            f.write(header)
            yaml.dump(config_dict, f, default_flow_style=False, sort_keys=False, allow_unicode=True)

        return save_path

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

        # Expand ${VAR} environment variable references
        data = _expand_env_vars(data)

        config = cls()
        config.config_path = config_path.resolve()

        # Extract all sections with validation
        sections = _extract_config_sections(data, config_path)
        repo_root = repo_root_from_config_path(config_path)

        # Set repo root
        config.repo_root = repo_root
        if sections["repo"].get("root"):
            config.repo_root = resolve_relative_path(sections["repo"]["root"], repo_root)
            config.repo_root_from_yaml = True

        # Store raw data for unknown field validation
        config.raw_data = data
        config.raw_agents = sections["agents"]
        config.config_strict = sections["config"].get("strict", False)
        if sections["state"].get("file"):
            config.state_file = resolve_relative_path(sections["state"]["file"], repo_root)

        # Parse default_agent section
        default_agent_section = data.get("default_agent", {})
        if default_agent_section:
            config.default_agent = DefaultAgentConfig(
                provider=default_agent_section.get("provider"),
                model=default_agent_section.get("model"),
                provider_args=default_agent_section.get("provider_args", {}),
            )

        # Load all sections using helper functions
        _load_worktrees_section(config, sections["worktrees"], repo_root, config_path)
        _load_agents_section(config, sections["agents"], repo_root)
        _load_execution_section(config, sections["execution"], config_path)
        _load_labels_section(config, sections["labels"])
        _load_repo_section(config, sections["repo"], sections["github"])
        _load_github_write_verify(config, sections["github"])
        _load_ui_section(config, sections["ui"])
        _load_observability_section(config, sections["observability"])
        _load_security_section(config, sections["security"], repo_root)
        _load_review_section(config, sections["review"])
        _load_cleanup_section(config, sections["cleanup"])
        _load_validation_section(config, sections["validation"])
        _load_retry_section(config, data)

        # Simple direct assignments
        config.e2e_pr_labels = sections["e2e"].get("pr_labels", [])
        config.filtering = _parse_filtering_config(sections["filtering"])
        config.milestone_sort = sections["milestones"].get("sort", "due_date")
        config.milestone_sort_config = sections["milestones"].get("sort_config", {})
        config.milestone_order = _parse_milestone_order(sections["milestones"].get("order", []))
        config.foundation_milestone = sections["milestones"].get("foundation", "M0")
        config.ai_systems_allowed = _parse_ai_systems_allowed(
            sections["ai_systems"].get("allowed", [])
        )


        # Parse complex optional configs
        _apply_optional_sections(config, sections)
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

        Uses modular validators from the validators subpackage for cleaner
        separation of concerns and easier testing.
        """
        from .validators import (
            AgentValidator,
            GoalPilotValidator,
            IsolationValidator,
            ReviewWorkflowValidator,
            TemplateValidator,
            UnknownFieldsValidator,
            WorktreeValidator,
        )

        errors: list[str] = []

        # Run all validators
        errors.extend(WorktreeValidator().validate(self))
        errors.extend(AgentValidator().validate(self))
        errors.extend(ReviewWorkflowValidator().validate(self))
        errors.extend(GoalPilotValidator().validate(self))
        errors.extend(IsolationValidator().validate(self))
        errors.extend(TemplateValidator().validate(self))
        errors.extend(UnknownFieldsValidator().validate(self))

        if not (0 <= self.scheduling.default_priority_tier <= 9):
            errors.append("scheduling.default_priority_tier must be between 0 and 9")

        if self.triage.priority is not None and not re.fullmatch(r"P\d", self.triage.priority.strip()):
            errors.append("triage.priority must be a tier like 'P0'..'P9'")

        return errors

    def validate_unknown_fields(self) -> list[tuple[str, str]]:
        """Check for unknown fields in the raw YAML data.

        Returns list of (field_path, level) tuples where:
        - field_path is like "repo.root" or "agents.agent:web.some_field"
        - level is "top" or "agent"
        """
        unknown = []

        # Check top-level fields
        for key in self.raw_data.keys():
            if key not in ALLOWED_TOP_LEVEL_FIELDS:
                unknown.append((key, "top"))

        # Check per-agent fields
        for agent_name, agent_data in self.raw_agents.items():
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
        # system_prompt includes agent-done instructions, built by get_command()
        VALID_COMMAND_VARS = VALID_INITIAL_PROMPT_VARS | {"initial_prompt", "system_prompt"}

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
            "pre_push_dirty_check": "tracked",
            "coverage_guardrail": {
                "enabled": False,
                "min_percent": None,
                "apply_to": "changed",
                "scope": [],
                "coverage_type": "line",
                "exclude": [],
            },
        }
    """
    config_path = find_config_file(start_path)
    if not config_path:
        return {
            "cmd": None,
            "timeout_seconds": 300,
            "pre_push_dirty_check": "tracked",
            "coverage_guardrail": {
                "enabled": False,
                "min_percent": None,
                "apply_to": "changed",
                "scope": [],
                "coverage_type": "line",
                "exclude": [],
            },
        }

    try:
        with open(config_path) as f:
            config = yaml.safe_load(f) or {}

        validation = config.get("validation", {})
        guardrail = validation.get("coverage_guardrail", {}) or {}
        return {
            "cmd": validation.get("cmd"),
            "timeout_seconds": validation.get("timeout_seconds", 300),
            "pre_push_dirty_check": validation.get("pre_push_dirty_check", "tracked"),
            "coverage_guardrail": {
                "enabled": guardrail.get("enabled", False),
                "min_percent": guardrail.get("min_percent"),
                "apply_to": guardrail.get("apply_to", "changed"),
                "scope": guardrail.get("scope", []) or [],
                "coverage_type": guardrail.get("coverage_type", "line"),
                "exclude": guardrail.get("exclude", []) or [],
            },
        }
    except Exception:
        return {
            "cmd": None,
            "timeout_seconds": 300,
            "pre_push_dirty_check": "tracked",
            "coverage_guardrail": {
                "enabled": False,
                "min_percent": None,
                "apply_to": "changed",
                "scope": [],
                "coverage_type": "line",
                "exclude": [],
            },
        }
