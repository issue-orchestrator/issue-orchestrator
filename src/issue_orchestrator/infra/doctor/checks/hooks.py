"""Hook verification checks for doctor."""

from ..types import Check
from ...config import Config


def check_hook_verification(config: Config) -> list[Check]:
    from ...hooks.hooks import (
        detect_agents_from_config,
        get_adapter,
        check_verification_status,
        MetaAgentType,
    )

    checks: list[Check] = []

    agent_types = detect_agents_from_config(config)
    unique_types = set(agent_types.values())

    unsupported_types = {
        MetaAgentType.UNKNOWN,
        MetaAgentType.CURSOR,
        MetaAgentType.COPILOT_CLI,
        MetaAgentType.CODEX_CLI,
        MetaAgentType.AIDER,
        MetaAgentType.GEMINI_CLI,
    }

    missing_hooks = []
    unsupported = []
    for agent_type in unique_types:
        if agent_type in unsupported_types:
            unsupported.append(agent_type.value)
            continue
        adapter = get_adapter(agent_type)
        if not adapter.is_installed(config.repo_root):
            missing_hooks.append(agent_type.value)

    if not unique_types:
        checks.append(Check(
            name="Meta-Agent Hooks (Installation)",
            status="warning",
            detail="No agents configured",
        ))
        hooks_ok = False
    elif unsupported:
        status = "warning" if config.dangerous.allow_unsupported_agents else "error"
        checks.append(Check(
            name="Meta-Agent Hooks (Installation)",
            status=status,
            detail=(
                "Unsupported meta-agents: "
                f"{', '.join(sorted(unsupported))}. "
                "Use Claude Code or set dangerous.allow_unsupported_agents: true"
            ),
        ))
        hooks_ok = False
    elif missing_hooks:
        checks.append(Check(
            name="Meta-Agent Hooks (Installation)",
            status="error",
            detail=(
                "Hooks not installed for: "
                f"{', '.join(sorted(missing_hooks))}. "
                "Run 'issue-orchestrator setup-hooks'"
            ),
        ))
        hooks_ok = False
    else:
        checks.append(Check(
            name="Meta-Agent Hooks (Installation)",
            status="ok",
            detail=f"{len(unique_types)} meta-agent type(s) installed",
        ))
        hooks_ok = True

    cached_ok = False
    if hooks_ok:
        is_valid, status_msg = check_verification_status(config.repo_root, config)
        if is_valid:
            checks.append(Check(
                name="Meta-Agent Hooks (Cached)",
                status="ok",
                detail=status_msg,
            ))
            cached_ok = True
        else:
            checks.append(Check(
                name="Meta-Agent Hooks (Cached)",
                status="warning",
                detail=(
                    f"{status_msg} "
                    "Run 'issue-orchestrator verify' to refresh or "
                    "'issue-orchestrator setup-hooks' to reinstall."
                ),
            ))
    else:
        checks.append(Check(
            name="Meta-Agent Hooks (Cached)",
            status="info",
            detail="Skipped because hooks are not installed or unsupported",
        ))

    if cached_ok:
        full_failures = []
        for agent_type in unique_types:
            if agent_type in unsupported_types:
                continue
            adapter = get_adapter(agent_type)
            result_obj = adapter.verify_hooks(config.repo_root)
            if not result_obj.success:
                full_failures.append(
                    f"{agent_type.value}: {', '.join(result_obj.checks_failed[:3])}"
                )
        if full_failures:
            checks.append(Check(
                name="Meta-Agent Hooks (Full)",
                status="error",
                detail="; ".join(full_failures),
            ))
        else:
            checks.append(Check(
                name="Meta-Agent Hooks (Full)",
                status="ok",
                detail="All checks passed",
            ))
    else:
        checks.append(Check(
            name="Meta-Agent Hooks (Full)",
            status="info",
            detail="Skipped because cached verification is not valid",
        ))

    return checks
