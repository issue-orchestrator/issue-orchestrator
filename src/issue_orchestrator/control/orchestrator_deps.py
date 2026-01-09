"""OrchestratorDeps - All dependencies required by the Orchestrator.

This module defines a frozen dataclass containing all required collaborators
for the Orchestrator. No Optional fields, no Null defaults - the Orchestrator
must be constructed in a fully-wired, valid state.

Principle: "No Nulls in Orchestrator"
- Bootstrap is the single source of truth for choosing implementations
- Tests explicitly pass fakes/nulls (never via defaults)
- Makes wiring readable and type-safe
"""

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..events import EventHub
    from ..ports import (
        EventSink,
        SessionRunner,
        RepositoryHost,
        CommandRunner,
        HookVerifier,
    )
    from ..ports.fresh_issue_reader import FreshIssueReader
    from ..ports.worktree_manager import WorktreeManager
    from ..ports.working_copy import WorkingCopy
    from .planner import Planner
    from .session_manager import SessionManager
    from .label_sync import LabelSync
    from .action_applier import ActionApplier
    from .fact_gatherer import FactGatherer
    from .pr_scanner import PRScanner
    from .session_restorer import SessionRestorer
    from .state_machine_manager import StateMachineManager
    from .completion_processor import CompletionProcessor
    from .session_controller import SessionController
    from .health_gate import HealthGate


@dataclass(frozen=True)
class OrchestratorDeps:
    """All dependencies required by the Orchestrator.

    This is a frozen (immutable) container for all collaborators.
    No Optional fields - all must be provided at construction time.

    The Orchestrator receives this bundle instead of many individual parameters,
    making the wiring explicit and type-checked.

    Attributes:
        events: Event sink for publishing trace events
        runner: Session runner for terminal operations
        repository_host: GitHub adapter for issue/PR operations
        fresh_issue_reader: FreshIssueReader for correctness-critical reads
        event_hub: Event hub for internal event distribution
        planner: Planning engine for action generation
        session_manager: Manages terminal sessions
        label_sync: Label synchronization operations
        action_applier: Applies planned actions to external systems
        fact_gatherer: Gathers facts for planning cycle
        pr_scanner: Scans PRs for review/rework state
        session_restorer: Restores sessions after restart
        worktree_manager: Manages git worktrees
        working_copy: Git working copy operations
        hook_verifier: Verifies agent hooks on startup
        command_runner: Executes shell commands
        state_machine_manager: Manages issue/session/review state machines
        completion_processor: Processes session completion files
        session_controller: Decides session outcomes
        health_gate: System health checks (capacity, rate limits)
    """

    # Core event/runtime ports
    events: "EventSink"
    runner: "SessionRunner"

    # Repository adapter
    repository_host: "RepositoryHost"
    fresh_issue_reader: "FreshIssueReader"

    # Event distribution
    event_hub: "EventHub"

    # Control plane components
    planner: "Planner"
    session_manager: "SessionManager"
    label_sync: "LabelSync"
    action_applier: "ActionApplier"
    fact_gatherer: "FactGatherer"
    pr_scanner: "PRScanner"
    session_restorer: "SessionRestorer"
    state_machine_manager: "StateMachineManager"
    completion_processor: "CompletionProcessor"
    session_controller: "SessionController"
    health_gate: "HealthGate"

    # IO adapters
    worktree_manager: "WorktreeManager"
    working_copy: "WorkingCopy"
    hook_verifier: "HookVerifier"
    command_runner: "CommandRunner"
