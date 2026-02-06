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
    manifest = manifest_payload.get("manifest") or {}
    worktree = manifest.get("worktree") or ""
    run_dir = manifest.get("run_dir") or manifest_payload.get("run_dir") or ""
    diagnostic_rel = manifest.get("diagnostic_path") or ""
    diagnostic_path = f"{worktree}/{diagnostic_rel}" if worktree and diagnostic_rel else ""

    rows = [
        DialogRow("Session", str(manifest.get("session_name") or manifest_payload.get("session_name") or "-")),
        DialogRow("Started", str(manifest.get("started_at") or "-")),
        DialogRow("Run ID", str(manifest.get("run_id") or "-")),
        DialogRow("Backend", str(manifest.get("backend") or "-")),
        DialogRow("Agent", str(manifest.get("agent_label") or "-")),
        DialogRow("Claude Session", str(manifest.get("claude_session_id") or "-")),
        DialogRow("Worktree", worktree or "-"),
    ]

    actions: list[dict[str, Any]] = []
    if run_dir:
        actions.append({"type": "open_path", "label": "Open Session Dir", "path": run_dir})

    actions.append({"type": "open_agent_log", "label": "View Session Log", "issue_number": issue_number})

    if manifest.get("claude_log_path"):
        actions.append({"type": "view_claude_log", "label": "View Claude Log", "issue_number": issue_number})
        actions.append({
            "type": "open_path",
            "label": "Open Claude Log File",
            "path": manifest.get("claude_log_path"),
        })
    if manifest.get("claude_log_dir"):
        actions.append({
            "type": "open_path",
            "label": "Open Claude Log Dir",
            "path": manifest.get("claude_log_dir"),
        })

    actions.append({"type": "open_orchestrator_log", "label": "Open Orchestrator Log", "issue_number": issue_number})

    if manifest.get("orchestrator_log"):
        actions.append({
            "type": "open_path",
            "label": "Open Full Log",
            "path": manifest.get("orchestrator_log"),
        })

    if diagnostic_path:
        actions.append({"type": "open_path", "label": "Open Diagnostic", "path": diagnostic_path})

    if manifest.get("validation_record_path") and worktree:
        actions.append({
            "type": "open_path",
            "label": "Open Validation",
            "path": f"{worktree}/{manifest.get('validation_record_path')}",
        })

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
