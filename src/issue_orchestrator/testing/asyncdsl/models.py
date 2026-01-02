from __future__ import annotations
from dataclasses import dataclass, field
from typing import Dict, Optional, Set

@dataclass
class PRView:
    number: Optional[int] = None
    draft: Optional[bool] = None
    labels: Set[str] = field(default_factory=set)

@dataclass
class IssueView:
    issue_key: str
    labels: Set[str] = field(default_factory=set)
    state: Optional[str] = None
    pr: PRView = field(default_factory=PRView)
    updated_at: Optional[str] = None
    apply_attempts: int = 0
    reconcile_required: int = 0

@dataclass
class OrchestratorView:
    idle: bool = False
    paused: bool = False
    last_tick_id: Optional[int] = None

@dataclass
class Snapshot:
    snapshot_id: int
    orchestrator: OrchestratorView
    issues: Dict[str, IssueView]
