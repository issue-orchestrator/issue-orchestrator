"""Data-driven settings schema using Pydantic.

Defines a SettingsSchema model hierarchy where each field carries all its metadata
via Field() + json_schema_extra. This single source of truth drives:
- settings.html (Jinja2 renders from schema)
- GET/POST /api/settings (serialize/validate via Pydantic)
- setup_wizard.py (defaults, labels, hints)
- docs/user/configuration.md (auto-generated reference)
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any, Literal, Optional, TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from .config import Config


# ---------------------------------------------------------------------------
# Sub-models for each settings tab
# ---------------------------------------------------------------------------

class ConcurrencySettings(BaseModel):
    """Settings for the Concurrency tab."""

    max_concurrent_sessions: int = Field(
        3,
        title="Max Concurrent Sessions",
        description="Maximum parallel agent sessions",
        ge=1,
        le=20,
        json_schema_extra={
            "section": "Session Limits",
            "config_attr": "max_concurrent_sessions",
            "yaml_path": "execution.concurrency.max_concurrent_sessions",
        },
    )
    session_timeout_minutes: int = Field(
        45,
        title="Session Timeout (minutes)",
        description="Kill sessions after this duration",
        ge=5,
        le=180,
        json_schema_extra={
            "section": "Session Limits",
            "config_attr": "session_timeout_minutes",
            "yaml_path": "execution.concurrency.session_timeout_minutes",
        },
    )
    queue_refresh_seconds: int = Field(
        600,
        title="Queue Refresh Interval (seconds)",
        description="How often to refresh the issue queue from GitHub (0 = manual only)",
        ge=0,
        le=3600,
        json_schema_extra={
            "section": "Queue",
            "config_attr": "queue_refresh_seconds",
            "yaml_path": "ui.queue_refresh_seconds",
        },
    )


class E2ESettings(BaseModel):
    """Settings for the E2E Runner tab."""

    enabled: bool = Field(
        False,
        title="Enable E2E Test Runner",
        description="Automatically run E2E tests when main branch changes",
        json_schema_extra={
            "config_attr": "e2e.enabled",
            "yaml_path": "e2e.enabled",
        },
    )
    auto_run_interval_minutes: int = Field(
        30,
        title="Auto-run Interval (minutes)",
        description="Min interval between auto runs (0 = disable)",
        ge=0,
        le=1440,
        json_schema_extra={
            "config_attr": "e2e.auto_run_interval_minutes",
            "yaml_path": "e2e.auto_run_interval_minutes",
        },
    )
    role: Literal["auto", "executor", "reader", "disabled"] = Field(
        "auto",
        title="Role",
        description="Role in multi-orchestrator setup",
        json_schema_extra={
            "config_attr": "e2e.role",
            "yaml_path": "e2e.role",
        },
    )
    pytest_args: str = Field(
        "tests/e2e -v",
        title="Pytest Arguments",
        description="Space-separated pytest arguments (e.g., tests/e2e -v)",
        json_schema_extra={
            "config_attr": "e2e.pytest_args",
            "yaml_path": "e2e.pytest_args",
            "ui_transform": "space_separated_list",
        },
    )
    allow_retry_once: bool = Field(
        True,
        title="Retry failed tests once",
        description="Retry failing tests to reduce flakiness",
        json_schema_extra={
            "config_attr": "e2e.allow_retry_once",
            "yaml_path": "e2e.allow_retry_once",
        },
    )
    stop_on_first_failure: bool = Field(
        False,
        title="Stop on first failure",
        description="Add -x flag to stop test run on first failure",
        json_schema_extra={
            "config_attr": "e2e.stop_on_first_failure",
            "yaml_path": "e2e.stop_on_first_failure",
        },
    )
    quarantine_file: str = Field(
        "tests/e2e/quarantine.txt",
        title="Quarantine File",
        description="Path to quarantine file for skipping known-flaky tests",
        json_schema_extra={
            "config_attr": "e2e.quarantine_file",
            "yaml_path": "e2e.quarantine_file",
        },
    )


class FilteringSettings(BaseModel):
    """Settings for the Filtering tab."""

    label: Optional[str] = Field(
        None,
        title="Label Filter",
        description="Only process issues with this label (optional)",
        json_schema_extra={
            "config_attr": "filtering.label",
            "yaml_path": "filtering.label",
        },
    )
    milestones: str = Field(
        "",
        title="Milestones",
        description="Comma-separated list of milestones to process",
        json_schema_extra={
            "config_attr": "filtering.milestones",
            "config_read_method": "filtering.get_milestones",
            "yaml_path": "filtering.milestones",
            "ui_transform": "comma_separated_list",
        },
    )
    exclude_labels: str = Field(
        "",
        title="Exclude Labels",
        description="Comma-separated labels to exclude",
        json_schema_extra={
            "config_attr": "filtering.exclude_labels",
            "yaml_path": "filtering.exclude_labels",
            "ui_transform": "comma_separated_list",
        },
    )
    fetch_limit: int = Field(
        100,
        title="Fetch Limit",
        description="Max issues to fetch per API call",
        ge=1,
        le=500,
        json_schema_extra={
            "config_attr": "filtering.fetch_limit",
            "yaml_path": "filtering.fetch_limit",
        },
    )
    max_to_start: int = Field(
        0,
        title="Max to Start",
        description="Stop after starting N issues (0 = unlimited)",
        ge=0,
        le=100,
        json_schema_extra={
            "config_attr": "filtering.max_to_start",
            "yaml_path": "filtering.max_to_start",
        },
    )


class ReviewSettings(BaseModel):
    """Settings for the Review tab."""

    enabled: bool = Field(
        False,
        title="Enable Code Review",
        description="Enable automated code review workflow",
        json_schema_extra={
            "section": "Code Review Workflow",
            "config_attr": "review_enabled",
            "yaml_path": "review.enabled",
        },
    )
    default_reviewer: Optional[str] = Field(
        None,
        title="Default Reviewer Agent",
        description="Agent label for code reviews (e.g., agent:reviewer)",
        json_schema_extra={
            "section": "Code Review Workflow",
            "config_attr": "code_review_agent",
            "yaml_path": "review.default",
        },
    )
    max_rework_cycles: int = Field(
        2,
        title="Max Rework Cycles",
        description="Max times to re-queue work agent before escalating",
        ge=0,
        le=10,
        json_schema_extra={
            "section": "Code Review Workflow",
            "config_attr": "max_rework_cycles",
            "yaml_path": "review.max_rework_cycles",
        },
    )
    triage_agent: Optional[str] = Field(
        None,
        title="Triage Review Agent",
        description="Agent for batch reviews (optional)",
        json_schema_extra={
            "section": "Triage Review",
            "config_attr": "triage_review_agent",
            "yaml_path": "review.triage_review_agent",
        },
    )
    triage_threshold: int = Field(
        0,
        title="Triage Threshold",
        description="Trigger triage after N PRs (0 = manual only)",
        ge=0,
        le=50,
        json_schema_extra={
            "section": "Triage Review",
            "config_attr": "triage_review_threshold",
            "yaml_path": "review.triage_review_threshold",
        },
    )


class AdvancedSettings(BaseModel):
    """Settings for the Advanced tab."""

    session_no_output_seconds: int = Field(
        120,
        title="No-Output Threshold (seconds)",
        description="Emit event after this much idle time",
        ge=30,
        le=600,
        json_schema_extra={
            "section": "Observability",
            "config_attr": "session_no_output_seconds",
            "yaml_path": "observability.session_no_output_seconds",
        },
    )
    stale_escalation_ticks: int = Field(
        0,
        title="Stale Escalation Ticks",
        description="Escalate after K consecutive stale ticks (0 = disabled)",
        ge=0,
        le=20,
        json_schema_extra={
            "section": "Observability",
            "config_attr": "stale_escalation_ticks",
            "yaml_path": "observability.stale_escalation_ticks",
        },
    )
    web_port: int = Field(
        8080,
        title="Web Dashboard Port",
        ge=1024,
        le=65535,
        json_schema_extra={
            "section": "Ports",
            "restart_required": True,
            "config_attr": "web_port",
            "yaml_path": "ui.web_port",
        },
    )
    control_api_port: int = Field(
        19080,
        title="Control API Port",
        description="0 = disabled",
        ge=0,
        le=65535,
        json_schema_extra={
            "section": "Ports",
            "restart_required": True,
            "config_attr": "control_api_port",
            "yaml_path": "ui.control_api_port",
        },
    )
    worktree_base: str = Field(
        "../",
        title="Worktree Base Directory",
        description="Directory where git worktrees are created",
        json_schema_extra={
            "section": "Worktrees",
            "restart_required": True,
            "config_attr": "worktree_base",
            "yaml_path": "worktrees.base",
        },
    )
    worktree_branch_on_recreate: Literal["delete", "create_new_branch"] = Field(
        "delete",
        title="Branch on Recreate",
        description="What to do when recreating a worktree with existing branch",
        json_schema_extra={
            "section": "Worktrees",
            "restart_required": True,
            "config_attr": "worktree_branch_on_recreate",
            "yaml_path": "worktrees.worktree_branch_on_recreate",
        },
    )


# ---------------------------------------------------------------------------
# Tab definitions (ordered as they appear in the UI)
# ---------------------------------------------------------------------------

TAB_DEFINITIONS: list[dict[str, Any]] = [
    {"key": "concurrency", "label": "Concurrency", "model": ConcurrencySettings},
    {"key": "e2e", "label": "E2E Runner", "model": E2ESettings},
    {"key": "filtering", "label": "Filtering", "model": FilteringSettings},
    {"key": "review", "label": "Review", "model": ReviewSettings},
    {"key": "advanced", "label": "Advanced", "model": AdvancedSettings},
]


# ---------------------------------------------------------------------------
# Config <-> Schema bridge
# ---------------------------------------------------------------------------

def _get_nested_attr(obj: Any, path: str) -> Any:
    """Get obj.a.b.c from dotted path 'a.b.c'."""
    for part in path.split("."):
        obj = getattr(obj, part)
    return obj


def _set_nested_attr(obj: Any, path: str, value: Any) -> None:
    """Set obj.a.b.c = value from dotted path 'a.b.c'."""
    parts = path.split(".")
    for part in parts[:-1]:
        obj = getattr(obj, part)
    setattr(obj, parts[-1], value)


def from_config(config: Config) -> dict[str, BaseModel]:
    """Build all tab models from a Config object.

    Returns a dict mapping tab key -> Pydantic model instance with current values.
    """
    result: dict[str, BaseModel] = {}
    for tab in TAB_DEFINITIONS:
        model_cls = tab["model"]
        values: dict[str, Any] = {}
        for field_name, field_info in model_cls.model_fields.items():
            extra = field_info.json_schema_extra
            assert isinstance(extra, dict), f"Missing json_schema_extra on {field_name}"

            # Use config_read_method if specified (e.g., "filtering.get_milestones"),
            # otherwise fall back to config_attr for simple attribute reads.
            read_method = extra.get("config_read_method")
            if read_method:
                raw = _get_nested_attr(config, read_method)()
            else:
                config_attr = extra["config_attr"]
                raw = _get_nested_attr(config, config_attr)

            # Handle UI transforms (list -> string for display)
            transform = extra.get("ui_transform")
            if transform == "comma_separated_list":
                raw = ", ".join(raw) if raw else ""
            elif transform == "space_separated_list":
                raw = " ".join(raw) if raw else ""
            elif isinstance(raw, Path):
                raw = str(raw)

            values[field_name] = raw
        result[tab["key"]] = model_cls(**values)
    return result


def apply_to(tabs: dict[str, BaseModel], config: Config) -> bool:
    """Apply all tab models to a Config object.

    Returns True if any field marked restart_required changed.
    """
    restart = False
    for tab in TAB_DEFINITIONS:
        model = tabs.get(tab["key"])
        if model is None:
            continue
        model_cls = tab["model"]
        for field_name, field_info in model_cls.model_fields.items():
            extra = field_info.json_schema_extra
            assert isinstance(extra, dict), f"Missing json_schema_extra on {field_name}"
            config_attr = extra["config_attr"]
            value = getattr(model, field_name)

            # Handle transforms (string -> list for storage)
            transform = extra.get("ui_transform")
            if transform == "comma_separated_list":
                value = [s.strip() for s in value.split(",") if s.strip()] if value else []
            elif transform == "space_separated_list":
                value = value.split() if value else []

            # Check restart requirement before applying
            if extra.get("restart_required"):
                old = _get_nested_attr(config, config_attr)
                if str(old) != str(value):
                    restart = True

            _set_nested_attr(config, config_attr, value)
    return restart


def get_restart_fields() -> set[str]:
    """Return field names that require restart when changed."""
    fields: set[str] = set()
    for tab in TAB_DEFINITIONS:
        model_cls = tab["model"]
        for field_name, field_info in model_cls.model_fields.items():
            extra = field_info.json_schema_extra
            if isinstance(extra, dict) and extra.get("restart_required"):
                fields.add(field_name)
    return fields


# ---------------------------------------------------------------------------
# JSON Schema generation (cached for template rendering)
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=1)
def get_settings_json_schema() -> dict[str, Any]:
    """Generate per-tab JSON schemas for template rendering.

    Returns a dict mapping tab key -> JSON schema dict.
    The schema includes x_extra with section, restart_required, etc.
    """
    schemas: dict[str, Any] = {}
    for tab in TAB_DEFINITIONS:
        model_cls = tab["model"]
        schema = model_cls.model_json_schema()

        # Pydantic v2 puts json_schema_extra into the property dict.
        # We normalize it into an x_extra key for template access.
        for prop_name, prop in schema.get("properties", {}).items():
            field_info = model_cls.model_fields[prop_name]
            extra = field_info.json_schema_extra
            if isinstance(extra, dict):
                prop["x_extra"] = extra

        schemas[tab["key"]] = schema
    return schemas


# ---------------------------------------------------------------------------
# Metadata accessor for wizard / docs
# ---------------------------------------------------------------------------

def get_field_meta(tab_key: str, field_name: str) -> dict[str, Any]:
    """Get schema metadata for a specific field.

    Returns dict with 'title', 'description', 'default', and any json_schema_extra.
    """
    for tab in TAB_DEFINITIONS:
        if tab["key"] == tab_key:
            field_info = tab["model"].model_fields[field_name]
            extra = field_info.json_schema_extra or {}
            return {
                "title": field_info.title,
                "description": field_info.description,
                "default": field_info.default,
                **extra,
            }
    raise KeyError(f"Unknown tab '{tab_key}' or field '{field_name}'")


# ---------------------------------------------------------------------------
# Documentation generation
# ---------------------------------------------------------------------------

def generate_config_reference() -> str:
    """Generate markdown configuration reference from schema.

    Returns a markdown string with tables for each tab.
    """
    lines = ["# Settings Reference", "", "_Auto-generated from settings schema._", ""]
    for tab in TAB_DEFINITIONS:
        lines.append(f"## {tab['label']}")
        lines.append("")
        lines.append("| Field | Type | Default | Description |")
        lines.append("|-------|------|---------|-------------|")
        model_cls = tab["model"]
        schema = model_cls.model_json_schema()
        for prop_name, prop in schema.get("properties", {}).items():
            field_info = model_cls.model_fields[prop_name]
            extra = field_info.json_schema_extra
            yaml_path = extra.get("yaml_path", prop_name) if isinstance(extra, dict) else prop_name
            type_str = prop.get("type", "string")
            # Handle anyOf (Optional types)
            if "anyOf" in prop:
                types = [t.get("type", "null") for t in prop["anyOf"]]
                type_str = " | ".join(t for t in types if t != "null")
                if "null" in types:
                    type_str += " (optional)"
            default = prop.get("default", "")
            desc = prop.get("description", "")
            lines.append(f"| `{yaml_path}` | {type_str} | `{default}` | {desc} |")
        lines.append("")
    return "\n".join(lines)
