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

    supported_count = len(unique_types) - len(unsupported)

    if not unique_types:
        return Check(
            name="AI Agent Hooks (Installation)",
            status="warning",
            detail="No agents configured",
        ), False

    # Unsupported agents block launch unless dangerous mode allows them
    if unsupported and not config.dangerous.allow_unsupported_agents:
        return Check(
            name="AI Agent Hooks (Installation)",
            status="error",
            detail=(
                "Unsupported AI agents: "
                f"{', '.join(sorted(unsupported))}. "
                "Use Claude Code or set dangerous.allow_unsupported_agents: true"
            ),
        ), False

    if missing_hooks:
        return Check(
            name="AI Agent Hooks (Installation)",
            status="error",
            detail=(
                "Hooks not installed for: "
                f"{', '.join(sorted(missing_hooks))}. "
                "Run 'issue-orchestrator setup-hooks'"
            ),
        ), False

    # Unsupported agents allowed — warn but let supported agents proceed to verification
    if unsupported:
        return Check(
            name="AI Agent Hooks (Installation)",
            status="warning",
            detail=(
                f"{supported_count} supported agent(s) installed; "
                f"unsupported (allowed): {', '.join(sorted(unsupported))}"
            ),
        ), True  # hooks_ok=True so supported agents still get verified

    return Check(
        name="AI Agent Hooks (Installation)",
        status="ok",
        detail=f"{len(unique_types)} AI agent type(s) installed",
    ), True


def _check_full_verification(config: Config, unique_types: set, unsupported_types: set, hooks_ok: bool) -> Check:
    """Run full hook verification."""
    from ...hooks.hooks import get_adapter

    if not hooks_ok:
        return Check(
            name="AI Agent Hooks (Verification)",
            status="info",
            detail="Skipped because hooks are not installed or unsupported",
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
            name="AI Agent Hooks (Verification)",
            status="error",
            detail="; ".join(full_failures),
        )

    return Check(
        name="AI Agent Hooks (Verification)",
        status="ok",
        detail="All checks passed",
    )


def check_hook_verification(config: Config) -> list[Check]:
    from ...hooks.hooks import detect_agents_from_config, AiAgentType

    checks: list[Check] = []

    agent_types = detect_agents_from_config(config)
    unique_types = set(agent_types.values())

    # Only AIDER (no hook mechanism) and UNKNOWN are unsupported.
    # All others have adapters: Claude, Cursor, Gemini, Copilot, Codex
    unsupported_types = {
        AiAgentType.UNKNOWN,
        AiAgentType.AIDER,
    }

    # Check hook installation
    install_check, hooks_ok = _check_hook_installation(config, unique_types, unsupported_types)
    checks.append(install_check)

    # Run full verification (gated on installation success)
    full_check = _check_full_verification(config, unique_types, unsupported_types, hooks_ok)
    checks.append(full_check)

    return checks
