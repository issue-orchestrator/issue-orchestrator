"""Dashboard dialog and diagnostics routes."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import json
import platform
from typing import Annotated

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from ..contracts.ui_openapi_models import (
    BlockedIssuesDialogPayload,
    ConfigDialogPayload,
    DebugDialogPayload,
    DoctorDialogPayload,
    InfoDialogPayload,
    PhaseDialogPayload,
    SessionDiagnosticsDialogPayload,
    ValidationFailureDialogPayload,
)
from ..execution.client_host import ClientHost
from ..view_models.dialogs import (
    build_blocked_issues_dialog,
    build_config_dialog,
    build_debug_dialog,
    build_doctor_dialog,
    build_info_dialog,
    build_phase_dialog,
    build_session_diagnostics_dialog,
    build_validation_failure_dialog,
)
from .web_session_context import WebOrchestratorDependency
from .web_session_routes import get_session_manifest, get_session_phases

web_diagnostics_router = APIRouter()

_WEB_DIAGNOSTICS_DEPENDENCIES_STATE_KEY = "web_diagnostics_dependencies"


@dataclass(frozen=True)
class WebDiagnosticsDependencies:
    """Runtime adapters needed by diagnostics routes."""

    get_client_host: Callable[[], ClientHost]


def install_web_diagnostics_dependencies(
    app: FastAPI,
    *,
    get_client_host: Callable[[], ClientHost],
) -> None:
    """Install diagnostics route dependencies on the FastAPI app."""
    setattr(
        app.state,
        _WEB_DIAGNOSTICS_DEPENDENCIES_STATE_KEY,
        WebDiagnosticsDependencies(get_client_host=get_client_host),
    )


def get_web_diagnostics_dependencies(request: Request) -> WebDiagnosticsDependencies:
    """Return diagnostics route dependencies for the current app."""
    deps = getattr(request.app.state, _WEB_DIAGNOSTICS_DEPENDENCIES_STATE_KEY, None)
    if not isinstance(deps, WebDiagnosticsDependencies):
        raise RuntimeError("Web diagnostics dependencies are not installed")
    return deps


WebDiagnosticsDependency = Annotated[
    WebDiagnosticsDependencies,
    Depends(get_web_diagnostics_dependencies),
]


def _response_json(response: JSONResponse) -> dict:
    body = response.body
    if isinstance(body, memoryview):
        body = body.tobytes()
    return json.loads(body.decode("utf-8"))


@web_diagnostics_router.get("/api/dialog/info", response_model=InfoDialogPayload)
async def get_info_dialog(
    orchestrator: WebOrchestratorDependency,
    deps: WebDiagnosticsDependency,
) -> InfoDialogPayload | JSONResponse:
    """Get view model for the About dialog."""
    response = await get_info(orchestrator, deps)
    if response.status_code != 200:
        return response
    payload = _response_json(response)
    return InfoDialogPayload.model_validate(build_info_dialog(payload))


@web_diagnostics_router.get("/api/dialog/config", response_model=ConfigDialogPayload)
async def get_config_dialog(orchestrator: WebOrchestratorDependency) -> ConfigDialogPayload | JSONResponse:
    """Get view model for the configuration dialog."""
    response = await get_config(orchestrator)
    if response.status_code != 200:
        return response
    payload = _response_json(response)
    return ConfigDialogPayload.model_validate(build_config_dialog(payload.get("config", "")))


@web_diagnostics_router.get("/api/dialog/debug", response_model=DebugDialogPayload)
async def get_debug_dialog(orchestrator: WebOrchestratorDependency) -> DebugDialogPayload | JSONResponse:
    """Get view model for the debug dialog."""
    response = await get_debug(orchestrator)
    if response.status_code != 200:
        return response
    payload = _response_json(response)
    return DebugDialogPayload.model_validate(build_debug_dialog(payload))


@web_diagnostics_router.get("/api/dialog/doctor", response_model=DoctorDialogPayload)
async def get_doctor_dialog(orchestrator: WebOrchestratorDependency) -> DoctorDialogPayload | JSONResponse:
    """Get view model for the doctor dialog."""
    response = await get_doctor(orchestrator)
    if response.status_code != 200:
        return response
    payload = _response_json(response)
    return DoctorDialogPayload.model_validate(build_doctor_dialog(payload))


@web_diagnostics_router.get("/api/dialog/session-diagnostics/{issue_number}", response_model=SessionDiagnosticsDialogPayload)
async def get_session_diagnostics_dialog(
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
    run_dir: str | None = None,
) -> SessionDiagnosticsDialogPayload | JSONResponse:
    """Get view model for session diagnostics dialog."""
    response = await get_session_manifest(
        issue_number,
        orchestrator=orchestrator,
        run_dir=run_dir,
    )
    if response.status_code != 200:
        return response
    payload = _response_json(response)
    return SessionDiagnosticsDialogPayload.model_validate(build_session_diagnostics_dialog(issue_number, payload))


@web_diagnostics_router.get("/api/dialog/validation-failure/{issue_number}", response_model=ValidationFailureDialogPayload)
async def get_validation_failure_dialog(
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
    run_dir: str | None = None,
) -> ValidationFailureDialogPayload | JSONResponse:
    """Get a focused dialog for a failed validation run."""
    response = await get_session_manifest(
        issue_number,
        orchestrator=orchestrator,
        run_dir=run_dir,
    )
    if response.status_code != 200:
        return response
    payload = _response_json(response)
    validation = payload.get("validation_failure")
    if not isinstance(validation, dict):
        return JSONResponse({"error": "No validation failure details found"}, status_code=404)
    return ValidationFailureDialogPayload.model_validate(build_validation_failure_dialog(issue_number, payload))


@web_diagnostics_router.get("/api/dialog/blocked-issues", response_model=BlockedIssuesDialogPayload)
async def get_blocked_issues_dialog(orchestrator: WebOrchestratorDependency) -> BlockedIssuesDialogPayload | JSONResponse:
    """Get view model for blocked issues dialog."""
    response = await get_blocked_issues(orchestrator)
    if response.status_code != 200:
        return response
    payload = _response_json(response)
    return BlockedIssuesDialogPayload.model_validate(build_blocked_issues_dialog(payload))


@web_diagnostics_router.get("/api/dialog/phase/{issue_number}", response_model=PhaseDialogPayload)
async def get_phase_dialog(
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
    phase: str | None = None,
) -> PhaseDialogPayload | JSONResponse:
    """Get view model for phase details dialog."""
    response = await get_session_phases(issue_number, orchestrator=orchestrator)
    if response.status_code != 200:
        return response
    payload = _response_json(response)
    return PhaseDialogPayload.model_validate(build_phase_dialog(payload, issue_number, phase))


@web_diagnostics_router.get("/api/dependency-problems")
async def get_dependency_problems(orchestrator: WebOrchestratorDependency) -> JSONResponse:
    """Get current dependency problems for issues."""
    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    state = orchestrator.state
    config = orchestrator.config

    def make_issue_url(issue_number: int) -> str:
        return f"https://github.com/{config.repo}/issues/{issue_number}" if config.repo else ""

    problems = {}
    for issue_num, problem in state.dependency_problems.items():
        problems[issue_num] = {
            "issue_number": problem.issue_number,
            "issue_title": problem.issue_title,
            "summary": problem.summary,
            "issue_url": make_issue_url(problem.issue_number),
        }

    return JSONResponse({"problems": problems})


@web_diagnostics_router.get("/api/stale-issues")
async def get_stale_issues(orchestrator: WebOrchestratorDependency) -> JSONResponse:
    """Get issues with stale in-progress labels."""
    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    state = orchestrator.state
    config = orchestrator.config
    threshold = config.stale_escalation_ticks

    stale = {}
    for issue_num, ticks in state.stale_issue_ticks.items():
        stale[issue_num] = {
            "issue_number": issue_num,
            "consecutive_ticks": ticks,
            "persistent": threshold > 0 and ticks >= threshold,
            "threshold": threshold,
        }

    return JSONResponse({"stale": stale})


@web_diagnostics_router.get("/api/info")
async def get_info(
    orchestrator: WebOrchestratorDependency,
    deps: WebDiagnosticsDependency,
) -> JSONResponse:
    """Get orchestrator info for the About modal."""
    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    state = orchestrator.state
    config = orchestrator.config
    from ..infra.repo_identity import build_repo_identity

    repo_identity = build_repo_identity(config.repo_root)
    commit_sha = repo_identity.commit_sha
    client_capabilities = deps.get_client_host().capabilities()

    return JSONResponse({
        "version": "0.1.0",  # TODO: get from package
        "repo": config.repo,
        "repo_root": str(config.repo_root) if config.repo_root else None,
        "ui_mode": config.ui_mode,
        "terminal_backend": config.terminal_adapter or "subprocess",
        "client_capabilities": {
            "focus_session": (config.terminal_adapter or "subprocess") != "subprocess",
            "open_path": client_capabilities.open_path,
            "reveal_worktree": client_capabilities.reveal_worktree,
            "local_server_paths_only": client_capabilities.local_only,
            "host_platform": platform.system().lower(),
        },
        "commit_sha": commit_sha,
        "commit_short": commit_sha[:7] if commit_sha else None,
        "repo_identity": repo_identity.to_dict(),
        "max_sessions": config.max_concurrent_sessions,
        "active_sessions": len(state.active_sessions),
        "completed_today": len(state.completed_today),
    })


@web_diagnostics_router.get("/api/config")
async def get_config(orchestrator: WebOrchestratorDependency) -> JSONResponse:
    """Get the raw config file contents."""
    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    config = orchestrator.config

    config_text = "Config file not found"
    if config.config_path and config.config_path.exists():
        config_text = config.config_path.read_text()

    return JSONResponse({"config": config_text})


@web_diagnostics_router.get("/api/blocked-issues")
async def get_blocked_issues(orchestrator: WebOrchestratorDependency) -> JSONResponse:
    """Get all blocked issues with their blocking labels and context."""
    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    from ..control.worktree_manager import get_worktree_path

    state = orchestrator.state
    config = orchestrator.config
    lm = orchestrator.deps.label_manager

    def make_issue_url(issue_number: int) -> str:
        return f"https://github.com/{config.repo}/issues/{issue_number}" if config.repo else ""

    blocked_issues = []

    if state.startup_status == "complete":
        for issue in state.cached_queue_issues:
            if not issue.is_blocked:
                continue

            blocking_labels = lm.get_blocking(list(issue.labels))
            blocking_label = blocking_labels[0] if blocking_labels else "blocked"
            needs_human = lm.requires_human_any(list(issue.labels))

            failure_reason = None
            for entry in reversed(state.session_history):
                if entry.issue_number == issue.number:
                    failure_reason = getattr(entry, "status_reason", None) or entry.status
                    break

            worktree_path = get_worktree_path(config, issue.number)
            worktree_exists = worktree_path.exists()
            has_completion = False
            if worktree_exists:
                completion_path = worktree_path / ".issue-orchestrator" / "completion.json"
                has_completion = completion_path.exists()

            blocked_issues.append({
                "issue_number": issue.number,
                "title": issue.title,
                "agent_type": (issue.agent_type or "unknown").replace("agent:", ""),
                "blocking_label": blocking_label,
                "all_blocking_labels": blocking_labels,
                "needs_human": needs_human,
                "failure_reason": failure_reason,
                "issue_url": make_issue_url(issue.number),
                "worktree_path": str(worktree_path) if worktree_exists else None,
                "has_completion": has_completion,
            })

    return JSONResponse({"blocked_issues": blocked_issues})


@web_diagnostics_router.get("/api/failure-diagnosis/{issue_number}")
async def get_failure_diagnosis(
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
) -> JSONResponse:
    """Get detailed failure diagnosis for an issue."""
    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    diagnosis = orchestrator.get_failure_diagnosis(issue_number)
    return JSONResponse(diagnosis)


@web_diagnostics_router.post("/api/issues/{issue_number}/audit")
async def force_issue_audit(
    issue_number: int,
    orchestrator: WebOrchestratorDependency,
) -> JSONResponse:
    """Force a fresh session-failure audit for an issue."""
    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    diagnosis = orchestrator.get_failure_diagnosis(issue_number)
    return JSONResponse(diagnosis)


@web_diagnostics_router.get("/api/debug")
async def get_debug(orchestrator: WebOrchestratorDependency) -> JSONResponse:
    """Get debug info for troubleshooting."""
    if not orchestrator:
        return JSONResponse({"error": "Orchestrator not running"}, status_code=503)

    state = orchestrator.state
    config = orchestrator.config

    agents = {}
    for name, agent_cfg in config.agents.items():
        agents[name] = {
            "timeout": agent_cfg.timeout_minutes,
            "command": agent_cfg.command[:50] + "..." if len(agent_cfg.command) > 50 else agent_cfg.command,
        }

    startup_options = {
        "ui_mode": config.ui_mode,
        "web_port": config.web_port,
        "test_mode": config.filtering.label == "test-data",
        "filtering": {
            "label": config.filtering.label,
            "milestone": config.filtering.milestone,
            "milestones": config.filtering.get_milestones(),
        },
        "max_sessions": config.max_concurrent_sessions,
    }

    return JSONResponse({
        "paused": state.paused,
        "config_path": str(config.config_path) if config.config_path else "None",
        "repo_root": str(config.repo_root),
        "priority_queue": state.priority_queue,
        "agents": agents,
        "startup_options": startup_options,
    })


@web_diagnostics_router.get("/api/doctor")
async def get_doctor(orchestrator: WebOrchestratorDependency) -> JSONResponse:
    """Run diagnostics and return health status."""
    from ..execution.command_runner import LocalCommandRunner
    from ..infra.doctor import run_doctor

    config = orchestrator.config if orchestrator else None

    result = run_doctor(config=config, runner=LocalCommandRunner())

    if orchestrator:
        result.checks.insert(2, type(result.checks[0])(
            name="Orchestrator",
            status="ok",
            detail=f"Running, {'paused' if orchestrator.state.paused else 'active'}",
        ))
    else:
        result.checks.insert(2, type(result.checks[0])(
            name="Orchestrator",
            status="error",
            detail="Not running",
        ))

    return JSONResponse(result.to_dict())
