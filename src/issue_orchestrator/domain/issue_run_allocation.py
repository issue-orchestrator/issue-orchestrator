"""Typed requests to the owner allocating registered issue run artifacts."""

from dataclasses import dataclass
from pathlib import Path

from .session_key import SessionKey


@dataclass(frozen=True, slots=True)
class IssueRunAllocation:
    worktree_path: Path
    session_name: str
    issue_number: int
    session_key: SessionKey
    agent_label: str
    backend: str
    claude_log_dir: str | None = None
    orchestrator_log: str | None = None
    retention_tier: str = "hot"
    retention_days: int = 7
    retention_pinned: bool = False


@dataclass(frozen=True, slots=True)
class IssueExchangeRunAllocation:
    worktree_path: Path
    issue_number: int
    session_key: SessionKey
    parent_session_name: str
    agent_label: str
