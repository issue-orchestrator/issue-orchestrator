"""Issue Orchestrator - Orchestrate AI agents working on GitHub issues in parallel."""

from importlib.metadata import version

from issue_orchestrator.infra import gh_audit

__version__ = version("issue-orchestrator")

__all__ = ["gh_audit"]
