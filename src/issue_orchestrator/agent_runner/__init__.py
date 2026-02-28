"""Facade for agent_runner subsystem.

Exports both the vendored types (RunSpec, RunResult — used by provider_runner)
and the unified types (AgentSpec, AgentResult, AgentSession — used everywhere else).

The unified AgentRunner in ``execution.agent_runner`` is the canonical way to
spawn agent processes.  All new code should use AgentSpec/AgentResult.

All exports are lazy-loaded to avoid creating transitive import chains that
would violate architectural contracts (e.g. domain → agent_runner → execution).
"""

from importlib import import_module
from typing import Any

_UNIFIED_MODULE = "issue_orchestrator.execution.agent_runner"
_VENDOR_MODULE = "issue_orchestrator._vendor.agent_runner"

# Unified types — the preferred API for new code.
_UNIFIED_EXPORTS = {
    "AgentResult",
    "AgentRunner",
    "AgentSession",
    "AgentSpec",
    "RetryPolicy",
}

# Vendored types — kept for backward compatibility (provider_runner).
_VENDOR_EXPORTS = {
    "AIProvider",
    "RunSpec",
    "RunResult",
    "ProviderErrorType",
    "classify_provider_error",
    "list_providers",
    "get_provider",
    "is_valid_provider",
    "__version__",
}


def __getattr__(name: str) -> Any:
    if name in _UNIFIED_EXPORTS:
        unified = import_module(_UNIFIED_MODULE)
        return getattr(unified, name)
    if name in _VENDOR_EXPORTS:
        vendor = import_module(_VENDOR_MODULE)
        return getattr(vendor, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + list(_UNIFIED_EXPORTS) + list(_VENDOR_EXPORTS))
