"""Which adapter runs the improver on a chosen provider and model (#8001).

The one place that maps an :class:`~..contracts.improver_run.ImproverAgentChoice`
to its :class:`~..ports.improver.ImproverAgent`, so the CLI, the budgeted
suite and the live improver exam launch a provider the same way.
"""

from __future__ import annotations

from ..contracts.improver_run import ImproverAgentChoice, ImproverProvider
from ..ports.command_runner import CommandRunner
from ..ports.improver import ImproverAgent
from .claude_improver_agent import ClaudeImproverAgent
from .codex_improver_agent import CodexImproverAgent


def improver_agent(choice: ImproverAgentChoice, *, runner: CommandRunner, timeout_seconds: int) -> ImproverAgent:
    match choice.provider:
        case ImproverProvider.CLAUDE:
            return ClaudeImproverAgent(runner=runner, model=choice.model, timeout_seconds=timeout_seconds)
        case ImproverProvider.CODEX:
            return CodexImproverAgent(runner=runner, model=choice.model, timeout_seconds=timeout_seconds)


__all__ = ["improver_agent"]
