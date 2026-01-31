"""Facade for vendored agent_runner.

This keeps imports stable while agent_runner is bundled inside issue_orchestrator.
"""

from importlib import import_module
from typing import Any

__all__ = [
    "AgentRunner",
    "AIProvider",
    "RunSpec",
    "RunResult",
    "list_providers",
    "get_provider",
    "is_valid_provider",
    "__version__",
]

_VENDOR_MODULE = "issue_orchestrator._vendor.agent_runner"


def __getattr__(name: str) -> Any:
    if name not in __all__:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    vendor = import_module(_VENDOR_MODULE)
    return getattr(vendor, name)


def __dir__() -> list[str]:
    return sorted(list(globals().keys()) + __all__)
