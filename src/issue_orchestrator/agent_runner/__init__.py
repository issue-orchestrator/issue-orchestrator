"""Facade for vendored agent_runner.

This keeps imports stable while agent_runner is bundled inside issue_orchestrator.
"""

from .._vendor.agent_runner import AgentRunner, AIProvider, RunResult, RunSpec
from .._vendor.agent_runner import get_provider, is_valid_provider, list_providers

__all__ = [
    "AgentRunner",
    "AIProvider",
    "RunSpec",
    "RunResult",
    "list_providers",
    "get_provider",
    "is_valid_provider",
]

__version__ = "0.1.0"
