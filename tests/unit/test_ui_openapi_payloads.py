"""Validate UI payloads against the UI OpenAPI schema."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

import warnings

warnings.filterwarnings(
    "ignore",
    category=DeprecationWarning,
    message="jsonschema.RefResolver is deprecated",
)

from jsonschema import Draft202012Validator, RefResolver

from issue_orchestrator.domain.issue_key import FakeIssueKey
from issue_orchestrator.domain.models import (
    AgentConfig,
    Issue,
    OrchestratorState,
    Session,
)
from issue_orchestrator.domain.session_key import SessionKey, TaskKind
from issue_orchestrator.infra.config import Config
from issue_orchestrator.view_models.dashboard import build_dashboard_view_model
from issue_orchestrator.view_models.dialogs import (
    build_blocked_issues_dialog,
    build_config_dialog,
    build_debug_dialog,
    build_doctor_dialog,
    build_info_dialog,
    build_phase_dialog,
    build_session_diagnostics_dialog,
)


@dataclass
class _OrchestratorStub:
    state: OrchestratorState
    config: Config
    shutdown_requested: bool = False


def _make_config() -> Config:
    config = Config()
    config.repo = "test/repo"
    config.repo_root = Path("/tmp/repo")
    config.queue_refresh_seconds = 300
    config.terminal_adapter = "subprocess"
    config.e2e.enabled = False
    return config


def _make_agent_config() -> AgentConfig:
    return AgentConfig(
        prompt_path=Path("/tmp/prompt.txt"),
        model="sonnet",
        timeout_minutes=30,
    )


def _validator(component: str) -> Draft202012Validator:
    schema = Path("docs/api/ui-openapi.json").read_text()
    data = __import__("json").loads(schema)
    resolver = RefResolver.from_schema(data)
    return Draft202012Validator(data["components"]["schemas"][component], resolver=resolver)


def test_dashboard_view_model_matches_ui_openapi() -> None:
    config = _make_config()
    agent_config = _make_agent_config()
    config.agents = {"agent:web": agent_config}

    issue = Issue(number=12, title="Fix bug", labels=["agent:web"])
    session_key = SessionKey(issue=FakeIssueKey("12"), task=TaskKind.REVIEW)
    session = Session(
        key=session_key,
        issue=issue,
        agent_config=agent_config,
        terminal_id="review-12",
        worktree_path=Path("/tmp/worktree-12"),
        branch_name="feature/12",
        started_at=datetime.now() - timedelta(minutes=3),
    )

    state = OrchestratorState(active_sessions=[session], startup_status="complete")
    orchestrator = _OrchestratorStub(state=state, config=config)

    view_model = build_dashboard_view_model(
        orchestrator,
        queue_page=1,
        active_tab="active",
        e2e_page=1,
        e2e_status_provider=lambda _: {"enabled": False, "running": False},
    )

    validator = _validator("DashboardViewModelPayload")
    validator.validate(view_model.to_dict())


def test_dialog_payloads_match_ui_openapi() -> None:
    info = build_info_dialog({
        "version": "1.0",
        "repo": "test/repo",
        "ui_mode": "web",
        "terminal_backend": "subprocess",
        "commit_short": "abc123",
        "max_sessions": 2,
        "active_sessions": 1,
        "completed_today": 0,
    })
    _validator("InfoDialogPayload").validate(info)

    config_dialog = build_config_dialog("config: value")
    _validator("ConfigDialogPayload").validate(config_dialog)

    debug_dialog = build_debug_dialog({
        "startup_options": {"ui_mode": "web", "web_port": 8080, "test_mode": False, "filtering": {}},
        "paused": False,
        "priority_queue": [],
        "config_path": "/tmp/config.yaml",
        "repo_root": "/tmp/repo",
    })
    _validator("DebugDialogPayload").validate(debug_dialog)

    doctor_dialog = build_doctor_dialog({
        "overall": "ok",
        "checks": [{"name": "health", "status": "ok", "detail": "ok"}],
    })
    _validator("DoctorDialogPayload").validate(doctor_dialog)

    session_diag = build_session_diagnostics_dialog(42, {
        "manifest": {
            "session_name": "session-42",
            "started_at": "2024-01-01T00:00:00Z",
            "run_id": "run-1",
            "backend": "subprocess",
            "agent_label": "agent:web",
            "claude_session_id": "abc",
            "worktree": "/tmp/worktree",
        },
        "run_dir": "/tmp/run",
    })
    _validator("SessionDiagnosticsDialogPayload").validate(session_diag)

    blocked_dialog = build_blocked_issues_dialog({"blocked_issues": [{"issue": 1}]})
    _validator("BlockedIssuesDialogPayload").validate(blocked_dialog)

    phase_dialog = build_phase_dialog({"phases": [{"name": "review-1", "display_name": "Review"}]}, 12, None)
    _validator("PhaseDialogPayload").validate(phase_dialog)
