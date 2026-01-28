"""Hook verification checks for doctor."""

import logging

from ..types import Check
from ...config import Config
from ...hooks.hooks import get_adapter
from ...safety_state import load_safety_state, save_safety_state

logger = logging.getLogger(__name__)


def _check_hook_installation(config: Config, unique_types: set, unsupported_types: set) -> tuple[Check, bool]:
    """Check if hooks are installed. Returns (check, hooks_ok)."""
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


def _get_unsupported_types(unique_types: set) -> set:
    """Determine which agent types are unsupported by querying adapters.

    Rather than maintaining a hardcoded list, we ask the adapter system
    directly - if get_adapter returns an UnsupportedAdapter, it's unsupported.
    """
    from ...hooks.hooks import get_adapter, UnsupportedAdapter

    unsupported = set()
    for agent_type in unique_types:
        adapter = get_adapter(agent_type)
        if isinstance(adapter, UnsupportedAdapter):
            unsupported.add(agent_type)
    return unsupported


def _check_safety_report(
    config: Config,
    unique_types: set,
    unsupported_types: set,
    hooks_ok: bool,
) -> Check | None:
    """Check if safety check is stale and run live verification if needed.

    Returns a Check with expandable details showing what was tested and results,
    or None if safety check is disabled.
    """
    # Check if safety check is disabled
    interval_days = config.hooks.safety_check.interval_days
    if interval_days <= 0:
        return None  # Disabled, don't show in doctor

    # Load current state
    state = load_safety_state(config.repo_root)

    # Determine if check is stale
    is_stale = state.is_stale(interval_days)

    # Prepare expandable details
    expandable: dict = {
        "ran": False,
        "triggered_by": None,
        "agents_tested": [],
        "results": {},
        "last_check": state.last_check.isoformat() if state.last_check else None,
    }

    if not hooks_ok:
        return Check(
            name="Safety Check",
            status="info",
            detail="Skipped - hooks not installed",
            expandable=expandable,
        )

    if not is_stale:
        # Show previous results
        for agent_type, result in state.last_results.items():
            expandable["results"][agent_type] = {
                "success": result.success,
                "message": result.message,
            }
        days_ago = (
            (state.last_check.date() - state.last_check.date()).days
            if state.last_check else 0
        )
        return Check(
            name="Safety Check",
            status="ok",
            detail=f"Passed (last check {days_ago}d ago)",
            expandable=expandable,
        )

    # Need to run live verification
    expandable["ran"] = True
    expandable["triggered_by"] = "first run" if state.last_check is None else "interval exceeded"

    results: dict[str, tuple[bool, str]] = {}
    failures: list[str] = []

    for agent_type in unique_types:
        if agent_type in unsupported_types:
            continue

        agent_name = agent_type.value
        expandable["agents_tested"].append(agent_name)

        adapter = get_adapter(agent_type)
        try:
            success, message = adapter.live_verify(config.repo_root)
            results[agent_name] = (success, message)
            expandable["results"][agent_name] = {"success": success, "message": message}

            if not success:
                failures.append(f"{agent_name}: {message[:50]}")
        except Exception as e:
            error_msg = f"Error: {e}"
            results[agent_name] = (False, error_msg)
            expandable["results"][agent_name] = {"success": False, "message": error_msg}
            failures.append(f"{agent_name}: {error_msg[:50]}")
            logger.warning("Live verification failed for %s: %s", agent_name, e)

    # Save state
    state.mark_checked(results)
    save_safety_state(config.repo_root, state)

    # Determine status based on results and dangerous_allow_failure
    if failures:
        if config.hooks.safety_check.dangerous_allow_failure:
            return Check(
                name="Safety Check",
                status="warning",
                detail=f"Failed ({len(failures)} agent(s)) - allowed by config",
                expandable=expandable,
            )
        else:
            return Check(
                name="Safety Check",
                status="error",
                detail=f"Failed: {'; '.join(failures)}",
                expandable=expandable,
            )

    return Check(
        name="Safety Check",
        status="ok",
        detail=f"Passed ({len(results)} agent(s) verified)",
        expandable=expandable,
    )


def check_hook_verification(config: Config) -> list[Check]:
    from ...hooks.hooks import detect_agents_from_config

    checks: list[Check] = []

    agent_types = detect_agents_from_config(config)
    unique_types = set(agent_types.values())

    # Ask the adapter system what's unsupported (no hardcoded list)
    unsupported_types = _get_unsupported_types(unique_types)

    # Check hook installation
    install_check, hooks_ok = _check_hook_installation(config, unique_types, unsupported_types)
    checks.append(install_check)

    # Run full verification (gated on installation success)
    full_check = _check_full_verification(config, unique_types, unsupported_types, hooks_ok)
    checks.append(full_check)

    # Run safety check (live verification with state persistence)
    safety_check = _check_safety_report(config, unique_types, unsupported_types, hooks_ok)
    if safety_check:
        checks.append(safety_check)

    return checks
