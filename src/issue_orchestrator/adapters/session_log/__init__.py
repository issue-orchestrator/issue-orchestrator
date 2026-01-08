"""Session log adapters for different AI systems.

AI systems are configured via ai_systems.yaml (data-driven).
Users can add custom AI systems by creating:
  - ~/.issue-orchestrator/ai-systems.yaml (user-level)
  - .issue-orchestrator/ai-systems.yaml (project-level)
"""

from .registry import (
    DataDrivenLogProvider,
    get_log_provider,
    get_failure_context_for_session,
)

__all__ = [
    "DataDrivenLogProvider",
    "get_log_provider",
    "get_failure_context_for_session",
]
