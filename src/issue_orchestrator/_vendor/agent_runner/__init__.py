"""Vendored agent_runner — slim remnant.

The runner, types, env_filter, and errors have been moved to
``execution/agent_runner_*.py``.  This package retains only:
- AIProvider protocol (ports.py)
- Provider implementations (providers/)

All consumer code should import via the ``agent_runner`` facade or
directly from ``execution/``.
"""

from .ports import AIProvider
from .providers import get_provider, is_valid_provider, list_providers

__all__ = [
    "AIProvider",
    "list_providers",
    "get_provider",
    "is_valid_provider",
]

__version__ = "0.1.0"
