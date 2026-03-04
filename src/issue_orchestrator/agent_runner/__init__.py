"""Facade for agent_runner subsystem.

Exports unified types from ``execution/`` and backward-compatible aliases.

The unified AgentRunner (pexpect-based) and SubprocessAgentRunner (Popen-based)
are the two canonical runners.  All new code should use AgentSpec/AgentResult.

All exports are lazy-loaded to avoid creating transitive import chains that
would violate architectural contracts (e.g. domain → agent_runner → execution).
"""

from importlib import import_module
from typing import Any

_TYPES_MODULE = "issue_orchestrator.execution.agent_runner_types"
_BASE_MODULE = "issue_orchestrator.execution.agent_runner_base"
_PTY_MODULE = "issue_orchestrator.execution.agent_runner"
_SUBPROCESS_MODULE = "issue_orchestrator.execution.subprocess_runner"
_ENV_MODULE = "issue_orchestrator.execution.agent_runner_env"
_ERRORS_MODULE = "issue_orchestrator.execution.agent_runner_errors"
_VENDOR_PROVIDERS_MODULE = "issue_orchestrator._vendor.agent_runner.providers"

# Map export name → module that provides it
_EXPORT_MAP: dict[str, str] = {
    # Shared types (agent_runner_types)
    "AgentResult": _TYPES_MODULE,
    "AgentSpec": _TYPES_MODULE,
    "RetryPolicy": _TYPES_MODULE,
    # Base class
    "BaseAgentRunner": _BASE_MODULE,
    # PTY runner (pexpect)
    "AgentRunner": _PTY_MODULE,
    "AgentSession": _PTY_MODULE,
    # Subprocess runner (Popen)
    "SubprocessAgentRunner": _SUBPROCESS_MODULE,
    # Env filtering
    "build_filtered_env": _ENV_MODULE,
    "DEFAULT_FORBIDDEN_ENV_VARS": _ENV_MODULE,
    "GIT_SAFE_ENV": _ENV_MODULE,
    "get_forbidden_env_vars": _ENV_MODULE,
    "verify_env_scrubbed": _ENV_MODULE,
    "all_env_scrubbed": _ENV_MODULE,
    # Error classification
    "ProviderErrorType": _ERRORS_MODULE,
    "classify_provider_error": _ERRORS_MODULE,
    # Provider registry (still in _vendor for now — providers are unchanged)
    "AIProvider": _VENDOR_PROVIDERS_MODULE,
    "list_providers": _VENDOR_PROVIDERS_MODULE,
    "get_provider": _VENDOR_PROVIDERS_MODULE,
    "is_valid_provider": _VENDOR_PROVIDERS_MODULE,
}

# Backward-compatible aliases: old vendored names → unified types
_ALIASES: dict[str, str] = {
    "RunSpec": "AgentSpec",
    "RunResult": "AgentResult",
}


def __getattr__(name: str) -> Any:
    # Direct exports
    if name in _EXPORT_MAP:
        mod = import_module(_EXPORT_MAP[name])
        return getattr(mod, name)
    # Aliases (RunSpec → AgentSpec, etc.)
    if name in _ALIASES:
        canonical = _ALIASES[name]
        mod = import_module(_EXPORT_MAP[canonical])
        return getattr(mod, canonical)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(
        list(globals().keys()) + list(_EXPORT_MAP.keys()) + list(_ALIASES.keys())
    )
