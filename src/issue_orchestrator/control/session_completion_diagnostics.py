"""Diagnostics emitted during session completion handling."""

import logging
from pathlib import Path

from ..domain.models import Session, SessionStatus

logger = logging.getLogger(__name__)


def run_session_analysis(run_dir: Path) -> None:
    """Run the session analyzer and write analysis.json (best-effort)."""
    from ..domain.run_manifest import RunManifest
    from .session_analyzer import analyze, write_analysis

    try:
        manifest = RunManifest.load(run_dir)
        analysis = analyze(manifest)
        write_analysis(run_dir, analysis)
        logger.info("[ANALYSIS] %s — %s", run_dir.name, analysis.headline[:80])
    except FileNotFoundError:
        logger.debug("[ANALYSIS] No manifest in %s — skipping analysis", run_dir.name)
    except Exception:
        logger.warning("[ANALYSIS] Failed to analyze %s", run_dir.name, exc_info=True)


def surface_failure_context(session: Session, status: SessionStatus) -> None:
    """Surface AI session logs when a session fails.

    Extracts and logs relevant failure context from the AI system's logs to help
    users understand why a session failed.
    """
    try:
        from ..adapters.session_log.registry import get_log_provider
        from ..ports.session_log import detect_ai_system_from_command

        ai_system = detect_ai_system_from_command(session.agent_config.command) or "claude-code"
        provider = get_log_provider(ai_system)

        diag_lines = [
            f"## Session Failure Diagnostic for Issue #{session.issue.number}",
            f"- Status: {status.value}",
            f"- Agent: {session.agent_label or 'unknown'}",
            f"- AI System: {ai_system}",
            f"- Permission Mode: {session.agent_config.effective_permission_mode}",
            f"- Worktree: {session.worktree_path}",
            f"- Runtime: {session.runtime_minutes} minutes",
        ]

        if session.agent_config.effective_permission_mode == "default":
            diag_lines.append("")
            diag_lines.append("⚠️  WARNING: permission_mode is 'default' - Claude will prompt for permissions!")
            diag_lines.append("   This causes sessions to hang/fail in non-interactive mode.")
            diag_lines.append("   FIX: Add 'permission_mode: bypassPermissions' to your agent config in YAML.")

        context = None
        if provider:
            log_path = provider.get_log_path(session.worktree_path, session.terminal_id)
            if log_path:
                diag_lines.append(f"- Log file: {log_path}")
                context = provider.get_failure_context(log_path)
            else:
                diag_lines.append("- Log file: NOT FOUND (check ~/.claude/projects/)")

        if context:
            diag_lines.append("")
            diag_lines.append(context)
        else:
            diag_lines.append("")
            diag_lines.append("No detailed failure context available from AI logs.")

        diag_lines.append("")
        diag_lines.append("## Next Steps")
        diag_lines.append("1. Check the log file above for errors")
        diag_lines.append("2. Run: grep '[FAILURE_CONTEXT]' ~/.issue-orchestrator.log")
        diag_lines.append("3. See troubleshooting docs: /troubleshooting skill")

        logger.warning(
            "[FAILURE_CONTEXT] Issue #%d (%s):\n%s",
            session.issue.number,
            status.value,
            "\n".join(diag_lines),
        )

    except Exception as e:
        logger.warning("[FAILURE_CONTEXT] Could not extract failure context for #%d: %s", session.issue.number, e)
