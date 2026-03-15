"""Dialog view models for the web UI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DialogRow:
    label: str
    value: str

    def to_dict(self) -> dict[str, str]:
        return {"label": self.label, "value": self.value}


@dataclass(frozen=True)
class DialogSection:
    title: str
    rows: list[DialogRow]

    def to_dict(self) -> dict[str, Any]:
        return {
            "title": self.title,
            "rows": [row.to_dict() for row in self.rows],
        }


@dataclass(frozen=True)
class SessionDiagnosticsContext:
    issue_number: int
    session_name: str
    started_at: str
    run_id: str
    backend: str
    agent_label: str
    claude_session_id: str
    worktree: str
    retention_tier: str
    retention_expires_at: str
    retention_pinned: str
    run_dir: str
    claude_log_path: str
    claude_log_dir: str
    orchestrator_log: str
    diagnostic_path: str
    validation_path: str
    validation_output_path: str
    validation_stderr_path: str
    run_audit_path: str
    validation_status: str
    validation_reason: str
    branch: str
    task: str
    claude_args: str
    claude_prompt_mode: str
    provider: str
    model: str
    permission_mode: str
    timeout_minutes: str
    extra_provider_args: str
    session_settings_path: str

    @classmethod
    def from_payload(
        cls,
        issue_number: int,
        manifest_payload: dict[str, Any],
    ) -> "SessionDiagnosticsContext":
        manifest = manifest_payload.get("manifest") or {}
        session_identity = manifest_payload.get("session_identity") or {}
        worktree = str(manifest.get("worktree") or "")
        session_name = str(manifest.get("session_name") or manifest_payload.get("session_name") or "")
        diagnostic_path = _join_worktree_path(worktree, manifest.get("diagnostic_path"))
        validation_path = _join_worktree_path(worktree, manifest.get("validation_record_path"))
        validation_output_path = _join_worktree_path(
            worktree,
            manifest.get("validation_output_path") or manifest.get("validation_stdout"),
        )
        validation_stderr_path = _join_worktree_path(
            worktree,
            manifest.get("validation_stderr"),
        )
        run_audit_path = _join_worktree_path(worktree, manifest.get("run_audit_path"))
        return cls(
            issue_number=issue_number,
            session_name=session_name,
            started_at=str(manifest.get("started_at") or ""),
            run_id=str(manifest.get("run_id") or ""),
            backend=str(manifest.get("backend") or ""),
            agent_label=str(manifest.get("agent_label") or ""),
            claude_session_id=str(manifest.get("claude_session_id") or ""),
            worktree=worktree,
            retention_tier=str(manifest.get("retention_tier") or ""),
            retention_expires_at=str(manifest.get("retention_expires_at") or ""),
            retention_pinned=str(manifest.get("retention_pinned") if "retention_pinned" in manifest else ""),
            run_dir=str(manifest.get("run_dir") or manifest_payload.get("run_dir") or ""),
            claude_log_path=str(manifest.get("claude_log_path") or ""),
            claude_log_dir=str(manifest.get("claude_log_dir") or ""),
            orchestrator_log=str(manifest.get("orchestrator_log") or ""),
            diagnostic_path=diagnostic_path,
            validation_path=validation_path,
            validation_output_path=validation_output_path,
            validation_stderr_path=validation_stderr_path,
            run_audit_path=run_audit_path,
            validation_status=str(manifest.get("validation_status") or ""),
            validation_reason=str(manifest.get("validation_reason") or ""),
            branch=str(session_identity.get("branch") or ""),
            task=str(session_identity.get("task") or ""),
            claude_args=str(session_identity.get("claude_args") or ""),
            claude_prompt_mode=str(session_identity.get("claude_prompt_mode") or ""),
            provider=str(session_identity.get("provider") or ""),
            model=str(session_identity.get("model") or ""),
            permission_mode=str(session_identity.get("permission_mode") or ""),
            timeout_minutes=str(session_identity.get("timeout_minutes") or ""),
            extra_provider_args=_format_extra_provider_args(session_identity.get("extra_provider_args")),
            session_settings_path=str(Path(manifest_payload.get("run_dir") or "") / "session-identity.json")
            if manifest_payload.get("run_dir")
            else "",
        )


@dataclass(frozen=True)
class SessionDiagnosticAnalysis:
    """Human-oriented diagnostic summary for the current run."""

    headline: str
    detail: str | None = None
    suggestions: tuple[str, ...] = ()

    @classmethod
    def from_payload(cls, payload: dict[str, Any] | None) -> "SessionDiagnosticAnalysis | None":
        if not isinstance(payload, dict):
            return None
        headline = payload.get("headline")
        if not isinstance(headline, str) or not headline.strip():
            return None
        detail = payload.get("detail")
        suggestions_raw = payload.get("suggestions")
        suggestions = tuple(
            item for item in suggestions_raw
            if isinstance(item, str) and item.strip()
        ) if isinstance(suggestions_raw, list) else ()
        return cls(
            headline=headline,
            detail=detail if isinstance(detail, str) and detail.strip() else None,
            suggestions=suggestions,
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"headline": self.headline}
        if self.detail is not None:
            payload["detail"] = self.detail
        if self.suggestions:
            payload["suggestions"] = list(self.suggestions)
        return payload


def _join_worktree_path(worktree: str, rel_path: Any) -> str:
    """Resolve manifest path to an openable filesystem path.

    - Absolute path values are returned as-is.
    - Relative path values are resolved under worktree.
    - Missing worktree + relative path returns empty string.
    """
    rel_value = str(rel_path or "")
    if not rel_value:
        return ""
    rel_candidate = Path(rel_value)
    if rel_candidate.is_absolute():
        return str(rel_candidate)
    if not worktree:
        return ""
    return str(Path(worktree) / rel_candidate)


def _build_session_diagnostics_rows(ctx: SessionDiagnosticsContext) -> list[DialogRow]:
    rows = [
        DialogRow("Session", ctx.session_name or "-"),
        DialogRow("Started", ctx.started_at or "-"),
        DialogRow("Run ID", ctx.run_id or "-"),
        DialogRow("Backend", ctx.backend or "-"),
        DialogRow("Agent", ctx.agent_label or "-"),
        DialogRow("Task", ctx.task or "-"),
        DialogRow("Branch", ctx.branch or "-"),
        DialogRow("Provider", ctx.provider or "-"),
        DialogRow("Model", ctx.model or "-"),
        DialogRow("Permission Mode", ctx.permission_mode or "-"),
        DialogRow("Timeout", f"{ctx.timeout_minutes}m" if ctx.timeout_minutes else "-"),
        DialogRow("Provider Args", ctx.extra_provider_args or "-"),
        DialogRow("Launch Args", ctx.claude_args or "-"),
        DialogRow("Prompt Mode", ctx.claude_prompt_mode or "-"),
        DialogRow("Claude Session", ctx.claude_session_id or "-"),
        DialogRow("Retention Tier", ctx.retention_tier or "-"),
        DialogRow("Retention Expires", ctx.retention_expires_at or "-"),
        DialogRow("Retention Pinned", ctx.retention_pinned or "-"),
        DialogRow("Worktree", ctx.worktree or "-"),
    ]
    if ctx.validation_status:
        rows.append(DialogRow("Validation Status", ctx.validation_status))
    if ctx.validation_reason:
        rows.append(DialogRow("Validation Reason", ctx.validation_reason))
    return rows


def _build_session_diagnostics_actions(ctx: SessionDiagnosticsContext) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    _append_open_path(actions, "Open Session Dir", ctx.run_dir)
    _append_open_path(actions, "Open Session Settings", ctx.session_settings_path)
    _append_run_scoped_action(actions, ctx, action_type="open_agent_log", label="View Session Recording")
    _append_run_scoped_action(actions, ctx, action_type="copy_agent_log", label="Copy Session Recording")
    if ctx.claude_log_path:
        _append_run_scoped_action(actions, ctx, action_type="view_claude_log", label="View Claude Log")
        _append_open_path(actions, "Open Claude Log File", ctx.claude_log_path)
    _append_open_path(actions, "Open Claude Log Dir", ctx.claude_log_dir)
    _append_run_scoped_action(actions, ctx, action_type="open_orchestrator_log", label="Open Orchestrator Log")
    _append_open_path(actions, "Open Full Log", ctx.orchestrator_log)
    _append_open_path(actions, "Open Diagnostic", ctx.diagnostic_path)
    _append_open_path(actions, "Open Run Audit", ctx.run_audit_path)
    _append_open_path(actions, "Open Validation Record", ctx.validation_path)
    _append_open_path(actions, "Open Validation Output", ctx.validation_output_path)
    _append_open_path(actions, "Open Validation Stderr", ctx.validation_stderr_path)
    return actions


def _append_open_path(actions: list[dict[str, Any]], label: str, path: str) -> None:
    if not path:
        return
    actions.append({"type": "open_path", "label": label, "path": path})


def _append_run_scoped_action(
    actions: list[dict[str, Any]],
    ctx: SessionDiagnosticsContext,
    *,
    action_type: str,
    label: str,
) -> None:
    if not ctx.run_dir:
        return
    actions.append(
        {
            "type": action_type,
            "label": label,
            "issue_number": ctx.issue_number,
            "run_dir": ctx.run_dir,
        }
    )


def _format_extra_provider_args(raw: Any) -> str:
    if not isinstance(raw, dict) or not raw:
        return ""
    parts = [f"{key}={value}" for key, value in sorted(raw.items())]
    return ", ".join(parts)


def build_info_dialog(info: dict[str, Any]) -> dict[str, Any]:
    rows = [
        DialogRow("Version", info.get("version") or "dev"),
        DialogRow("Repository", info.get("repo") or ""),
        DialogRow("UI Mode", info.get("ui_mode") or ""),
        DialogRow("Terminal", info.get("terminal_backend") or ""),
        DialogRow("Commit", info.get("commit_short") or "unknown"),
        DialogRow("Max Sessions", str(info.get("max_sessions") or "-")),
        DialogRow("Active Sessions", str(info.get("active_sessions") or 0)),
        DialogRow("Completed Today", str(info.get("completed_today") or 0)),
    ]
    return {
        "title": "About Issue Orchestrator",
        "rows": [row.to_dict() for row in rows],
    }


def build_config_dialog(config_text: str) -> dict[str, Any]:
    return {
        "title": "Configuration",
        "config_text": config_text,
    }


def build_debug_dialog(debug_data: dict[str, Any]) -> dict[str, Any]:
    startup = debug_data.get("startup_options", {})
    filtering = startup.get("filtering", {})
    sections = [
        DialogSection(
            "Startup Options",
            [
                DialogRow("UI Mode", str(startup.get("ui_mode") or "-")),
                DialogRow("Web Port", str(startup.get("web_port") or "-")),
                DialogRow("Test Mode", "yes" if startup.get("test_mode") else "no"),
                DialogRow("Filter Label", str(filtering.get("label") or "none")),
                DialogRow("Filter Milestone", str(filtering.get("milestone") or "none")),
                DialogRow("Max Sessions", str(startup.get("max_sessions") or "-")),
            ],
        ),
        DialogSection(
            "State",
            [
                DialogRow("Paused", str(debug_data.get("paused"))),
                DialogRow(
                    "Priority Queue",
                    ", ".join(map(str, debug_data.get("priority_queue") or [])) or "empty",
                ),
            ],
        ),
        DialogSection(
            "Paths",
            [
                DialogRow("Config Path", str(debug_data.get("config_path") or "")),
                DialogRow("Repo Root", str(debug_data.get("repo_root") or "")),
            ],
        ),
    ]

    agents = debug_data.get("agents", {})
    if agents:
        sections.append(
            DialogSection(
                "Agent Types",
                [
                    DialogRow(name, f"timeout: {cfg.get('timeout')}m")
                    for name, cfg in agents.items()
                ],
            )
        )

    return {
        "title": "Debug Info",
        "sections": [section.to_dict() for section in sections],
    }


def build_doctor_dialog(doctor_data: dict[str, Any]) -> dict[str, Any]:
    checks = doctor_data.get("checks", [])
    return {
        "title": "Doctor",
        "overall": doctor_data.get("overall", "unknown"),
        "checks": [
            {
                "name": check.get("name"),
                "status": check.get("status"),
                "detail": check.get("detail"),
            }
            for check in checks
        ],
    }


def build_session_diagnostics_dialog(
    issue_number: int,
    manifest_payload: dict[str, Any],
) -> dict[str, Any]:
    ctx = SessionDiagnosticsContext.from_payload(issue_number, manifest_payload)
    analysis = SessionDiagnosticAnalysis.from_payload(manifest_payload.get("analysis"))
    rows = _build_session_diagnostics_rows(ctx)
    actions = _build_session_diagnostics_actions(ctx)

    return {
        "title": f"Session Diagnostics #{issue_number}",
        "rows": [row.to_dict() for row in rows],
        "actions": actions,
        "analysis": analysis.to_dict() if analysis else None,
    }


def build_blocked_issues_dialog(blocked_payload: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": "Blocked Issues",
        "blocked_issues": blocked_payload.get("blocked_issues", []),
    }


def _find_last_phase_with_prefix(phases: list[dict[str, Any]], prefix: str) -> dict[str, Any] | None:
    for phase in reversed(phases):
        if phase.get("name", "").startswith(prefix):
            return phase
    return None


def _select_phase(phases: list[dict[str, Any]], phase_key: str | None) -> dict[str, Any] | None:
    if phase_key in ("in_progress", "rework"):
        return _find_last_phase_with_prefix(phases, "coding-")
    if phase_key in ("review", "triage"):
        return _find_last_phase_with_prefix(phases, "review-")
    if phase_key:
        for phase in phases:
            if phase.get("name") == phase_key:
                return phase
    return None


def build_phase_dialog(phases_payload: dict[str, Any], issue_number: int, phase_key: str | None) -> dict[str, Any]:
    phases = phases_payload.get("phases", [])
    current = _select_phase(phases, phase_key)

    if current is None and phases:
        current = phases[-1]

    return {
        "title": current.get("display_name") if current else "Phase Details",
        "issue_number": issue_number,
        "phase": current,
        "phases": phases,
    }
