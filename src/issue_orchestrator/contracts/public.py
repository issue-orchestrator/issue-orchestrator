"""Public contracts for UI-facing payloads.

These contracts are intentionally minimal and stable:
- Require only fields used by the UI.
- Allow extra fields to avoid brittleness.
"""

from __future__ import annotations

from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field


class ContractBase(BaseModel):
    """Base contract with permissive extra fields."""

    model_config = ConfigDict(extra="allow")


class DashboardDataContract(ContractBase):
    startupComplete: bool
    paused: bool
    e2eRunning: bool
    queueRefreshSeconds: int
    repo: Optional[str]
    repoRoot: Optional[str]
    githubOwner: Optional[str]
    githubRepo: Optional[str]
    e2eLastRun: Optional[dict[str, Any]] = None
    agents: list[str]


class DashboardViewModelContract(ContractBase):
    dashboard_data: DashboardDataContract
    paused: bool
    startup_status: str
    active_tab: str
    shutdown_requested: bool


class SessionStartedPayload(ContractBase):
    issue_number: int


class SessionCompletedPayload(ContractBase):
    issue_number: int


class OrchestratorPausedPayload(ContractBase):
    pass


class OrchestratorResumedPayload(ContractBase):
    pass


class QueueChangedPayload(ContractBase):
    added: list[dict[str, Any]]
    removed: list[dict[str, Any]]
    total: int


class DependencyBlockedPayload(ContractBase):
    issue_number: int
    summary: str


class DependencyUnblockedPayload(ContractBase):
    issue_number: int


class StaleDetectedPayload(ContractBase):
    issue_number: int


class StaleClearedPayload(ContractBase):
    issue_number: int


class PersistentStalePayload(ContractBase):
    issue_number: int
    consecutive_ticks: int
    threshold: int


class HistoryReconciledPayload(ContractBase):
    issue_number: int
    issue_key: str
    pr_number: int
    pr_url: str
    previous_status: Literal["completed"]
    status: Literal["merged", "closed"]
    status_reason: str
    source: Literal["pull_request", "issue"]


class StartupCompletePayload(ContractBase):
    elapsed_seconds: float


class ShutdownRequestedPayload(ContractBase):
    force: bool
    # ``reason`` is the calling-site's "why" string. Required on the
    # API contract (see ``/api/shutdown`` in ``web_operator_routes``)
    # so each shutdown is traceable in the orchestrator log.
    reason: str | None = None
    # ``actor`` identifies the source (cc, dashboard, cli, mcp, …)
    # for log-aggregation grouping. Optional on the wire because
    # legacy clients haven't been updated yet.
    actor: str | None = None
    active_sessions: int | None = None


class TimelineArtifactContract(ContractBase):
    type: str
    label: str
    value: str


class TimelineEventContract(ContractBase):
    event_id: str
    timestamp: str
    event: str
    issue_number: int
    phase: str
    step: str
    status: str
    level: str
    summary: Optional[str] = None
    parent_key: str
    artifacts: list[TimelineArtifactContract] = Field(default_factory=list)
    round_index: Optional[int] = None
    reviewer_response_type: Optional[str] = None
    reviewer_response_text: Optional[str] = None
    coder_response_type: Optional[str] = None
    coder_response_text: Optional[str] = None


class TimelineIssueContract(ContractBase):
    issue_number: int
    events: list[TimelineEventContract]


PUBLIC_CONTRACTS: dict[str, type[BaseModel]] = {
    "dashboard.view_model": DashboardViewModelContract,
    "sse.session.started": SessionStartedPayload,
    "sse.session.completed": SessionCompletedPayload,
    "sse.orchestrator.paused": OrchestratorPausedPayload,
    "sse.orchestrator.resumed": OrchestratorResumedPayload,
    "sse.queue.changed": QueueChangedPayload,
    "sse.dependency.blocked": DependencyBlockedPayload,
    "sse.dependency.unblocked": DependencyUnblockedPayload,
    "sse.stale.in_progress_detected": StaleDetectedPayload,
    "sse.stale.in_progress_cleared": StaleClearedPayload,
    "sse.stale.persistent_detected": PersistentStalePayload,
    "sse.history.reconciled": HistoryReconciledPayload,
    "sse.startup_complete": StartupCompletePayload,
    "sse.shutdown_requested": ShutdownRequestedPayload,
    "timeline.issue": TimelineIssueContract,
}


def generate_public_schemas() -> dict[str, dict[str, Any]]:
    """Generate JSON schemas for public contracts."""
    return {name: model.model_json_schema() for name, model in PUBLIC_CONTRACTS.items()}
