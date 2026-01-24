"""Hook verification checks for doctor."""

from ..types import Check
from ...config import Config


def _check_hook_installation(config: Config, unique_types: set, unsupported_types: set) -> tuple[Check, bool]:
    """Check if hooks are installed. Returns (check, hooks_ok)."""
    from ...hooks.hooks import get_adapter

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
        return Check(
            name="Meta-Agent Hooks (Installation)",
            status="warning",
            detail="No agents configured",
        ), False

    if unsupported:
        status = "warning" if config.dangerous.allow_unsupported_agents else "error"
        return Check(
            name="Meta-Agent Hooks (Installation)",
            status=status,
            detail=(
                "Unsupported meta-agents: "
                f"{', '.join(sorted(unsupported))}. "
                "Use Claude Code or set dangerous.allow_unsupported_agents: true"
            ),
        ), False

    if missing_hooks:
        return Check(
            name="Meta-Agent Hooks (Installation)",
            status="error",
            detail=(
                "Hooks not installed for: "
                f"{', '.join(sorted(missing_hooks))}. "
                "Run 'issue-orchestrator setup-hooks'"
            ),
        ), False

    return Check(
        name="Meta-Agent Hooks (Installation)",
        status="ok",
        detail=f"{len(unique_types)} meta-agent type(s) installed",
    ), True


def _check_cached_verification(config: Config, hooks_ok: bool) -> tuple[Check, bool]:
    """Check cached verification status. Returns (check, cached_ok)."""
    from ...hooks.hooks import check_verification_status

    if not hooks_ok:
        return Check(
            name="Meta-Agent Hooks (Cached)",
            status="info",
            detail="Skipped because hooks are not installed or unsupported",
        ), False

    is_valid, status_msg = check_verification_status(config.repo_root, config)
    if is_valid:
        return Check(
            name="Meta-Agent Hooks (Cached)",
            status="ok",
            detail=status_msg,
        ), True

    return Check(
        name="Meta-Agent Hooks (Cached)",
        status="warning",
        detail=(
            f"{status_msg} "
            "Run 'issue-orchestrator verify' to refresh or "
            "'issue-orchestrator setup-hooks' to reinstall."
        ),
    ), False


def _check_full_verification(config: Config, unique_types: set, unsupported_types: set, cached_ok: bool) -> Check:
    """Run full hook verification."""
    from ...hooks.hooks import get_adapter

    if not cached_ok:
        return Check(
            name="Meta-Agent Hooks (Full)",
            status="info",
            detail="Skipped because cached verification is not valid",
        )

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
        return Check(
            name="Meta-Agent Hooks (Full)",
            status="error",
            detail="; ".join(full_failures),
        )

    return Check(
        name="Meta-Agent Hooks (Full)",
        status="ok",
        detail="All checks passed",
    )


def check_hook_verification(config: Config) -> list[Check]:
    from ...hooks.hooks import detect_agents_from_config, MetaAgentType

    checks: list[Check] = []

    agent_types = detect_agents_from_config(config)
    unique_types = set(agent_types.values())

    unsupported_types = {
        MetaAgentType.UNKNOWN,
        MetaAgentType.CURSOR,
        MetaAgentType.COPILOT,
        MetaAgentType.CODEX,
        MetaAgentType.AIDER,
        MetaAgentType.GEMINI,
    }

    # Check hook installation
    install_check, hooks_ok = _check_hook_installation(config, unique_types, unsupported_types)
    checks.append(install_check)

    # Check cached verification
    cached_check, cached_ok = _check_cached_verification(config, hooks_ok)
    checks.append(cached_check)

    # Run full verification
    full_check = _check_full_verification(config, unique_types, unsupported_types, cached_ok)
    checks.append(full_check)

    return checks
