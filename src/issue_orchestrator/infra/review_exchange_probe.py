"""MCP review exchange probes and scheduling."""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

from ..ports.command_runner import CommandRunner
from ..ports.session_log import detect_ai_system_from_command
from .ai_systems_config import get_ai_systems_config
from .doctor.types import Check
from .review_exchange_registry import supports_mcp_pair
from .review_exchange_state import load_state, save_state


def probe_review_exchange(
    config,
    runner: Optional[CommandRunner],
    *,
    force: bool = False,
) -> list[Check]:
    """Run MCP probes for the configured review exchange.

    Args:
        config: Orchestrator Config
        runner: CommandRunner (required for probes)
        force: If True, bypass schedule gating
    """
    checks: list[Check] = []

    if not config.review_enabled:
        return checks

    mode = config.review_exchange_mode
    if mode not in {"via-mcp", "auto"}:
        return checks

    coder_label = config.review_exchange_coder
    reviewer_label = config.review_exchange_reviewer
    if not coder_label or not reviewer_label:
        checks.append(Check(
            name="Review Exchange",
            status="error",
            detail="review.exchange.agent_pair is required for via-mcp/auto",
        ))
        return checks

    def resolve_system(label: str) -> str:
        agent = config.agents[label]
        if agent.ai_system:
            return agent.ai_system
        detected = detect_ai_system_from_command(agent.command)
        if detected:
            return detected
        systems = get_ai_systems_config(config.repo_root)
        return systems.default_ai_system

    coder_system = resolve_system(coder_label)
    reviewer_system = resolve_system(reviewer_label)

    if not supports_mcp_pair(coder_system, reviewer_system):
        status = "error" if mode == "via-mcp" else "warning"
        checks.append(Check(
            name="Review Exchange",
            status=status,
            detail=(
                "Unsupported MCP pair "
                f"{coder_label}({coder_system}) -> {reviewer_label}({reviewer_system}). "
                "Use via-draft-pr or update the MCP allowlist."
            ),
        ))
        return checks

    if runner is None:
        checks.append(Check(
            name="Review Exchange",
            status="info",
            detail="MCP probe skipped (no CommandRunner provided)",
        ))
        return checks

    schedule = config.review_exchange_probe_schedule
    interval_days = config.review_exchange_probe_interval_days
    if schedule == "manual" and not force:
        checks.append(Check(
            name="Review Exchange",
            status="info",
            detail="MCP probe skipped (manual schedule)",
        ))
        return checks

    state = load_state(config.repo_root)
    if not force:
        if schedule == "startup":
            pass
        elif schedule == "daily":
            if not state.is_stale(timedelta(days=1)):
                checks.append(Check(
                    name="Review Exchange",
                    status="info",
                    detail="MCP round-trip probe skipped (recently checked)",
                ))
                return checks
        elif schedule == "interval":
            if not state.is_stale(timedelta(days=interval_days)):
                checks.append(Check(
                    name="Review Exchange",
                    status="info",
                    detail="MCP round-trip probe skipped (recently checked)",
                ))
                return checks

    checks.extend(_probe_mcp_systems(coder_system, reviewer_system, runner))
    checks.extend(_probe_mcp_round_trip(coder_system, reviewer_system, runner))

    summary = {
        check.name: (check.status == "ok", check.detail)
        for check in checks
        if check.name.startswith("MCP Round-trip")
    }
    if summary:
        state.mark_checked(summary)
        save_state(config.repo_root, state)

    return checks


def _probe_mcp_systems(
    coder_system: str,
    reviewer_system: str,
    runner: CommandRunner,
) -> list[Check]:
    """Run a lightweight MCP probe for involved systems."""
    checks: list[Check] = []
    systems: list[str] = []
    for system in (coder_system, reviewer_system):
        if system not in systems:
            systems.append(system)
    for system in systems:
        if system == "claude-code":
            checks.extend(_probe_claude_mcp(runner))
        elif system == "codex":
            checks.extend(_probe_codex_cli(runner))
        else:
            checks.append(Check(
                name=f"MCP Probe ({system})",
                status="warning",
                detail="No MCP probe implemented for this AI system",
            ))
    return checks


def _probe_mcp_round_trip(
    coder_system: str,
    reviewer_system: str,
    runner: CommandRunner,
) -> list[Check]:
    """Run a minimal MCP round-trip probe for supported pairs."""
    checks: list[Check] = []

    supports_round_trip = {coder_system, reviewer_system} == {"claude-code", "codex"}
    if not supports_round_trip:
        checks.append(Check(
            name="MCP Round-trip",
            status="warning",
            detail="Round-trip probe not implemented for this pair",
        ))
        return checks

    prompt = (
        "Call the MCP tool `codex` and reply with exactly one line of JSON: "
        "{\"ok\":true}. Do not include any other text."
    )
    result = runner.run(
        ["claude", "--permission-mode", "bypassPermissions", "-p", prompt],
        timeout_seconds=30,
    )
    if result.returncode != 0:
        checks.append(Check(
            name="MCP Round-trip",
            status="error",
            detail=f"Round-trip failed: {result.stderr[:200]}",
        ))
        return checks

    if '{"ok":true}' not in result.stdout.replace(" ", ""):
        checks.append(Check(
            name="MCP Round-trip",
            status="error",
            detail=f"Unexpected response: {result.stdout[:200]}",
        ))
        return checks

    checks.append(Check(
        name="MCP Round-trip",
        status="ok",
        detail="MCP round-trip succeeded",
    ))
    return checks


def _probe_claude_mcp(runner: CommandRunner) -> list[Check]:
    checks: list[Check] = []
    version = runner.run(["claude", "--version"], timeout_seconds=5)
    if version.returncode != 0:
        checks.append(Check(
            name="MCP Probe (claude-code)",
            status="error",
            detail=f"claude not available: {version.stderr[:120]}",
        ))
        return checks

    mcp_list = runner.run(["claude", "mcp", "list"], timeout_seconds=10)
    if mcp_list.returncode != 0:
        checks.append(Check(
            name="MCP Probe (claude-code)",
            status="error",
            detail=f"claude mcp list failed: {mcp_list.stderr[:120]}",
        ))
        return checks

    checks.append(Check(
        name="MCP Probe (claude-code)",
        status="ok",
        detail="claude CLI and MCP list OK",
    ))
    return checks


def _probe_codex_cli(runner: CommandRunner) -> list[Check]:
    result = runner.run(["codex", "--version"], timeout_seconds=5)
    if result.returncode != 0:
        return [Check(
            name="MCP Probe (codex)",
            status="error",
            detail=f"codex not available: {result.stderr[:120]}",
        )]
    return [Check(
        name="MCP Probe (codex)",
        status="ok",
        detail="codex CLI OK",
    )]
