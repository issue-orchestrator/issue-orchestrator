"""MCP review exchange probes and scheduling."""

from __future__ import annotations

from datetime import timedelta
from typing import Optional

from ..ports.command_runner import CommandRunner
from ..ports.session_log import detect_ai_system_from_command
from .ai_systems_config import get_ai_systems_config
from .doctor.types import Check
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

    if runner is None:
        checks.append(Check(
            name="Review Exchange",
            status="info",
            detail="MCP probe skipped (no CommandRunner provided)",
        ))
        return checks

    state = load_state(config.repo_root)
    skip_check = _probe_schedule_guard(config, state, force)
    if skip_check is not None:
        return [skip_check]

    pairs = _exchange_pairs(config)
    if not pairs:
        checks.append(Check(
            name="Review Exchange",
            status="info",
            detail="MCP probe skipped (no review exchange pairs configured)",
        ))
        return checks

    systems = {
        _resolve_ai_system(config, label)
        for pair in pairs
        for label in pair
    }
    checks.extend(_probe_mcp_systems_from_set(systems, runner))
    checks.extend(_probe_mcp_round_trip_from_pairs(pairs, config, mode, runner))

    _update_probe_state(config, state, checks)

    return checks


def _resolve_ai_system(config, label: str) -> str:
    agent = config.agents[label]
    if agent.ai_system:
        return agent.ai_system
    detected = detect_ai_system_from_command(agent.command)
    if detected:
        return detected
    systems = get_ai_systems_config(config.repo_root)
    return systems.default_ai_system


def _exchange_pairs(config) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for label, agent in config.agents.items():
        if config.tech_lead_review_agent and label == config.tech_lead_review_agent:
            continue
        if agent.skip_review:
            continue
        reviewer_label = config.get_reviewer_for_agent(label)
        if not reviewer_label:
            continue
        if reviewer_label not in config.agents:
            continue
        pairs.append((label, reviewer_label))
    return pairs


def _probe_schedule_guard(config, state, force: bool) -> Optional[Check]:
    schedule = config.review_exchange_probe_schedule
    interval_days = config.review_exchange_probe_interval_days
    if schedule == "manual" and not force:
        return Check(
            name="Review Exchange",
            status="info",
            detail="MCP probe skipped (manual schedule)",
        )

    if force:
        return None

    if schedule == "startup":
        return None

    if schedule == "daily" and not state.is_stale(timedelta(days=1)):
        return Check(
            name="Review Exchange",
            status="info",
            detail="MCP round-trip probe skipped (recently checked)",
        )

    if schedule == "interval" and not state.is_stale(timedelta(days=interval_days)):
        return Check(
            name="Review Exchange",
            status="info",
            detail="MCP round-trip probe skipped (recently checked)",
        )

    return None


def _update_probe_state(config, state, checks: list[Check]) -> None:
    summary = {
        check.name: (check.status == "ok", check.detail)
        for check in checks
        if check.name.startswith("MCP Round-trip")
    }
    if summary:
        state.mark_checked(summary)
        save_state(config.repo_root, state)


def _probe_mcp_systems_from_set(
    systems: set[str],
    runner: CommandRunner,
) -> list[Check]:
    """Run a lightweight MCP probe for involved systems."""
    checks: list[Check] = []
    for system in sorted(systems):
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


def _probe_mcp_round_trip_from_pairs(
    pairs: list[tuple[str, str]],
    config,
    mode: str,
    runner: CommandRunner,
) -> list[Check]:
    """Run a minimal MCP round-trip probe for supported pairs."""
    checks: list[Check] = []

    from .review_exchange_registry import SUPPORTED_MCP_PAIRS

    system_pairs = [
        (_resolve_ai_system(config, coder), _resolve_ai_system(config, reviewer))
        for coder, reviewer in pairs
    ]
    supported_pairs = [pair for pair in system_pairs if pair in SUPPORTED_MCP_PAIRS]
    if not supported_pairs:
        status = "warning" if mode == "via-mcp" else "info"
        checks.append(Check(
            name="MCP Round-trip",
            status=status,
            detail="MCP round-trip skipped (no supported MCP pairs configured)",
        ))
        return checks

    if not any(set(pair) == {"claude-code", "codex"} for pair in supported_pairs):
        status = "warning" if mode == "via-mcp" else "info"
        checks.append(Check(
            name="MCP Round-trip",
            status=status,
            detail="MCP round-trip skipped (no claude-code↔codex pairs configured)",
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
