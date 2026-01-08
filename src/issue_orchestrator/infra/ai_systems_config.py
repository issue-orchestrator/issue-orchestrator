"""AI Systems configuration loader.

Loads AI system definitions from:
1. Built-in defaults (bundled with package)
2. User overrides (~/.issue-orchestrator/ai-systems.yaml)
3. Project overrides (.issue-orchestrator/ai-systems.yaml)

Later files override earlier ones, allowing customization.
"""

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


@dataclass
class AISystemConfig:
    """Configuration for a single AI system."""
    name: str
    description: str = ""
    log_pattern: str = ""
    log_format: str = "text"  # jsonl, json, markdown, text
    console_tags: list[str] = field(default_factory=list)
    error_patterns: list[str] = field(default_factory=list)
    completion_marker: str | None = None


@dataclass
class AISystemsConfig:
    """Complete AI systems configuration."""
    systems: dict[str, AISystemConfig] = field(default_factory=dict)
    default_ai_system: str = "claude-code"

    def get_system(self, name: str) -> AISystemConfig | None:
        """Get an AI system config by name."""
        return self.systems.get(name)

    def detect_from_tags(self, text: str) -> str | None:
        """Detect AI system from console output using tags."""
        if not text:
            return None
        text_lower = text.lower()
        for name, config in self.systems.items():
            for tag in config.console_tags:
                if tag.lower() in text_lower:
                    return name
        return None

    def detect_from_command(self, command: str) -> str | None:
        """Detect AI system from command prefix."""
        if not command:
            return None
        # Skip env var assignments at the start
        parts = command.strip().split()
        cmd_start = None
        for part in parts:
            if "=" not in part:
                cmd_start = part.lower()
                break
        if not cmd_start:
            return None
        # Check known prefixes
        for name, config in self.systems.items():
            # Match command prefix against name or first console tag
            if cmd_start == name or cmd_start == name.replace("-", ""):
                return name
            # Also check first console tag (e.g., "claude" matches "claude-code")
            if config.console_tags and cmd_start == config.console_tags[0].lower():
                return name
        return None

    def resolve_log_pattern(
        self,
        pattern: str,
        worktree: Path,
        issue_number: int | None = None,
    ) -> str:
        """Resolve variables in a log pattern.

        Variables:
            {home} - User home directory
            {worktree} - Session worktree path
            {escaped_worktree} - Worktree with / replaced by -
            {project_hash} - MD5 hash of worktree (for Gemini)
            {date_path} - Current date as YYYY/MM/DD
            {issue_number} - Issue number
        """
        home = str(Path.home())
        worktree_str = str(worktree.resolve())
        escaped = worktree_str.lstrip("/").replace("/", "-")
        project_hash = hashlib.md5(worktree_str.encode()).hexdigest()[:12]
        now = datetime.now()
        date_path = now.strftime("%Y/%m/%d")

        result = pattern.replace("{home}", home)
        result = result.replace("{worktree}", worktree_str)
        result = result.replace("{escaped_worktree}", escaped)
        result = result.replace("{project_hash}", project_hash)
        result = result.replace("{date_path}", date_path)
        if issue_number is not None:
            result = result.replace("{issue_number}", str(issue_number))
        return result


def _load_yaml_file(path: Path) -> dict[str, Any]:
    """Load a YAML file, returning empty dict if not found or invalid."""
    if not path.exists():
        return {}
    try:
        with open(path, "r") as f:
            data = yaml.safe_load(f)
            return data if isinstance(data, dict) else {}
    except Exception as e:
        logger.warning("Failed to load %s: %s", path, e)
        return {}


def _merge_configs(base: dict, override: dict) -> dict:
    """Deep merge override into base."""
    result = base.copy()
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _merge_configs(result[key], value)
        else:
            result[key] = value
    return result


def _parse_system_config(name: str, data: dict[str, Any]) -> AISystemConfig:
    """Parse a single AI system config from YAML data."""
    return AISystemConfig(
        name=name,
        description=data.get("description", ""),
        log_pattern=data.get("log_pattern", ""),
        log_format=data.get("log_format", "text"),
        console_tags=data.get("console_tags", []),
        error_patterns=data.get("error_patterns", []),
        completion_marker=data.get("completion_marker"),
    )


def load_ai_systems_config(project_root: Path | None = None) -> AISystemsConfig:
    """Load AI systems configuration with layered overrides.

    Loading order (later overrides earlier):
    1. Built-in defaults from package
    2. User-level: ~/.issue-orchestrator/ai-systems.yaml
    3. Project-level: {project_root}/.issue-orchestrator/ai-systems.yaml

    Args:
        project_root: Project root directory for project-level config

    Returns:
        Merged AISystemsConfig
    """
    # 1. Load built-in defaults
    default_path = Path(__file__).parent.parent / "config" / "ai_systems.yaml"
    merged = _load_yaml_file(default_path)

    # 2. Load user-level overrides
    user_path = Path.home() / ".issue-orchestrator" / "ai-systems.yaml"
    user_config = _load_yaml_file(user_path)
    if user_config:
        logger.debug("Loaded user AI systems config from %s", user_path)
        merged = _merge_configs(merged, user_config)

    # 3. Load project-level overrides
    if project_root:
        project_path = project_root / ".issue-orchestrator" / "ai-systems.yaml"
        project_config = _load_yaml_file(project_path)
        if project_config:
            logger.debug("Loaded project AI systems config from %s", project_path)
            merged = _merge_configs(merged, project_config)

    # Parse into dataclass
    config = AISystemsConfig(
        default_ai_system=merged.get("default_ai_system", "claude-code"),
    )

    # Parse each AI system
    for name, system_data in merged.get("ai_systems", {}).items():
        if isinstance(system_data, dict):
            config.systems[name] = _parse_system_config(name, system_data)

    logger.debug("Loaded %d AI system configs", len(config.systems))
    return config


# Global cached config (loaded once at startup)
_cached_config: AISystemsConfig | None = None


def get_ai_systems_config(project_root: Path | None = None) -> AISystemsConfig:
    """Get the AI systems config, loading and caching if needed."""
    global _cached_config
    if _cached_config is None:
        _cached_config = load_ai_systems_config(project_root)
    return _cached_config


def clear_ai_systems_cache() -> None:
    """Clear the cached config (for testing)."""
    global _cached_config
    _cached_config = None
