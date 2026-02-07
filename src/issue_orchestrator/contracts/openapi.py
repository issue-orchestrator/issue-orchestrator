"""OpenAPI-facing schemas for the web UI view-model endpoints."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict


class IssueItemPayload(BaseModel):
    """UI issue item payload (shared across tabs)."""

    model_config = ConfigDict(extra="allow")

    issue_number: int | str | None = None
    title: str | None = None
    status: str | None = None
    action: str | None = None
    action_hint: str | None = None
    url: str | None = None
    issue_url: str | None = None


class DashboardDataPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    startupComplete: bool
    paused: bool
    e2eRunning: bool
    queueRefreshSeconds: int
    repo: str
    repoRoot: str
    githubOwner: str
    githubRepo: str
    e2eLastRun: dict[str, Any] | None = None
    agents: list[str]


class DashboardViewModelPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issues: list[IssueItemPayload]
    active_items: list[IssueItemPayload]
    queue_items: list[IssueItemPayload]
    blocked_items: list[IssueItemPayload]
    history_items: list[IssueItemPayload]
    e2e_items: list[IssueItemPayload]

    active_count: int
    queue_count: int
    blocked_count: int
    history_count: int
    e2e_count: int

    active_tab: str
    paused: bool
    shutdown_requested: bool
    active_session_count: int
    startup_status: str
    startup_message: str

    repo: str
    repo_root: str
    github_owner: str
    github_repo: str

    queue_page: int
    queue_total_pages: int
    queue_total: int
    queue_refresh_seconds: int

    e2e_status: dict[str, Any]
    e2e_page: int
    e2e_total_pages: int
    e2e_total: int

    agents: list[str]
    dashboard_data: DashboardDataPayload


class IssueRowPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue_number: int | str | None = None
    html: str


class IssueRowsPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rows: list[IssueRowPayload]
    active_tab: str
    count: int


class DialogRowPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    label: str
    value: str


class DialogSectionPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    rows: list[DialogRowPayload]


class InfoDialogPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    rows: list[DialogRowPayload]


class ConfigDialogPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    config_text: str


class DebugDialogPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    sections: list[DialogSectionPayload]


class DoctorCheckPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = None
    status: str | None = None
    detail: str | None = None


class DoctorDialogPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    overall: str
    checks: list[DoctorCheckPayload]


class SessionDiagnosticsActionPayload(BaseModel):
    model_config = ConfigDict(extra="allow")

    type: str
    label: str
    path: str | None = None
    issue_number: int | None = None


class SessionDiagnosticsDialogPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    rows: list[DialogRowPayload]
    actions: list[SessionDiagnosticsActionPayload]


class BlockedIssuePayload(BaseModel):
    model_config = ConfigDict(extra="allow")


class BlockedIssuesDialogPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    blocked_issues: list[BlockedIssuePayload]


class PhaseDialogPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str
    issue_number: int
    phase: dict[str, Any] | None
    phases: list[dict[str, Any]]
