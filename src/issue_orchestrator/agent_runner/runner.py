"""Facade for agent_runner runner — now SubprocessAgentRunner in execution/."""

from ..execution.subprocess_runner import SubprocessAgentRunner as AgentRunner

__all__ = ["AgentRunner"]
