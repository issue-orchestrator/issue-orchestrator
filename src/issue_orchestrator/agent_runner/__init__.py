"""Facade for vendored agent_runner.

This keeps imports stable while agent_runner is bundled inside issue_orchestrator.
"""

from importlib import import_module
from typing import Any

_VENDOR_MODULE = "issue_orchestrator._vendor.agent_runner"
_EXPORTS = {
    "AgentRunner",
    "AIProvider",
    "RunSpec",
    "RunResult",
    "RetryPolicy",
    "ProviderErrorType",
    "list_providers",
    "get_provider",
    "is_valid_provider",
    "__version__",
}


def __getattr__(name: str) -> Any:
    if name not in _EXPORTS:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    vendor = import_module(_VENDOR_MODULE)
    return getattr(vendor, name)


def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + list(_EXPORTS))
