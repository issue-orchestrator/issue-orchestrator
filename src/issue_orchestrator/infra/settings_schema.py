"""Data-driven settings schema using Pydantic.

Defines a SettingsSchema model hierarchy where each field carries all its metadata
via Field() + json_schema_extra. This single source of truth drives:
- settings.html (Jinja2 renders from schema)
- GET/POST /api/settings (serialize/validate via Pydantic)
- setup_wizard.py (defaults, labels, hints)
    - docs/user/configuration_reference.md (auto-generated reference)
- doctor checks (path validation, agent references, status summaries)
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any, Literal, Optional, TYPE_CHECKING

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from .config import Config


# ---------------------------------------------------------------------------
# Doctor check type constants — used in json_schema_extra["doctor_check"]
# ---------------------------------------------------------------------------

DOCTOR_CHECK_PATH_EXISTS = "path_exists"
DOCTOR_CHECK_FIRST_ARG_PATH_EXISTS = "first_arg_path_exists"
DOCTOR_CHECK_REFERENCES_AGENT = "references_agent"

# Doctor severity levels
DOCTOR_SEVERITY_ERROR = "error"
DOCTOR_SEVERITY_WARNING = "warning"

# Summary format constants — used in json_schema_extra["summary"]
SUMMARY_ENABLED_FLAG = "enabled_flag"
SUMMARY_KEY_VALUE = "key_value"
SUMMARY_INTERVAL = "interval"
SUMMARY_BOOLEAN_FLAG = "boolean_flag"


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
            "doc_examples": ["1", "3", "5"],
            "doc_notes": "Set based on CPU, RAM, and how many concurrent sessions you can actively review.",
            "section": "Session Limits",
            "config_attr": "max_concurrent_sessions",
            "yaml_path": "execution.concurrency.max_concurrent_sessions",
            "setup": {
                "enabled": True,
                "section": "concurrency",
                "order": 10,
            },
        },
    )
    session_timeout_minutes: int = Field(
        45,
        title="Session Timeout (minutes)",
        description="Kill sessions after this duration",
        ge=5,
        le=180,
        json_schema_extra={
            "doc_examples": ["30", "45", "90"],
            "doc_notes": "Lower values fail faster for stuck sessions; higher values help long builds.",
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
            "doc_examples": ["0", "300", "600"],
            "doc_notes": "Use 0 to disable automatic refreshes and refresh manually in the UI.",
            "section": "Queue",
            "config_attr": "queue_refresh_seconds",
            "yaml_path": "ui.queue_refresh_seconds",
        },
    )
    fetch_layer_enabled: bool = Field(
        True,
        title="Fetch-Layer Optimization",
        description="Enable incremental refreshes between periodic full scans",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": "Disable to force a full GitHub queue scan on every refresh.",
            "section": "Queue",
            "config_attr": "fetch_layer_enabled",
            "yaml_path": "ui.fetch_layer.enabled",
        },
    )
    fetch_layer_network_sync_seconds: int = Field(
        60,
        title="Network Sync Interval (seconds)",
        description="How often to run GitHub network sync cycles (independent of control tick)",
        ge=5,
        le=3600,
        json_schema_extra={
            "doc_examples": ["15", "60", "120"],
            "doc_notes": "Lower values improve freshness; higher values reduce GitHub API calls.",
            "section": "Queue",
            "config_attr": "fetch_layer_network_sync_seconds",
            "yaml_path": "ui.fetch_layer.network_sync_seconds",
        },
    )
    fetch_layer_full_scan_interval_seconds: int = Field(
        1800,
        title="Full Scan Interval (seconds)",
        description="Run a full queue scan at this interval even when incremental mode is enabled",
        ge=60,
        le=86400,
        json_schema_extra={
            "doc_examples": ["600", "1800", "3600"],
            "doc_notes": "Lower values discover new work faster; higher values reduce API usage.",
            "section": "Queue",
            "config_attr": "fetch_layer_full_scan_interval_seconds",
            "yaml_path": "ui.fetch_layer.full_scan_interval_seconds",
        },
    )
    fetch_layer_discovery_limit: int = Field(
        25,
        title="Discovery Limit",
        description="Max issues fetched per incremental discovery pass",
        ge=0,
        le=200,
        json_schema_extra={
            "doc_examples": ["0", "25", "50"],
            "doc_notes": "Set to 0 to disable discovery during incremental refreshes.",
            "section": "Queue",
            "config_attr": "fetch_layer_discovery_limit",
            "yaml_path": "ui.fetch_layer.discovery_limit",
        },
    )
    fetch_layer_max_hot_issues_per_cycle: int = Field(
        40,
        title="Hot Issue Refresh Limit",
        description="Max existing queue issues to refresh by direct issue lookup per cycle",
        ge=0,
        le=500,
        json_schema_extra={
            "doc_examples": ["20", "40", "100"],
            "doc_notes": "Higher values improve freshness but increase API usage.",
            "section": "Queue",
            "config_attr": "fetch_layer_max_hot_issues_per_cycle",
            "yaml_path": "ui.fetch_layer.max_hot_issues_per_cycle",
        },
    )
    fetch_layer_pr_scan_every_n_refreshes: int = Field(
        2,
        title="PR Scan Cadence",
        description="Scan review/rework PRs every N queue refreshes",
        ge=1,
        le=20,
        json_schema_extra={
            "doc_examples": ["1", "2", "3"],
            "doc_notes": "Use 1 for max freshness; increase to reduce PR API calls.",
            "section": "Queue",
            "config_attr": "fetch_layer_pr_scan_every_n_refreshes",
            "yaml_path": "ui.fetch_layer.pr_scan_every_n_refreshes",
        },
    )
    fetch_layer_dependency_scan_every_n_refreshes: int = Field(
        1,
        title="Dependency Scan Cadence",
        description="Recompute dependency blocking every N queue refreshes",
        ge=1,
        le=20,
        json_schema_extra={
            "doc_examples": ["1", "2", "3"],
            "doc_notes": "Use 1 for immediate dependency updates; increase to reduce load.",
            "section": "Queue",
            "config_attr": "fetch_layer_dependency_scan_every_n_refreshes",
            "yaml_path": "ui.fetch_layer.dependency_scan_every_n_refreshes",
        },
    )
    fetch_layer_visibility_aware_enabled: bool = Field(
        False,
        title="Visibility-Aware Refresh",
        description="Prioritize refresh for issues currently visible in the Flow board",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": "Requires browser visibility hints from the Flow board.",
            "section": "Queue",
            "config_attr": "fetch_layer_visibility_aware_enabled",
            "yaml_path": "ui.fetch_layer.visibility_aware_enabled",
        },
    )
    fetch_layer_selective_sync_planner_enabled: bool = Field(
        False,
        title="Selective Sync Planner",
        description="Enable cross-entity selective sync planning for queue refresh cycles",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": "Use with telemetry to tune freshness versus API cost.",
            "section": "Queue",
            "config_attr": "fetch_layer_selective_sync_planner_enabled",
            "yaml_path": "ui.fetch_layer.selective_sync_planner_enabled",
        },
    )
    default_priority_tier: int = Field(
        1,
        title="Default Priority Tier",
        description="Default priority tier when none is specified (0-9)",
        ge=0,
        le=9,
        json_schema_extra={
            "doc_examples": ["0", "1", "2"],
            "doc_notes": "Used when issue titles do not include a [P?-nnn] prefix.",
            "section": "Scheduling",
            "config_attr": "scheduling.default_priority_tier",
            "yaml_path": "scheduling.default_priority_tier",
        },
    )


class E2ESettings(BaseModel):
    """Settings for the E2E Runner tab."""

    enabled: bool = Field(
        False,
        title="Enable E2E Test Runner",
        description="Automatically run E2E tests when main branch changes",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": "Keep disabled on repos without stable E2E tests.",
            "config_attr": "e2e.enabled",
            "yaml_path": "e2e.enabled",
            "summary": {
                "section": "e2e",
                "format": SUMMARY_ENABLED_FLAG,
                "label": "E2E Runner",
            },
        },
    )
    auto_run_interval_minutes: int = Field(
        30,
        title="Auto-run Interval (minutes)",
        description="Min interval between auto runs (0 = disable)",
        ge=0,
        le=1440,
        json_schema_extra={
            "doc_examples": ["0", "30", "60"],
            "doc_notes": "Set to 0 to disable automatic runs and trigger manually.",
            "config_attr": "e2e.auto_run_interval_minutes",
            "yaml_path": "e2e.auto_run_interval_minutes",
            "summary": {
                "section": "e2e",
                "format": SUMMARY_INTERVAL,
                "label": "auto",
                "zero_label": "manual",
                "unit": "m",
            },
        },
    )
    role: Literal["auto", "executor", "reader", "disabled"] = Field(
        "auto",
        title="Role",
        description="Role in multi-orchestrator setup",
        json_schema_extra={
            "doc_examples": ["auto", "executor", "reader", "disabled"],
            "doc_notes": "Use executor on the single machine that should run tests.",
            "config_attr": "e2e.role",
            "yaml_path": "e2e.role",
        },
    )
    pytest_args: str = Field(
        "tests/e2e -v",
        title="Pytest Arguments",
        description="Space-separated pytest arguments (e.g., tests/e2e -v)",
        json_schema_extra={
            "doc_examples": ["tests/e2e -v", "tests/e2e -v -x"],
            "doc_notes": "First argument should be a path; it is validated by the doctor.",
            "config_attr": "e2e.pytest_args",
            "yaml_path": "e2e.pytest_args",
            "ui_transform": "space_separated_list",
            "doctor_check": DOCTOR_CHECK_FIRST_ARG_PATH_EXISTS,
            "doctor_check_condition": "e2e.enabled",
            "doctor_severity": DOCTOR_SEVERITY_WARNING,
            "summary": {
                "section": "e2e",
                "format": SUMMARY_KEY_VALUE,
                "label": "tests",
                "value_index": 0,
            },
        },
    )
    allow_retry_once: bool = Field(
        True,
        title="Retry failed tests once",
        description="Retry failing tests to reduce flakiness",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": "Disable if reruns hide real failures or are too slow.",
            "config_attr": "e2e.allow_retry_once",
            "yaml_path": "e2e.allow_retry_once",
            "summary": {
                "section": "e2e",
                "format": SUMMARY_BOOLEAN_FLAG,
                "label": "retry",
                "true_value": "on",
                "false_value": "off",
            },
        },
    )
    stop_on_first_failure: bool = Field(
        False,
        title="Stop on first failure",
        description="Add -x flag to stop test run on first failure",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": "Enable for faster feedback when most tests pass.",
            "config_attr": "e2e.stop_on_first_failure",
            "yaml_path": "e2e.stop_on_first_failure",
        },
    )
    quarantine_file: str = Field(
        "tests/e2e/quarantine.txt",
        title="Quarantine File",
        description="Path to quarantine file for skipping known-flaky tests",
        json_schema_extra={
            "doc_examples": ["tests/e2e/quarantine.txt", "tests/e2e/quarantine-local.txt"],
            "doc_notes": "Doctor verifies the file exists when E2E is enabled.",
            "config_attr": "e2e.quarantine_file",
            "yaml_path": "e2e.quarantine_file",
            "doctor_check": DOCTOR_CHECK_PATH_EXISTS,
            "doctor_check_condition": "e2e.enabled",
            "doctor_severity": DOCTOR_SEVERITY_WARNING,
        },
    )
    auto_quarantine: bool = Field(
        True,
        title="Auto-quarantine failing tests",
        description="Automatically add failing tests to the quarantine list",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": "Set false to require manual quarantine updates.",
            "config_attr": "e2e.auto_quarantine",
            "yaml_path": "e2e.auto_quarantine",
            "summary": {
                "section": "e2e",
                "format": SUMMARY_BOOLEAN_FLAG,
                "label": "quarantine",
                "true_value": "auto",
                "false_value": "manual",
            },
        },
    )
    auto_create_issues: bool = Field(
        True,
        title="Auto-create failure issues",
        description="Automatically create GitHub issues for failed tests",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": "Disable if you prefer manual triage of failures.",
            "config_attr": "e2e.auto_create_issues",
            "yaml_path": "e2e.auto_create_issues",
            "summary": {
                "section": "e2e",
                "format": SUMMARY_BOOLEAN_FLAG,
                "label": "issues",
                "true_value": "auto",
                "false_value": "manual",
            },
        },
    )
    issue_agent_label: str = Field(
        "agent:backend",
        title="Failure issue agent label",
        description="Agent label assigned to auto-created failure issues",
        json_schema_extra={
            "doc_examples": ["agent:backend", "agent:triage"],
            "doc_notes": "Must refer to an agent defined in the config.",
            "config_attr": "e2e.issue_agent_label",
            "yaml_path": "e2e.issue_agent_label",
            "doctor_check": DOCTOR_CHECK_REFERENCES_AGENT,
            "doctor_check_condition": "e2e.auto_create_issues",
            "doctor_severity": DOCTOR_SEVERITY_WARNING,
        },
    )


class FilteringSettings(BaseModel):
    """Settings for the Filtering tab."""

    label: Optional[str] = Field(
        None,
        title="Label Filter",
        description="Only process issues with this label (optional)",
        json_schema_extra={
            "doc_examples": ["bot-ready", "needs-triage"],
            "doc_notes": "Use a single label to gate which issues are eligible.",
            "config_attr": "filtering.label",
            "yaml_path": "filtering.label",
        },
    )
    milestones: str = Field(
        "",
        title="Milestones",
        description="Milestones to process (comma-separated string or YAML list)",
        json_schema_extra={
            "doc_examples": ["M1, M2", "[\"M1\", \"M2\"]", ""],
            "doc_notes": "Accepts a comma-separated string or a YAML list. Leave empty to allow all milestones.",
            "config_attr": "filtering.milestones",
            "config_read_method": "filtering.get_milestones",
            "yaml_path": "filtering.milestones",
            "ui_transform": "comma_separated_list",
        },
    )
    exclude_labels: str = Field(
        "",
        title="Exclude Labels",
        description="Labels to exclude (comma-separated string or YAML list)",
        json_schema_extra={
            "doc_examples": ["test-data, skip", "[\"test-data\", \"skip\"]", ""],
            "doc_notes": "Accepts a comma-separated string or a YAML list.",
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
            "doc_examples": ["50", "100", "200"],
            "doc_notes": "Lower values reduce API load; higher values reduce pagination.",
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
            "doc_examples": ["0", "5", "10"],
            "doc_notes": "Useful for dry runs or throttling initial ramp-up.",
            "config_attr": "filtering.max_to_start",
            "yaml_path": "filtering.max_to_start",
        },
    )


class MilestonesSettings(BaseModel):
    """Settings for the Milestones tab."""

    order: str = Field(
        "",
        title="Milestone Order",
        description=(
            "Explicit ordered list of milestone titles. Does not filter; "
            "unlisted milestones are appended using the milestone sort strategy."
        ),
        json_schema_extra={
            "doc_examples": ["M1, M2", ""],
            "doc_notes": "Use to override the default sort order without filtering.",
            "section": "Ordering",
            "config_attr": "milestone_order",
            "yaml_path": "milestones.order",
            "ui_transform": "comma_separated_list",
        },
    )


class ReviewSettings(BaseModel):
    """Settings for the Review tab."""

    enabled: bool = Field(
        False,
        title="Enable Code Review",
        description="Enable automated code review workflow",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": "When enabled, a reviewer agent validates work agent PRs.",
            "section": "Code Review Workflow",
            "config_attr": "review_enabled",
            "yaml_path": "review.enabled",
            "summary": {
                "section": "review",
                "format": SUMMARY_ENABLED_FLAG,
                "label": "Code Review",
            },
        },
    )
    default_reviewer: Optional[str] = Field(
        None,
        title="Default Reviewer Agent",
        description="Agent label for code reviews (e.g., agent:reviewer)",
        json_schema_extra={
            "doc_examples": ["agent:reviewer"],
            "doc_notes": "Must match a label defined under agents.",
            "section": "Code Review Workflow",
            "config_attr": "code_review_agent",
            "yaml_path": "review.default",
            "doctor_check": DOCTOR_CHECK_REFERENCES_AGENT,
            "doctor_check_condition": "review_enabled",
            "doctor_severity": DOCTOR_SEVERITY_ERROR,
            "summary": {
                "section": "review",
                "format": SUMMARY_KEY_VALUE,
                "label": "default",
            },
        },
    )
    max_rework_cycles: int = Field(
        10,
        title="Max Rework Cycles",
        description="Max times to re-queue work agent before escalating",
        ge=0,
        le=10,
        json_schema_extra={
            "doc_examples": ["0", "2", "10"],
            "doc_notes": "Set to 0 to disable rework cycles (immediate escalation).",
            "section": "Code Review Workflow",
            "config_attr": "max_rework_cycles",
            "yaml_path": "review.max_rework_cycles",
        },
    )
    keep_current_approach_label: str = Field(
        "reviewer-keep-current-approach",
        title="Keep Current Approach Label",
        description="Label that tells reviewer to avoid alternative approaches",
        json_schema_extra={
            "doc_examples": ["reviewer-keep-current-approach"],
            "doc_notes": "Applied to issues where stability is preferred over refactors.",
            "section": "Code Review Workflow",
            "config_attr": "review_keep_current_approach_label",
            "yaml_path": "review.keep_current_approach_label",
        },
    )
    exchange_mode: Literal["via-draft-pr", "via-mcp", "via-local-loop", "auto"] = Field(
        "via-local-loop",
        title="Review Exchange Mode",
        description="Review exchange mode (via-mcp loop, local loop, or via-draft-pr review)",
        json_schema_extra={
            "doc_examples": ["via-local-loop", "via-draft-pr", "via-mcp", "auto"],
            "doc_notes": "Local loop is the default; use via-draft-pr for GitHub-mediated review cycles.",
            "section": "Code Review Workflow",
            "config_attr": "review_exchange_mode",
            "yaml_path": "review.exchange.mode",
        },
    )
    exchange_probe_schedule: Literal["startup", "daily", "interval", "manual"] = Field(
        "daily",
        title="Exchange Probe Schedule",
        description="When to run MCP round-trip validation",
        json_schema_extra={
            "doc_examples": ["daily", "startup", "interval", "manual"],
            "doc_notes": "Use manual to disable automatic probes and run on demand.",
            "section": "Code Review Workflow",
            "config_attr": "review_exchange_probe_schedule",
            "yaml_path": "review.exchange.probe.schedule",
        },
    )
    exchange_probe_interval_days: int = Field(
        1,
        title="Exchange Probe Interval (days)",
        description="Interval for MCP round-trip validation when schedule=interval",
        ge=1,
        le=30,
        json_schema_extra={
            "doc_examples": ["1", "7", "14"],
            "doc_notes": "Used only when schedule=interval.",
            "section": "Code Review Workflow",
            "config_attr": "review_exchange_probe_interval_days",
            "yaml_path": "review.exchange.probe.interval_days",
        },
    )
    exchange_max_rounds: int = Field(
        10,
        title="Exchange Max Rounds",
        description="Max coder/reviewer rounds before stopping the MCP loop",
        ge=1,
        le=50,
        json_schema_extra={
            "doc_examples": ["5", "10", "20"],
            "doc_notes": "Higher values allow longer back-and-forth reviews.",
            "section": "Code Review Workflow",
            "config_attr": "review_exchange_max_rounds",
            "yaml_path": "review.exchange.loop.max_rounds",
        },
    )
    exchange_max_no_progress: int = Field(
        2,
        title="Exchange Max No-Progress",
        description="Max rounds where reviewer reports no progress before stopping",
        ge=1,
        le=10,
        json_schema_extra={
            "doc_examples": ["1", "2", "3"],
            "doc_notes": "Limits loops when reviewer is not seeing improvements.",
            "section": "Code Review Workflow",
            "config_attr": "review_exchange_max_no_progress",
            "yaml_path": "review.exchange.loop.max_no_progress",
        },
    )
    exchange_require_validation: bool = Field(
        True,
        title="Exchange Requires Validation",
        description="Require a validation record before reviewer can approve",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": "Disable only if you accept reviewer approvals without validation.",
            "section": "Code Review Workflow",
            "config_attr": "review_exchange_require_validation",
            "yaml_path": "review.exchange.loop.require_validation",
        },
    )
    triage_agent: Optional[str] = Field(
        None,
        title="Triage Review Agent",
        description="Agent for batch reviews (optional)",
        json_schema_extra={
            "doc_examples": ["agent:triage"],
            "doc_notes": "Must match a label defined under agents.",
            "section": "Triage Review",
            "config_attr": "triage_review_agent",
            "yaml_path": "review.triage_review_agent",
            "doctor_check": DOCTOR_CHECK_REFERENCES_AGENT,
            "doctor_check_condition": "review_enabled",
            "doctor_severity": DOCTOR_SEVERITY_ERROR,
        },
    )
    triage_threshold: int = Field(
        0,
        title="Triage Threshold",
        description="Trigger triage after N PRs (0 = manual only)",
        ge=0,
        le=50,
        json_schema_extra={
            "doc_examples": ["0", "5", "10"],
            "doc_notes": "Set to 0 to only trigger triage manually.",
            "section": "Triage Review",
            "config_attr": "triage_review_threshold",
            "yaml_path": "review.triage_review_threshold",
        },
    )


class GoalPilotSettings(BaseModel):
    """Settings for the Goal Pilot tab."""

    enabled: bool = Field(
        False,
        title="Enable Goal Pilot",
        description="Enable the Goal Pilot AI controller",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": "Enable only when Goal Pilot prompts are configured and tested.",
            "section": "Goal Pilot",
            "config_attr": "goal_pilot.enabled",
            "yaml_path": "goal_pilot.enabled",
            "summary": {
                "section": "goal_pilot",
                "format": SUMMARY_ENABLED_FLAG,
                "label": "Goal Pilot",
            },
        },
    )
    agent: Optional[str] = Field(
        None,
        title="Goal Pilot Agent",
        description="Agent label to run as Goal Pilot (e.g., agent:goal-pilot)",
        json_schema_extra={
            "doc_examples": ["agent:goal-pilot"],
            "doc_notes": "Must match a label defined under agents.",
            "section": "Goal Pilot",
            "config_attr": "goal_pilot.agent",
            "yaml_path": "goal_pilot.agent",
            "doctor_check": DOCTOR_CHECK_REFERENCES_AGENT,
            "doctor_check_condition": "goal_pilot.enabled",
            "doctor_severity": DOCTOR_SEVERITY_ERROR,
        },
    )
    approval_policy: Literal["journeys_only", "gatekeeper", "batch"] = Field(
        "journeys_only",
        title="Approval Policy",
        description="How Goal Pilot applies repo changes",
        json_schema_extra={
            "doc_examples": ["journeys_only", "gatekeeper", "batch"],
            "doc_notes": "Batch mode bundles changes before approval; gatekeeper requests approval per change.",
            "section": "Goal Pilot",
            "config_attr": "goal_pilot.approval_policy",
            "yaml_path": "goal_pilot.approval_policy",
        },
    )
    approval_batch_size: int = Field(
        10,
        title="Approval Batch Size",
        description="How many changes to bundle before approval (batch mode)",
        ge=1,
        le=200,
        json_schema_extra={
            "doc_examples": ["5", "10", "25"],
            "doc_notes": "Used only when approval_policy=batch.",
            "section": "Goal Pilot",
            "config_attr": "goal_pilot.approval_batch_size",
            "yaml_path": "goal_pilot.approval_batch_size",
        },
    )
    approval_batch_window_minutes: int = Field(
        60,
        title="Approval Batch Window (minutes)",
        description="Max time to wait before asking for approval (batch mode)",
        ge=1,
        le=1440,
        json_schema_extra={
            "doc_examples": ["30", "60", "120"],
            "doc_notes": "Used only when approval_policy=batch.",
            "section": "Goal Pilot",
            "config_attr": "goal_pilot.approval_batch_window_minutes",
            "yaml_path": "goal_pilot.approval_batch_window_minutes",
        },
    )


class AdvancedSettings(BaseModel):
    """Settings for the Advanced tab."""

    sqlite_backup_enabled: bool = Field(
        True,
        title="Enable SQLite Backups",
        description="Enable automatic backups of local SQLite state",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": "Disable only if backups are managed externally.",
            "section": "State & Backups",
            "config_attr": "sqlite_backup.enabled",
            "yaml_path": "sqlite_backup.enabled",
        },
    )
    sqlite_backup_cadence_hours: int = Field(
        24,
        title="Backup Cadence (hours)",
        description="Minimum hours between backups",
        ge=1,
        le=168,
        json_schema_extra={
            "doc_examples": ["6", "24", "48"],
            "doc_notes": "Lower values increase backup frequency.",
            "section": "State & Backups",
            "config_attr": "sqlite_backup.cadence_hours",
            "yaml_path": "sqlite_backup.cadence_hours",
        },
    )
    sqlite_backup_check_interval_minutes: int = Field(
        60,
        title="Backup Check Interval (minutes)",
        description="How often to check whether backups are due",
        ge=5,
        le=1440,
        json_schema_extra={
            "doc_examples": ["30", "60", "120"],
            "doc_notes": "Checks are lightweight; keep reasonably frequent.",
            "section": "State & Backups",
            "config_attr": "sqlite_backup.check_interval_minutes",
            "yaml_path": "sqlite_backup.check_interval_minutes",
        },
    )
    sqlite_backup_retention_daily: int = Field(
        14,
        title="Daily Backup Retention",
        description="Number of daily backups to keep",
        ge=0,
        le=60,
        json_schema_extra={
            "doc_examples": ["7", "14", "30"],
            "doc_notes": "Set to 0 to disable daily backups.",
            "section": "State & Backups",
            "config_attr": "sqlite_backup.retention_daily",
            "yaml_path": "sqlite_backup.retention_daily",
        },
    )
    sqlite_backup_retention_weekly: int = Field(
        8,
        title="Weekly Backup Retention",
        description="Number of weekly backups to keep",
        ge=0,
        le=52,
        json_schema_extra={
            "doc_examples": ["4", "8", "12"],
            "doc_notes": "Set to 0 to disable weekly backups.",
            "section": "State & Backups",
            "config_attr": "sqlite_backup.retention_weekly",
            "yaml_path": "sqlite_backup.retention_weekly",
        },
    )
    sqlite_backup_enforce_on_startup: bool = Field(
        True,
        title="Enforce Backup on Startup",
        description="If cadence elapsed, force a backup on startup",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": "Keeps backups current if the process was stopped for a while.",
            "section": "State & Backups",
            "config_attr": "sqlite_backup.enforce_on_startup",
            "yaml_path": "sqlite_backup.enforce_on_startup",
        },
    )
    timeline_max_records: int = Field(
        5000,
        title="Timeline Retention (records)",
        description="Max timeline events kept per issue before trimming",
        ge=0,
        json_schema_extra={
            "doc_examples": ["2000", "5000", "10000"],
            "doc_notes": "Set to 0 to disable trimming; higher values keep more history but grow state files faster.",
            "section": "Timeline",
            "config_attr": "timeline.max_records",
            "yaml_path": "timeline.max_records",
        },
    )

    provider_short_retry_max_attempts: int = Field(
        4,
        title="Provider Retry Attempts",
        description="Max attempts for transient provider failures",
        ge=1,
        le=10,
        json_schema_extra={
            "doc_examples": ["2", "4", "6"],
            "doc_notes": "Higher values reduce failures but can prolong degraded runs.",
            "section": "Provider Resilience",
            "config_attr": "provider_resilience.short_retry.max_attempts",
            "yaml_path": "provider_resilience.short_retry.max_attempts",
        },
    )
    provider_short_retry_initial_backoff_seconds: int = Field(
        5,
        title="Provider Retry Backoff (seconds)",
        description="Initial backoff for transient provider retries",
        ge=1,
        le=300,
        json_schema_extra={
            "doc_examples": ["2", "5", "10"],
            "doc_notes": "Shorter backoffs retry faster but can amplify rate limits.",
            "section": "Provider Resilience",
            "config_attr": "provider_resilience.short_retry.initial_backoff_seconds",
            "yaml_path": "provider_resilience.short_retry.initial_backoff_seconds",
        },
    )
    provider_short_retry_max_backoff_seconds: int = Field(
        60,
        title="Provider Retry Max Backoff (seconds)",
        description="Maximum backoff for transient provider retries",
        ge=1,
        le=3600,
        json_schema_extra={
            "doc_examples": ["30", "60", "120"],
            "doc_notes": "Caps exponential backoff to avoid excessive waiting.",
            "section": "Provider Resilience",
            "config_attr": "provider_resilience.short_retry.max_backoff_seconds",
            "yaml_path": "provider_resilience.short_retry.max_backoff_seconds",
        },
    )
    provider_short_retry_jitter: bool = Field(
        True,
        title="Provider Retry Jitter",
        description="Apply full jitter to provider retry backoff",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": "Keep enabled to avoid synchronized retry storms.",
            "section": "Provider Resilience",
            "config_attr": "provider_resilience.short_retry.jitter",
            "yaml_path": "provider_resilience.short_retry.jitter",
        },
    )
    provider_circuit_cooldown_seconds: int = Field(
        1800,
        title="Provider Cooldown (seconds)",
        description="Cooldown window before retrying provider after outage",
        ge=60,
        le=86400,
        json_schema_extra={
            "doc_examples": ["600", "1800", "3600"],
            "doc_notes": "Longer cooldowns reduce repeated failures during incidents.",
            "section": "Provider Resilience",
            "config_attr": "provider_resilience.circuit_breaker.cooldown_seconds",
            "yaml_path": "provider_resilience.circuit_breaker.cooldown_seconds",
        },
    )
    provider_circuit_max_cooldowns: int = Field(
        6,
        title="Provider Max Cooldowns",
        description="Maximum cooldown escalation steps",
        ge=1,
        le=12,
        json_schema_extra={
            "doc_examples": ["3", "6", "8"],
            "doc_notes": "Limits how long we will keep extending cooldowns.",
            "section": "Provider Resilience",
            "config_attr": "provider_resilience.circuit_breaker.max_cooldowns",
            "yaml_path": "provider_resilience.circuit_breaker.max_cooldowns",
        },
    )
    provider_circuit_label: str = Field(
        "blocked:provider-unavailable",
        title="Provider Blocked Label",
        description="Label applied when provider is unavailable",
        json_schema_extra={
            "doc_examples": ["blocked:provider-unavailable"],
            "doc_notes": "Use a label that is visible and searchable in your workflow.",
            "section": "Provider Resilience",
            "config_attr": "provider_resilience.circuit_breaker.label",
            "yaml_path": "provider_resilience.circuit_breaker.label",
        },
    )

    session_no_output_seconds: int = Field(
        120,
        title="No-Output Threshold (seconds)",
        description="Emit event after this much idle time",
        ge=30,
        le=600,
        json_schema_extra={
            "doc_examples": ["60", "120", "300"],
            "doc_notes": "Lower values surface silent sessions sooner.",
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
            "doc_examples": ["0", "3", "5"],
            "doc_notes": "Set to 0 to disable automatic escalation.",
            "section": "Observability",
            "config_attr": "stale_escalation_ticks",
            "yaml_path": "observability.stale_escalation_ticks",
        },
    )
    session_output_retention_days: int = Field(
        7,
        title="Session Output Retention (days)",
        description="Retention window in days for session run artifacts",
        ge=0,
        le=365,
        json_schema_extra={
            "doc_examples": ["0", "7", "30"],
            "doc_notes": "Set to 0 to expire immediately; cleanup policy may still defer deletion.",
            "section": "Observability",
            "config_attr": "session_output_retention_days",
            "yaml_path": "observability.session_output_retention_days",
        },
    )
    session_output_retention_tier: Literal["hot", "cold"] = Field(
        "hot",
        title="Session Output Retention Tier",
        description="Retention tier tag recorded in run manifests",
        json_schema_extra={
            "doc_examples": ["hot", "cold"],
            "doc_notes": "Use hot for short-term troubleshooting and cold for longer forensic retention.",
            "section": "Observability",
            "config_attr": "session_output_retention_tier",
            "yaml_path": "observability.session_output_retention_tier",
        },
    )
    web_port: int = Field(
        8080,
        title="Web Dashboard Port",
        description="Port for the web dashboard (requires restart)",
        ge=1024,
        le=65535,
        json_schema_extra={
            "doc_examples": ["8080", "3000", "9090"],
            "doc_notes": "Change if the default port is occupied.",
            "section": "Ports",
            "restart_required": True,
            "config_attr": "web_port",
            "yaml_path": "ui.web_port",
            "setup": {
                "enabled": True,
                "section": "ui",
                "order": 10,
                "condition": {"field": "ui_mode", "value": "web"},
            },
        },
    )
    control_api_port: int = Field(
        19080,
        title="Control API Port",
        description="0 = disabled",
        ge=0,
        le=65535,
        json_schema_extra={
            "doc_examples": ["0", "19080", "19081"],
            "doc_notes": "Set to 0 to disable the control API listener.",
            "section": "Ports",
            "restart_required": True,
            "config_attr": "control_api_port",
            "yaml_path": "ui.control_api_port",
        },
    )
    ai_systems_allowed: str = Field(
        "",
        title="AI Systems Allowlist",
        description="Additional ai_system values allowed in config (comma-separated)",
        json_schema_extra={
            "doc_examples": ["codex, custom-system", ""],
            "doc_notes": "Use to allow new providers beyond ai_systems.yaml.",
            "section": "AI Systems",
            "config_attr": "ai_systems_allowed",
            "yaml_path": "ai_systems.allowed",
            "ui_transform": "comma_separated_list",
        },
    )
    worktree_base: str = Field(
        "../",
        title="Worktree Base Directory",
        description="Directory where git worktrees are created",
        json_schema_extra={
            "doc_examples": ["../", "../worktrees", "/tmp/worktrees"],
            "doc_notes": "Relative paths are resolved from the repo root.",
            "section": "Worktrees",
            "restart_required": True,
            "config_attr": "worktree_base",
            "yaml_path": "worktrees.base",
            "setup": {
                "enabled": True,
                "section": "worktrees",
                "order": 10,
            },
        },
    )
    worktree_base_branch_override: str | None = Field(
        None,
        title="Worktree Base Branch Override",
        description="Override the base branch for worktree creation (auto-detect if unset)",
        json_schema_extra={
            "doc_examples": ["main", "master"],
            "doc_notes": "Use when your default branch is not auto-detected correctly.",
            "section": "Worktrees",
            "restart_required": True,
            "config_attr": "worktree_base_branch_override",
            "yaml_path": "worktrees.base_branch_override",
        },
    )
    worktree_branch_on_recreate: Literal["delete", "create_new_branch"] = Field(
        "delete",
        title="Branch on Recreate",
        description="What to do when recreating a worktree with existing branch",
        json_schema_extra={
            "doc_examples": ["delete", "create_new_branch"],
            "doc_notes": "Use create_new_branch to keep the old branch intact.",
            "section": "Worktrees",
            "restart_required": True,
            "config_attr": "worktree_branch_on_recreate",
            "yaml_path": "worktrees.worktree_branch_on_recreate",
        },
    )
    worktree_setup: str = Field(
        "",
        title="Worktree Setup Commands",
        description="Commands to run in each new worktree after creation (one per line)",
        json_schema_extra={
            "doc_examples": ["npm install", "pip install -e '.[dev]'", "make setup"],
            "doc_notes": "Each command runs in the worktree directory. Leave empty if no setup needed. "
            "The orchestrator's own setup (hooks, coding-done, reviewer-done, Claude settings) is automatic.",
            "section": "Worktrees",
            "restart_required": True,
            "config_attr": "setup_worktree",
            "yaml_path": "worktrees.setup",
            "ui_transform": "newline_separated_list",
            "setup": {
                "enabled": True,
                "section": "worktrees",
                "order": 20,
            },
        },
    )


class HooksSettings(BaseModel):
    """Settings for the Hooks tab."""

    ai_gate_interval_days: int = Field(
        7,
        title="AI Gate Test Interval (days)",
        description="Run AI gate tests every N days (0 = disabled)",
        ge=0,
        le=365,
        json_schema_extra={
            "doc_examples": ["0", "7", "30"],
            "doc_notes": "Set to 0 to disable periodic AI gate tests.",
            "section": "AI Gate",
            "config_attr": "hooks.ai_gate.interval_days",
            "yaml_path": "hooks.ai_gate.interval_days",
            "summary": {
                "section": "hooks",
                "format": SUMMARY_INTERVAL,
                "label": "ai gate",
                "zero_label": "disabled",
                "unit": "d",
            },
        },
    )
    ai_gate_dangerous_allow_failure: bool = Field(
        False,
        title="Allow AI Gate Failure",
        description="If true, warn only on AI gate failure; if false, block orchestrator start",
        json_schema_extra={
            "doc_examples": ["true", "false"],
            "doc_notes": "Keep false in production to enforce hook integrity.",
            "section": "AI Gate",
            "config_attr": "hooks.ai_gate.dangerous_allow_failure",
            "yaml_path": "hooks.ai_gate.dangerous_allow_failure",
            "summary": {
                "section": "hooks",
                "format": SUMMARY_BOOLEAN_FLAG,
                "label": "on failure",
                "true_value": "warn",
                "false_value": "block",
            },
        },
    )


# ---------------------------------------------------------------------------
# Tab definitions (ordered as they appear in the UI)
# ---------------------------------------------------------------------------

TAB_DEFINITIONS: list[dict[str, Any]] = [
    {"key": "concurrency", "label": "Concurrency", "model": ConcurrencySettings},
    {"key": "e2e", "label": "E2E Runner", "model": E2ESettings},
    {"key": "filtering", "label": "Filtering", "model": FilteringSettings},
    {"key": "milestones", "label": "Milestones", "model": MilestonesSettings},
    {"key": "review", "label": "Review", "model": ReviewSettings},
    {"key": "goal_pilot", "label": "Goal Pilot", "model": GoalPilotSettings},
    {"key": "hooks", "label": "Hooks", "model": HooksSettings},
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
            elif transform == "newline_separated_list":
                raw = "\n".join(raw) if raw else ""
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
            elif transform == "newline_separated_list":
                value = [s.strip() for s in value.split("\n") if s.strip()] if value else []

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
# Setup wizard field extraction (data-driven)
# ---------------------------------------------------------------------------

def get_setup_fields(section: str) -> list[dict[str, Any]]:
    """Get schema fields for a wizard section, sorted by order.

    Returns a list of field metadata dicts with keys:
        name, title, description, default, type, order, prompt, condition, tab_key
    """
    fields: list[dict[str, Any]] = []
    for tab in TAB_DEFINITIONS:
        for field_name, field_info in tab["model"].model_fields.items():
            extra = field_info.json_schema_extra or {}
            setup = extra.get("setup")
            if not setup or not setup.get("enabled"):
                continue
            if setup.get("section") != section:
                continue
            fields.append({
                "name": field_name,
                "title": field_info.title,
                "description": field_info.description,
                "default": field_info.default,
                "type": field_info.annotation,
                "order": setup.get("order", 0),
                "prompt": setup.get("prompt", field_info.title),
                "condition": setup.get("condition"),
                "tab_key": tab["key"],
                "yaml_path": extra.get("yaml_path", field_name),
            })
    fields.sort(key=lambda f: f["order"])
    return fields


# ---------------------------------------------------------------------------
# Doctor check field extraction (data-driven)
# ---------------------------------------------------------------------------

def get_doctor_check_fields() -> list[dict[str, Any]]:
    """Get all schema fields that have doctor_check annotations.

    Returns a list of field metadata dicts with keys:
        name, doctor_check, doctor_check_condition, doctor_severity, config_attr,
        title, tab_key, ui_transform
    """
    fields: list[dict[str, Any]] = []
    for tab in TAB_DEFINITIONS:
        for field_name, field_info in tab["model"].model_fields.items():
            extra = field_info.json_schema_extra or {}
            doctor_check = extra.get("doctor_check")
            if not doctor_check:
                continue
            fields.append({
                "name": field_name,
                "doctor_check": doctor_check,
                "doctor_check_condition": extra.get("doctor_check_condition"),
                "doctor_severity": extra.get("doctor_severity", DOCTOR_SEVERITY_ERROR),
                "config_attr": extra["config_attr"],
                "title": field_info.title,
                "tab_key": tab["key"],
                "ui_transform": extra.get("ui_transform"),
            })
    return fields


def get_summary_fields(section: str) -> list[dict[str, Any]]:
    """Get schema fields that contribute to a doctor status summary.

    Returns a list of field metadata dicts with summary format info.
    """
    fields: list[dict[str, Any]] = []
    for tab in TAB_DEFINITIONS:
        for field_name, field_info in tab["model"].model_fields.items():
            extra = field_info.json_schema_extra or {}
            summary = extra.get("summary")
            if not summary or summary.get("section") != section:
                continue
            fields.append({
                "name": field_name,
                "config_attr": extra["config_attr"],
                "ui_transform": extra.get("ui_transform"),
                **summary,
            })
    return fields


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
        lines.append("| Field | Type | Default | Description | Examples | Notes |")
        lines.append("|-------|------|---------|-------------|----------|-------|")
        model_cls = tab["model"]
        schema = model_cls.model_json_schema()
        for prop_name, prop in schema.get("properties", {}).items():
            field_info = model_cls.model_fields[prop_name]
            extra = field_info.json_schema_extra or {}
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
            examples = extra.get("doc_examples", [])
            if not isinstance(examples, list):
                examples = [str(examples)]
            examples_str = ", ".join(f"`{e}`" for e in examples if str(e) != "")
            notes = extra.get("doc_notes", "") or ""
            lines.append(
                f"| `{yaml_path}` | {type_str} | `{default}` | {desc} | {examples_str} | {notes} |"
            )
        lines.append("")
    return "\n".join(lines)
