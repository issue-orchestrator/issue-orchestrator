"""Lightweight validation config loading for hooks and completion commands."""

from dataclasses import asdict
import os
from pathlib import Path

import yaml

from .config_paths import DEFAULT_CONFIG_NAME, find_config_file
from .config_models import ValidationConfig
from .env import get_env
from .validation_junit_paths import configured_validation_junit_xml_paths_from_mapping


def default_validation_config() -> dict:
    """Return the validation defaults without loading the full config model."""
    return asdict(ValidationConfig())


def extract_validation_config(config: dict) -> dict:
    """Extract the validation section from parsed YAML data."""
    defaults = default_validation_config()
    guardrail_defaults = defaults["coverage_guardrail"]
    validation = config.get("validation", {}) or {}
    quick = validation.get("quick", {}) or {}
    publish = validation.get("publish", {}) or {}
    guardrail = validation.get("coverage_guardrail", {}) or {}
    return {
        "quick": {
            "cmd": quick.get("cmd"),
            "timeout_seconds": quick.get(
                "timeout_seconds",
                defaults["quick"]["timeout_seconds"],
            ),
        },
        "publish": {
            "cmd": publish.get("cmd"),
            "timeout_seconds": publish.get(
                "timeout_seconds",
                defaults["publish"]["timeout_seconds"],
            ),
            "dirty_check": publish.get(
                "dirty_check",
                defaults["publish"]["dirty_check"],
            ),
        },
        "junit_xml_paths": configured_validation_junit_xml_paths_from_mapping(config),
        "coverage_guardrail": {
            "enabled": guardrail.get("enabled", guardrail_defaults["enabled"]),
            "min_percent": guardrail.get("min_percent", guardrail_defaults["min_percent"]),
            "apply_to": guardrail.get("apply_to", guardrail_defaults["apply_to"]),
            "scope": guardrail.get("scope", guardrail_defaults["scope"]) or [],
            "coverage_type": guardrail.get("coverage_type", guardrail_defaults["coverage_type"]),
            "exclude": guardrail.get("exclude", guardrail_defaults["exclude"]) or [],
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


def load_runtime_validation_config(
    start_path: Path | None = None,
) -> dict:
    """Load validation config honoring explicit runtime config selection.

    Precedence:
    1. ``ISSUE_ORCHESTRATOR_CONFIG_PATH`` / ``ORCHESTRATOR_CONFIG_PATH``
    2. ``ISSUE_ORCHESTRATOR_CONFIG_NAME`` / ``ORCHESTRATOR_CONFIG_NAME``
    3. repo-local ``default.yaml`` search
    """
    config_path_env = get_env("CONFIG_PATH") or os.environ.get("ORCHESTRATOR_CONFIG_PATH")
    if config_path_env:
        return load_validation_config_from_file(Path(config_path_env))

    config_name = get_env("CONFIG_NAME") or os.environ.get("ORCHESTRATOR_CONFIG_NAME")
    return load_validation_config(start_path, config_name=config_name)
