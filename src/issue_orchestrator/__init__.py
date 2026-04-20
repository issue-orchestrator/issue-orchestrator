"""Issue Orchestrator - Orchestrate AI agents working on GitHub issues in parallel."""

from importlib.metadata import PackageNotFoundError, version

from issue_orchestrator.infra import gh_audit

try:
    __version__ = version("issue-orchestrator")
except PackageNotFoundError:
    __version__ = "0+unknown"

__all__ = ["gh_audit"]
