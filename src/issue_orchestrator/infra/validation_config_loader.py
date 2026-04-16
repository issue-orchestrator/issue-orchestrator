"""Lightweight validation config loading for hooks and completion commands."""

from pathlib import Path

import yaml

from .config_paths import DEFAULT_CONFIG_NAME, find_config_file


def default_validation_config() -> dict:
    """Return the validation defaults without loading the full config model."""
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


def extract_validation_config(config: dict) -> dict:
    """Extract the validation section from parsed YAML data."""
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


def load_validation_config(
    start_path: Path | None = None,
    config_name: str | None = None,
) -> dict:
    """Load validation configuration from the config file.

    This is for validation hooks that need only the validation config, not the
    full Config object.
    """
    selected_config_name = config_name or DEFAULT_CONFIG_NAME
    if selected_config_name and not selected_config_name.endswith(".yaml"):
        selected_config_name = f"{selected_config_name}.yaml"

    config_path = find_config_file(start_path, selected_config_name)
    if not config_path:
        if config_name:
            start_from = start_path or Path.cwd()
            raise FileNotFoundError(
                f"Configured file '{selected_config_name}' not found under "
                f"{start_from}/.issue-orchestrator/config"
            )
        return default_validation_config()

    try:
        with config_path.open() as file:
            config = yaml.safe_load(file) or {}
        return extract_validation_config(config)
    except Exception:
        return default_validation_config()


def load_validation_config_from_file(config_path: Path) -> dict:
    """Load only the validation section from an explicit config file path.

    Raises:
        FileNotFoundError: when config_path does not exist.
    """
    if not config_path.exists():
        raise FileNotFoundError(f"Configured file not found: {config_path}")

    with config_path.open() as file:
        config = yaml.safe_load(file) or {}
    return extract_validation_config(config)
