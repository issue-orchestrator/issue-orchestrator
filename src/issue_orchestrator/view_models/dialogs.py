"""Dialog view models for the web UI."""

from __future__ import annotations

from dataclasses import dataclass
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
    session_prompt_path: str

    @classmethod
    def from_payload(
        cls,
        issue_number: int,
        manifest_payload: dict[str, Any],
    ) -> "SessionDiagnosticsContext":
        manifest = manifest_payload.get("manifest") or {}
        worktree = str(manifest.get("worktree") or "")
        session_name = str(manifest.get("session_name") or manifest_payload.get("session_name") or "")
        diagnostic_path = _join_worktree_path(worktree, manifest.get("diagnostic_path"))
        validation_path = _join_worktree_path(worktree, manifest.get("validation_record_path"))
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
            session_prompt_path=str(manifest.get("session_prompt_path") or ""),
        )


def _join_worktree_path(worktree: str, rel_path: Any) -> str:
    """Join worktree + manifest-relative path when both are present."""
    if not worktree:
        return ""
    rel_value = str(rel_path or "")
    if not rel_value:
        return ""
    return f"{worktree}/{rel_value}"


def _build_session_diagnostics_rows(ctx: SessionDiagnosticsContext) -> list[DialogRow]:
    return [
        DialogRow("Session", ctx.session_name or "-"),
        DialogRow("Started", ctx.started_at or "-"),
        DialogRow("Run ID", ctx.run_id or "-"),
        DialogRow("Backend", ctx.backend or "-"),
        DialogRow("Agent", ctx.agent_label or "-"),
        DialogRow("Claude Session", ctx.claude_session_id or "-"),
        DialogRow("Retention Tier", ctx.retention_tier or "-"),
        DialogRow("Retention Expires", ctx.retention_expires_at or "-"),
        DialogRow("Retention Pinned", ctx.retention_pinned or "-"),
        DialogRow("Worktree", ctx.worktree or "-"),
    ]


def _build_session_diagnostics_actions(ctx: SessionDiagnosticsContext) -> list[dict[str, Any]]:
    actions: list[dict[str, Any]] = []
    if ctx.run_dir:
        actions.append({"type": "open_path", "label": "Open Session Dir", "path": ctx.run_dir})

    actions.append(
        {
            "type": "open_agent_log",
            "label": "UI Log",
            "issue_number": ctx.issue_number,
            "run_dir": ctx.run_dir,
        }
    )

    if ctx.claude_log_path:
        actions.append({"type": "view_claude_log", "label": "View Claude Log", "issue_number": ctx.issue_number})
        actions.append({
            "type": "open_path",
            "label": "Open Claude Log File",
            "path": ctx.claude_log_path,
        })
    if ctx.claude_log_dir:
        actions.append({
            "type": "open_path",
            "label": "Open Claude Log Dir",
            "path": ctx.claude_log_dir,
        })

    actions.append({"type": "open_orchestrator_log", "label": "Open Orchestrator Log", "issue_number": ctx.issue_number})

    if ctx.orchestrator_log:
        actions.append({
            "type": "open_path",
            "label": "Open Full Log",
            "path": ctx.orchestrator_log,
        })

    if ctx.diagnostic_path:
        actions.append({"type": "open_path", "label": "Open Diagnostic", "path": ctx.diagnostic_path})

    if ctx.validation_path:
        actions.append({
            "type": "open_path",
            "label": "Open Validation",
            "path": ctx.validation_path,
        })
    if ctx.session_prompt_path:
        actions.append({
            "type": "view_session_prompt",
            "label": "View Prompt",
            "issue_number": ctx.issue_number,
            "run_dir": ctx.run_dir,
        })
    return actions


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
    rows = _build_session_diagnostics_rows(ctx)
    actions = _build_session_diagnostics_actions(ctx)

    return {
        "title": f"Session Diagnostics #{issue_number}",
        "rows": [row.to_dict() for row in rows],
        "actions": actions,
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
