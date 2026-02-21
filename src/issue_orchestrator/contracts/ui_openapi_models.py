# This file is generated from docs/api/ui-openapi.json.
# Do not edit by hand. Run: scripts/generate_ui_contracts.py



from __future__ import annotations


from typing import Any


from pydantic import BaseModel, ConfigDict




class BlockedIssuePayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    pass

class BlockedIssuesDialogPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    blocked_issues: list[BlockedIssuePayload]
    title: str

class ConfigDialogPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    config_text: str
    title: str

class DashboardDataPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    agents: list[str]
    e2eLastRun: dict[str, Any] | None = None
    e2eRunning: bool
    githubOwner: str
    githubRepo: str
    paused: bool
    queueRefreshSeconds: int
    repo: str
    repoRoot: str
    startupComplete: bool

class DashboardViewModelPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active_count: int
    active_items: list[IssueItemPayload]
    active_session_count: int
    active_tab: str
    agents: list[str]
    awaiting_merge_count: int
    awaiting_merge_items: list[IssueItemPayload]
    blocked_count: int
    blocked_items: list[IssueItemPayload]
    completed_count: int
    completed_items: list[IssueItemPayload]
    dashboard_data: DashboardDataPayload
    e2e_count: int
    e2e_items: list[IssueItemPayload]
    e2e_page: int
    e2e_status: dict[str, Any]
    e2e_total: int
    e2e_total_pages: int
    flow_columns: list[dict[str, Any]]
    github_owner: str
    github_repo: str
    history_items: list[IssueItemPayload]
    issues: list[IssueItemPayload]
    paused: bool
    queue_count: int
    queue_items: list[IssueItemPayload]
    queue_page: int
    queue_refresh_seconds: int
    queue_total: int
    queue_total_pages: int
    repo: str
    repo_root: str
    scope_summary: dict[str, Any]
    shutdown_requested: bool
    startup_message: str
    startup_status: str
    provider_circuits: list[dict[str, Any]]

class DebugDialogPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    sections: list[DialogSectionPayload]
    title: str

class DialogRowPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str
    value: str

class DialogSectionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rows: list[DialogRowPayload]
    title: str

class DoctorCheckPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    detail: str | None = None
    name: str | None = None
    status: str | None = None

class DoctorDialogPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    checks: list[DoctorCheckPayload]
    overall: str
    title: str

class InfoDialogPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rows: list[DialogRowPayload]
    title: str

class IssueDetailPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actions: list[dict[str, Any]]
    blocked_detail: dict[str, Any] | None
    cycles: list[dict[str, Any]]
    events: list[dict[str, Any]]
    issue_number: int
    issue_url: str
    journey_cycles: list[dict[str, Any]]
    journey_steps: list[dict[str, Any]]
    lifecycle_count: int
    phase_toc: list[dict[str, Any]]
    previous_cycles: list[dict[str, Any]]
    previous_cycles_count: int
    raw_events_count: int
    status_explanation: str
    summary: dict[str, Any]
    title: str

class IssueItemPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    action: str | None = None
    action_hint: str | None = None
    issue_number: int | str | None = None
    issue_url: str | None = None
    status: str | None = None
    title: str | None = None
    url: str | None = None

class IssueRowPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    html: str
    issue_number: int | str | None = None

class IssueRowsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    active_tab: str
    count: int
    rows: list[IssueRowPayload]

class PhaseDialogPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    issue_number: int
    phase: dict[str, Any] | None
    phases: list[dict[str, Any]]
    title: str

class SessionDiagnosticsActionPayload(BaseModel):
    model_config = ConfigDict(extra="allow")
    issue_number: int | None = None
    label: str
    path: str | None = None
    type: str

class SessionDiagnosticsDialogPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actions: list[SessionDiagnosticsActionPayload]
    rows: list[DialogRowPayload]
    title: str
