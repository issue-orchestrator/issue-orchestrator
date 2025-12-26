"""Main orchestrator - ties everything together."""

import asyncio
import logging
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .control.planner import Planner, Plan, OrchestratorSnapshot
    from .control.session_manager import SessionManager
    from .control.label_sync import LabelSync
    from .control.action_applier import ActionApplier
    from .control.fact_gatherer import FactGatherer
    from .control.actions import LaunchSessionAction, EscalateToHumanAction
    from .models import TriageFacts

logger = logging.getLogger(__name__)


def log_transition(
    entity_type: str,  # "issue", "review", "rework"
    number: int,
    from_state: str,
    to_state: str,
    reason: str,
    extra: dict | None = None,
) -> None:
    """Log a state transition in a consistent, searchable format.

    Format: [TRANSITION] {type} #{number}: {from} → {to} ({reason})

    Args:
        entity_type: Type of entity (issue, review, rework)
        number: Issue or PR number
        from_state: Previous state
        to_state: New state
        reason: Why the transition happened
        extra: Optional extra context (logged at debug level)
    """
    msg = f"[TRANSITION] {entity_type} #{number}: {from_state} → {to_state} ({reason})"
    logger.info(msg)
    if extra:
        logger.debug(f"[TRANSITION] #{number} extra: {extra}")


from .config import Config
from .models import Issue, Session, SessionStatus, OrchestratorState, PendingReview, PendingRework, PendingTriageReview, PendingCleanup, AgentConfig, ORCHESTRATOR_PR_MARKER
from .observation.observer import SessionObserver
from .control.scheduler import Scheduler
from .control.dependency_evaluator import DependencyEvaluator
# Terminal backend handled via adapters (see _terminal_adapter property)
# Worktree operations handled via WorktreeManager port (injected)
# State machine infrastructure
# Note: EventBus removed - state machines now use TransitionResult pattern
# See domain/state_machines/transition_result.py
from .domain.state_machines.issue_machine import IssueStateMachine, IssueState
from .domain.state_machines.session_machine import SessionStateMachine, SessionState
from .domain.state_machines.review_machine import ReviewStateMachine, ReviewState
from .control.completion_processor import CompletionProcessor
from .control.session_controller import SessionController
from .control.pr_scanner import PRScanner
from .control.session_launcher import SessionLauncher
from .control.cleanup_manager import CleanupManager
from .control.completion_handler import CompletionHandler
from .control.session_restorer import SessionRestorer
from .control.startup_manager import StartupManager
from .control.state_machine_manager import StateMachineManager
from .observation.observation import SessionObservation
# Port imports (protocols only - no concrete implementations in core)
from .ports import EventSink, SessionRunner, TraceEvent, NullEventSink, NullSessionRunner, RepositoryHost
from .ports.worktree_manager import WorktreeManager
from .ports.working_copy import WorkingCopy


@dataclass
class Orchestrator:
    """Main orchestrator that coordinates everything.

    Dependencies are injected via constructor following hexagonal architecture:
    - events: EventSink for trace event emission (SSE, IPC, logging)
    - runner: SessionRunner for terminal session management (tmux, iTerm2)
    - _repository_host: RepositoryHost for issue/label/PR operations (protocol, not implementation)

    The orchestrator core only knows about port interfaces (Protocols),
    never concrete implementations. Wiring happens in bootstrap.py.
    """

    config: Config
    # Injected dependencies (ports)
    events: EventSink = field(default_factory=NullEventSink)
    runner: SessionRunner = field(default_factory=NullSessionRunner)
    # Repository host (issues, labels, PRs) - required, injected from bootstrap
    _repository_host: Optional[RepositoryHost] = field(default=None, repr=False)
    # Optional planner (can be injected for testing, otherwise created in __post_init__)
    planner: Optional["Planner"] = field(default=None, repr=False)
    # Optional session manager (can be injected, otherwise created in __post_init__)
    session_manager: Optional["SessionManager"] = field(default=None, repr=False)
    label_sync: Optional["LabelSync"] = field(default=None, repr=False)
    # Action applier (IO boundary) - can be injected, otherwise created in __post_init__
    action_applier: Optional["ActionApplier"] = field(default=None, repr=False)
    # Fact gatherer (read-only snapshot creation) - can be injected, otherwise created in __post_init__
    fact_gatherer: Optional["FactGatherer"] = field(default=None, repr=False)
    # PR scanner (for orphaned review/rework discovery) - can be injected
    pr_scanner: Optional["PRScanner"] = field(default=None, repr=False)
    # Session restorer (for session recovery) - can be injected
    session_restorer: Optional["SessionRestorer"] = field(default=None, repr=False)
    # Worktree manager (port) - for worktree lifecycle operations
    worktree_manager: Optional[WorktreeManager] = field(default=None, repr=False)
    # Working copy (port) - for git operations inside worktrees
    working_copy: Optional[WorkingCopy] = field(default=None, repr=False)
    # State machine manager - single source of truth for state machines
    state_machine_manager: Optional[StateMachineManager] = field(default=None, repr=False)
    # Internal state
    state: OrchestratorState = field(default_factory=OrchestratorState)
    scheduler: Scheduler = field(init=False)
    observer: SessionObserver = field(init=False)
    _shutdown_requested: bool = field(default=False, init=False)
    _refresh_requested: bool = field(default=False, init=False)
    # Loop timing state (initialized in __post_init__)
    _last_issue_fetch: float = field(default=0.0, init=False)
    _last_ui_update: float = field(default=0.0, init=False)
    _loop_iteration: int = field(default=0, init=False)
    _ui_update_interval: int = field(default=30, init=False)  # Emit state_changed every 30s

    def __post_init__(self):
        # GitHub adapter must be injected via bootstrap.py
        # This enforces the hexagonal architecture boundary
        if self._repository_host is None:
            raise ValueError(
                "RepositoryHost (_repository_host) must be injected. "
                "Use bootstrap.build_orchestrator() or bootstrap.build_orchestrator_for_testing()."
            )

        # Create dependency evaluator for issue dependency gating
        dependency_evaluator = DependencyEvaluator(
            issue_checker=self._repository_host,
            events=self.events,
        )

        # Use injected planner or create default
        if self.planner is not None:
            # Use the injected planner's scheduler
            self.scheduler = self.planner.scheduler
        else:
            # Create scheduler and planner
            self.scheduler = Scheduler(
                self.config,
                dependency_evaluator=dependency_evaluator,
            )
            # Import here to avoid circular imports
            from .control.planner import Planner as PlannerClass
            self.planner = PlannerClass(
                config=self.config,
                scheduler=self.scheduler,
                dependency_evaluator=dependency_evaluator,
            )

        # Use injected session manager or create default
        if self.session_manager is None:
            from .control.session_manager import SessionManager as SessionManagerClass
            self.session_manager = SessionManagerClass(
                runner=self.runner,
                events=self.events,
                config=self.config,
            )

        # Use injected action applier or create default (requires worktree_manager)
        if self.action_applier is None:
            if self.worktree_manager is None:
                raise ValueError(
                    "Either action_applier or worktree_manager must be injected. "
                    "Use bootstrap.build_orchestrator() or bootstrap.build_orchestrator_for_testing()."
                )
            from .control.action_applier import ActionApplier as ActionApplierClass
            self.action_applier = ActionApplierClass(
                labels=self.repository_host,
                sessions=self.session_manager,
                events=self.events,
                repository_host=self.repository_host,
                worktree_manager=self.worktree_manager,
                issue_tracker=self.repository_host,
                reconcile=True,  # Compare-before-mutate for label operations
                session_launcher=self._session_launcher_callback,  # For LAUNCH_SESSION actions
            )

        # Use injected fact gatherer or create default
        if self.fact_gatherer is None:
            from .control.fact_gatherer import FactGatherer as FactGathererClass
            self.fact_gatherer = FactGathererClass(
                config=self.config,
                repository_host=self.repository_host,
            )

        # Note: Observer is initialized without session_machines initially
        # We'll update the reference after session_machines is created
        # Pass events for observability (tests can subscribe to observe behavior)
        self.observer = SessionObserver(
            self.config,
            events=self.events,
            session_runner=self.runner,
            repository_host=self._repository_host,
        )

        # State machine infrastructure - use injected manager or create
        # Note: State machines are now pure - they return TransitionResult via last_transition
        # The caller should emit TraceEvents via EventSink after transitions
        if self.state_machine_manager is None:
            self.state_machine_manager = StateMachineManager(
                config=self.config,
                events=self.events,
            )

        # Expose state machine dicts as properties for backwards compatibility
        # These delegate to the StateMachineManager
        self.issue_machines = self.state_machine_manager.issue_machines
        self.session_machines = self.state_machine_manager.session_machines
        self.review_machines = self.state_machine_manager.review_machines

        # Update observer's reference to session machines
        self.observer.session_machines = self.session_machines

    @property
    def repository_host(self) -> RepositoryHost:
        """Get the repository host (always initialized after __post_init__)."""
        assert self._repository_host is not None, "RepositoryHost not initialized"
        return self._repository_host

    @property
    def _completion_processor(self) -> CompletionProcessor:
        """Get the completion processor with proper adapters.

        Creates a CompletionProcessor with:
        - RepositoryHost for labels and PR operations
        - WorkingCopy for git push operations (injected via bootstrap)
        - Config-based label mapping
        """
        if self.working_copy is None:
            raise ValueError(
                "WorkingCopy (working_copy) must be injected. "
                "Use bootstrap.build_orchestrator() or bootstrap.build_orchestrator_for_testing()."
            )
        return CompletionProcessor(
            label_adapter=self.repository_host,
            pr_adapter=self.repository_host,
            git_adapter=self.working_copy,
            event_bus=None,  # EventBus removed - events emitted via EventSink
            label_config={
                "blocked": self.config.get_label_blocked(),
                "needs_human": self.config.get_label_needs_human(),
                "code_reviewed": self.config.code_reviewed_label or "code-reviewed",
                "needs_rework": self.config.get_label_needs_rework(),
                "code_review": self.config.code_review_label or "needs-code-review",
                "in_progress": self.config.get_label_in_progress(),
            },
        )

    @property
    def _session_controller(self) -> SessionController:
        """Get the session controller for deciding session outcomes.

        The controller uses observations + completion.json to decide outcomes.
        This is the proper separation: observer observes, controller decides.
        """
        return SessionController(
            completion_processor=self._completion_processor,
            events=self.events,
        )

    @property
    def _pr_scanner(self) -> PRScanner:
        """Get the PR scanner for discovering orphaned reviews/reworks."""
        if self.pr_scanner is not None:
            return self.pr_scanner
        # Fallback: create if not injected (for backwards compatibility)
        return PRScanner(
            config=self.config,
            repository=self.repository_host,
            events=self.events,
        )

    @property
    def _session_launcher(self) -> SessionLauncher:
        """Get the session launcher for launching agent sessions."""
        return SessionLauncher(
            config=self.config,
            events=self.events,
            repository_host=self.repository_host,
            session_manager=self.session_manager,
            worktree_manager=self.worktree_manager,
            session_exists_fn=self._session_exists,
            create_session_fn=self._create_session,
            get_issue_machine=self._get_issue_machine,
            get_session_machine=self._get_session_machine,
            get_review_machine=self._get_review_machine,
            refresh_issue_fn=self._refresh_issue,
            dependency_evaluator=getattr(self.scheduler, 'dependency_evaluator', None),
        )

    @property
    def _cleanup_manager(self) -> CleanupManager:
        """Get the cleanup manager for worktree and session cleanup."""
        return CleanupManager(
            config=self.config,
            repository_host=self.repository_host,
            worktree_manager=self.worktree_manager,
            kill_session_fn=self._kill_session,
            session_exists_fn=self._session_exists,
            get_worktree_path_fn=self._get_worktree_path,
            get_session_name_fn=self._get_session_name,
        )

    @property
    def _completion_handler(self) -> CompletionHandler:
        """Get the completion handler for session completion processing."""
        return CompletionHandler(
            config=self.config,
            events=self.events,
            repository_host=self.repository_host,
            get_issue_machine_fn=lambda n: self.issue_machines.get(n),
            get_session_machine_fn=lambda s: self.session_machines.get(s),
            get_review_machine_fn=lambda n: self.review_machines.get(n),
        )

    @property
    def _session_restorer(self) -> SessionRestorer:
        """Get the session restorer for recovering sessions after restart."""
        if self.session_restorer is not None:
            return self.session_restorer
        # Fallback: create if not injected (for backwards compatibility)
        return SessionRestorer(
            config=self.config,
            repository_host=self.repository_host,
        )

    @property
    def _state_machines(self) -> StateMachineManager:
        """Get the state machine manager."""
        # state_machine_manager is always set in __post_init__
        assert self.state_machine_manager is not None
        return self.state_machine_manager

    # Note: _verify_hooks_on_startup moved to StartupManager

    # ==================== Naming Conventions ====================
    # Centralized methods for deriving session names and paths.
    # These ensure consistency across launch, recovery, and cleanup.

    def _get_session_name(self, number: int, session_type: str = "issue") -> str:
        """Get the terminal session name for a given issue/PR number.

        Args:
            number: Issue number (for issue/rework) or PR number (for review)
            session_type: One of "issue", "review", or "rework"

        Returns:
            Session name like "issue-123", "review-456", or "rework-123"
        """
        if session_type not in ("issue", "review", "rework"):
            raise ValueError(f"Invalid session_type: {session_type}")
        return f"{session_type}-{number}"

    def _get_worktree_path(self, issue_number: int, agent_config: AgentConfig) -> Path:
        """Get the worktree path for a given issue number.

        Args:
            issue_number: The GitHub issue number
            agent_config: Agent configuration (for worktree_base and repo_root)

        Returns:
            Path to the worktree directory
        """
        repo_root = agent_config.repo_root or self.config.repo_root
        worktree_base = agent_config.worktree_base
        if worktree_base is None:
            worktree_base = repo_root.parent
        else:
            worktree_base = Path(worktree_base).resolve()

        repo_name = repo_root.name
        return worktree_base / f"{repo_name}-{issue_number}"

    # ==================== End Naming Conventions ====================

    # ==================== Session Launcher Callback ====================

    def _session_launcher_callback(self, session_type: str, number: int) -> Optional[Session]:
        """Session launcher callback for ActionApplier.

        This callback does entity lookup and delegates to the appropriate
        launch method. It bridges the gap between ActionApplier (which only
        knows session_type and number) and the actual launch logic (which
        needs the full entity).

        Args:
            session_type: "issue", "review", "rework", or "triage"
            number: Issue or PR number

        Returns:
            Session if launched successfully, None otherwise
        """
        if session_type == "issue":
            # Find the issue and launch
            issue = next(
                (i for i in self.state.cached_queue_issues if i.number == number),
                None
            )
            if issue:
                session = self.launch_session(issue)
                if session:
                    self.state.issues_started_count += 1
                    logger.info("[APPLIER] Launched issue session for #%d", number)
                return session
            else:
                logger.warning("[APPLIER] Issue #%d not found in cache", number)
                return None

        elif session_type == "review":
            # Find the pending review and launch
            review = next(
                (r for r in self.state.pending_reviews if r.pr_number == number),
                None
            )
            if review:
                session = self.launch_review_session(review)
                if session:
                    logger.info("[APPLIER] Launched review session for PR #%d", number)
                return session
            else:
                logger.warning("[APPLIER] Review for PR #%d not found", number)
                return None

        elif session_type == "rework":
            # Find the pending rework by issue number
            rework = next(
                (r for r in self.state.pending_reworks if int(r.issue_key.stable_id()) == number),
                None
            )
            if rework:
                session = self.launch_rework_session(rework)
                if session:
                    logger.info("[APPLIER] Launched rework session for issue #%d", number)
                return session
            else:
                logger.warning("[APPLIER] Rework for issue #%d not found", number)
                return None

        elif session_type == "triage":
            # Find the pending triage and launch
            triage = next(
                (t for t in self.state.pending_triage_reviews if t.issue_number == number),
                None
            )
            if triage:
                self._launch_triage_session(triage)
                # _launch_triage_session doesn't return a session, need to find it
                # Return the session from active_sessions
                session = next(
                    (s for s in self.state.active_sessions if s.issue.number == number),
                    None
                )
                if session:
                    logger.info("[APPLIER] Launched triage session for #%d", number)
                return session
            else:
                logger.warning("[APPLIER] Triage for #%d not found", number)
                return None

        else:
            logger.warning("[APPLIER] Unknown session type: %s", session_type)
            return None

    # ==================== End Session Launcher Callback ====================

    # ==================== State Machine Helpers ====================
    # These delegate to the StateMachineManager

    def _get_issue_machine(self, issue_number: int) -> IssueStateMachine:
        """Get or create issue state machine. Delegates to StateMachineManager."""
        return self._state_machines.get_issue_machine(issue_number)

    def _get_session_machine(
        self, session_name: str, issue_number: int, timeout_minutes: int
    ) -> SessionStateMachine:
        """Get or create session state machine. Delegates to StateMachineManager."""
        return self._state_machines.get_session_machine(session_name, issue_number, timeout_minutes)

    def _get_review_machine(self, pr_number: int, issue_number: int) -> ReviewStateMachine:
        """Get or create review state machine. Delegates to StateMachineManager."""
        return self._state_machines.get_review_machine(pr_number, issue_number)

    # ==================== Label Sync Helpers ====================
    # Note: These methods can be called directly after state machine transitions
    # to sync labels. They no longer rely on EventBus subscriptions.

    def _sync_label(self, issue_number: int, label: str, operation: str) -> None:
        """Sync a label on an issue via label_sync or direct adapter call.

        Args:
            issue_number: GitHub issue number
            label: Label to add/remove
            operation: "add" or "remove"
        """
        try:
            if self.label_sync:
                if operation == "add":
                    self.label_sync.sync_add(issue_number, label)
                else:
                    self.label_sync.sync_remove(issue_number, label)
            else:
                if operation == "add":
                    self.repository_host.add_label(issue_number, label)
                else:
                    self.repository_host.remove_label(issue_number, label)
        except Exception as e:
            self.events.publish(TraceEvent("labels.sync_error", {
                "issue_number": issue_number, "label": label, "operation": operation, "error": str(e)
            }))

    def _remove_blocked_labels(self, issue_number: int) -> None:
        """Remove all blocked-* labels from an issue."""
        try:
            labels = self.repository_host.get_issue_labels(issue_number)
            if self.label_sync:
                self.label_sync.remove_blocked_labels(issue_number, set(labels))
            else:
                for label in labels:
                    if label.startswith("blocked"):
                        self.repository_host.remove_label(issue_number, label)
        except Exception as e:
            self.events.publish(TraceEvent("labels.sync_error", {
                "issue_number": issue_number, "label": "blocked-*", "operation": "remove", "error": str(e)
            }))

    # ==================== End Label Sync Helpers ====================

    async def _restore_running_sessions(self, running: list[dict]) -> None:
        """Restore tracking for sessions that are still running after orchestrator restart.

        Delegates to SessionRestorer for the actual restoration logic.

        Args:
            running: List of dicts from discover_running_sessions() with
                     {issue_number, tab_name, is_review}
        """
        restored = self._session_restorer.restore_sessions(
            running=running,
            already_tracked=self.state.active_sessions,
        )
        self.state.active_sessions.extend(restored)

    def _create_session(self, session_name: str, command: str, working_dir: Path, title: str | None = None) -> bool:
        """Create a session using the SessionManager.

        Returns:
            True if session was created successfully, False otherwise.
        """
        from .control.session_manager import SessionRef, SessionContext
        assert self.session_manager is not None  # Set in __post_init__
        try:
            ref = SessionRef.from_name(session_name)
        except ValueError as e:
            self.events.publish(TraceEvent("session.name_parse_error", {
                "session_name": session_name,
                "error": str(e),
                "operation": "create",
            }))
            raise
        ctx = SessionContext(ref=ref, command=command, working_dir=working_dir, title=title)
        return self.session_manager.start(ctx)

    def _session_exists(self, session_name: str) -> bool:
        """Check if a session exists using the SessionManager."""
        from .control.session_manager import SessionRef
        assert self.session_manager is not None  # Set in __post_init__
        try:
            ref = SessionRef.from_name(session_name)
        except ValueError as e:
            self.events.publish(TraceEvent("session.name_parse_error", {
                "session_name": session_name,
                "error": str(e),
                "operation": "exists",
            }))
            raise
        return self.session_manager.exists(ref)

    def _kill_session(self, session_name: str) -> None:
        """Kill a session using the SessionManager."""
        from .control.session_manager import SessionRef
        assert self.session_manager is not None  # Set in __post_init__
        try:
            ref = SessionRef.from_name(session_name)
        except ValueError as e:
            self.events.publish(TraceEvent("session.name_parse_error", {
                "session_name": session_name,
                "error": str(e),
                "operation": "kill",
            }))
            raise
        self.session_manager.stop(ref)

    def _refresh_issue(self, issue_number: int) -> Optional[Issue]:
        """Fetch fresh issue data from GitHub.

        Used for CAS checks before launching to detect race conditions
        where the issue body may have been modified (adding dependencies).
        """
        try:
            return self.repository_host.get_issue(issue_number)
        except Exception as e:
            logger.warning("Failed to refresh issue #%d: %s", issue_number, e)
            return None

    def _build_labels(self, *labels: str) -> list[str]:
        """Build labels list, including filter_label if configured."""
        result = list(labels)
        if self.config.filter_label:
            result.append(self.config.filter_label)
        return result

    def _get_milestone_filter(self) -> str | None:
        """Get the milestone filter if configured."""
        return self.config.filter_milestone

    @property
    def _startup_manager(self) -> StartupManager:
        """Get the startup manager for handling startup sequence."""
        return StartupManager(
            config=self.config,
            events=self.events,
            runner=self.runner,
            repository_host=self.repository_host,
            session_exists_fn=self._session_exists,
            restore_sessions_fn=lambda running: self._restore_running_sessions(running),
            launch_session_fn=self.launch_session,
            update_queue_cache_fn=self.update_queue_cache,
        )

    async def startup(self) -> None:
        """Handle startup - delegates to StartupManager."""
        await self._startup_manager.run_startup(self.state)

    def launch_session(self, issue: Issue) -> Optional[Session]:
        """Launch a new session for an issue.

        Delegates to SessionLauncher for the actual launch logic.
        The orchestrator owns state management (active_sessions).
        """
        result = self._session_launcher.launch_issue_session(
            issue=issue,
            active_sessions=self.state.active_sessions,
        )
        if result.success and result.session:
            self.state.active_sessions.append(result.session)
            return result.session
        return None

    def handle_session_completion(self, session: Session, status: SessionStatus) -> None:
        """Handle a completed session.

        Delegates to CompletionHandler for:
        - State machine transitions
        - Event emission
        - History recording
        - Cleanup decision

        Orchestrator handles:
        - Active sessions list update
        - Observer notification
        - Actual cleanup execution
        - Review queue management
        """
        # Determine entity type from session name
        is_review = session.tmux_session_name.startswith("review-")
        is_rework = session.tmux_session_name.startswith("rework-")
        entity_type = "review" if is_review else ("rework" if is_rework else "issue")

        # Log the state transition
        log_transition(
            entity_type,
            session.issue.number,
            "ACTIVE",
            status.value.upper(),
            f"runtime={session.runtime_minutes}min",
            {"agent": session.issue.agent_type, "branch": session.branch_name},
        )

        print(f"Session #{session.issue.number} completed with status: {status.value}")

        # Remove from active sessions
        self.state.active_sessions = [
            s for s in self.state.active_sessions
            if s.issue.number != session.issue.number
        ]

        # Let observer handle label updates
        self.observer.handle_completion(session, status)

        # Track completion
        if status == SessionStatus.COMPLETED:
            self.state.completed_today.append(session.issue.number)

        # Delegate to completion handler for state machine updates, events, etc.
        result = self._completion_handler.process_completion(session, status)

        # Record history
        self.state.session_history.append(result.history_entry)

        # Handle cleanup based on result
        if result.should_defer_cleanup and result.pending_cleanup:
            self.state.pending_cleanups.append(result.pending_cleanup)
        else:
            # Immediate cleanup
            if status == SessionStatus.COMPLETED:
                # Remove worktree for completed sessions
                if self.config.cleanup.without_triage.close_ai_session_tabs or not self.config.code_review_agent:
                    try:
                        if self.worktree_manager:
                            self.worktree_manager.remove(session.worktree_path)
                    except Exception as e:
                        print(f"Warning: failed to remove worktree: {e}")

            # Close the terminal session/tab
            try:
                self._kill_session(session.tmux_session_name)
                logger.info(f"Closed session for #{session.issue.number}")
            except Exception as e:
                logger.warning(f"Failed to close session for #{session.issue.number}: {e}")

        # Store discovered review for Planner to decide (instead of calling queue_code_review directly)
        if result.should_queue_review and result.pr_url and result.pr_number:
            from .models import DiscoveredReview
            discovered = DiscoveredReview(
                issue_number=session.issue.number,
                pr_number=result.pr_number,
                pr_url=result.pr_url,
                branch_name=session.branch_name,
            )
            self.state.discovered_reviews.append(discovered)
            logger.info("[COMPLETION] Discovered review for PR #%d - Planner will decide", result.pr_number)

        # Store discovered failure for Planner to decide (instead of calling _queue_triage_failure_review directly)
        if status in (SessionStatus.FAILED, SessionStatus.TIMED_OUT):
            from .models import DiscoveredFailure
            discovered = DiscoveredFailure(
                issue_number=session.issue.number,
                issue_title=session.issue.title,
                failure_reason=status.value,
            )
            self.state.discovered_failures.append(discovered)
            logger.info("[COMPLETION] Discovered failure for issue #%d - Planner will decide", session.issue.number)

    def tick(self) -> bool:
        """Execute one iteration of the orchestration loop.

        Returns:
            True if the loop should continue, False if shutdown requested.
        """
        self._loop_iteration += 1
        logger.info("[LOOP] Iteration %d - active=%d, pending_reviews=%d, paused=%s",
                   self._loop_iteration, len(self.state.active_sessions),
                   len(self.state.pending_reviews), self.state.paused)

        if self._shutdown_requested:
            return False

        # Check status of all active sessions using observer/controller separation
        self._process_active_sessions()

        # Scan for PRs needing code review/rework (populates queues)
        self.scan_needs_code_review_prs()
        self.scan_needs_rework_prs()

        # Planner-based decision making (skipped if paused or at capacity)
        if not self.state.paused and len(self.state.active_sessions) < self.config.max_concurrent_sessions:
            self._run_planning_cycle()

        # Periodically emit state_changed for UI
        self._emit_ui_update_if_needed()

        return True

    def _process_active_sessions(self) -> None:
        """Check all active sessions and handle completions."""
        controller = self._session_controller
        for session in list(self.state.active_sessions):
            observation = self.observer.observe_session(session)
            if observation.observation == SessionObservation.RUNNING:
                continue

            decision = controller.decide_outcome(
                observation=observation,
                worktree_path=session.worktree_path,
                issue_number=session.issue.number,
                issue_title=session.issue.title,
                session_name=session.tmux_session_name,
                completion_path=session.completion_path,
            )

            if decision.recovered_from_timeout:
                logger.info("Session %s: timeout recovered - agent completed work",
                           session.tmux_session_name)

            self.handle_session_completion(session, decision.status)

    def _run_planning_cycle(self) -> None:
        """Fetch issues, create snapshot, plan, and apply."""
        # Only fetch issues when refresh interval passed or manual refresh requested
        issue_fetch_age = time.time() - self._last_issue_fetch
        should_fetch = issue_fetch_age >= self.config.queue_refresh_seconds or self._refresh_requested

        if should_fetch:
            if self._refresh_requested:
                logger.info("[FETCH] Manual refresh triggered")
                self._refresh_requested = False
            else:
                logger.info("[FETCH] Scheduled refresh (every %ds)", self.config.queue_refresh_seconds)

            all_issues = self._fetch_all_issues()
            self._last_issue_fetch = time.time()

            # Update dependency problems state
            _, dep_blocked = self.scheduler.get_available_issues(all_issues)
            self._update_dependency_problems(dep_blocked)

            # Filter issues for planning
            history_numbers = {e.issue_number for e in self.state.session_history}
            active_numbers = {s.issue.number for s in self.state.active_sessions}
            exclude_numbers = history_numbers | active_numbers
            filtered_issues = [i for i in all_issues if i.number not in exclude_numbers]

            if self.config.filter_issue:
                filtered_issues = [i for i in filtered_issues if i.number == self.config.filter_issue]

            self.state.cached_queue_issues = filtered_issues
        else:
            filtered_issues = self.state.cached_queue_issues

        # Create snapshot, plan, and apply
        snapshot = self.fact_gatherer.create_snapshot(self.state, filtered_issues)
        assert self.planner is not None, "Planner not initialized"
        plan = self.planner.plan(snapshot)

        if plan.action_count > 0:
            logger.info("[PLAN] Planning %d action(s): %s", plan.action_count,
                       ", ".join(f"{a.action_type.value}:{getattr(a, 'number', '?')}" for a in plan.actions))

        self._apply_plan(plan)
        self._clear_discovered_facts()

    def _clear_discovered_facts(self) -> None:
        """Clear discovered facts after plan is applied."""
        for attr in ("discovered_reviews", "discovered_reworks", "discovered_escalations", "discovered_failures"):
            lst = getattr(self.state, attr)
            if lst:
                logger.debug("[PLAN] Clearing %d %s after plan applied", len(lst), attr)
                lst.clear()

    def _emit_ui_update_if_needed(self) -> None:
        """Emit state_changed event for UI if interval has passed."""
        ui_age = time.time() - self._last_ui_update
        if ui_age >= self._ui_update_interval and self.state.active_sessions:
            self.events.publish(TraceEvent("orchestrator.state_changed", {
                "active_count": len(self.state.active_sessions),
                "sessions": [s.issue.number for s in self.state.active_sessions],
            }))
            self._last_ui_update = time.time()

    async def run_loop(self) -> None:
        """Main orchestration loop."""
        print("Starting orchestration loop...")

        # Reconcile orphaned PR labels on startup
        self.reconcile_orphaned_pr_labels()

        # Initialize timing state
        self._last_issue_fetch = 0.0  # Force immediate fetch
        self._last_ui_update = time.time()
        self._loop_iteration = 0

        while not self._shutdown_requested:
            try:
                if not self.tick():
                    break
            except Exception as e:
                logger.exception("[LOOP] Error in iteration %d: %s", self._loop_iteration, e)
                print(f"[LOOP] Error in iteration {self._loop_iteration}: {e}")
            await asyncio.sleep(10)

    def request_shutdown(self, force: bool = False) -> None:
        """Request graceful shutdown.

        Args:
            force: If True, kill active sessions immediately instead of waiting.
        """
        self._shutdown_requested = True
        active = self.state.active_sessions
        if active:
            if force:
                print(f"Force shutdown - killing {len(active)} active session(s):")
                for s in active:
                    print(f"  #{s.issue.number}: {s.issue.title}")
                    try:
                        self._kill_session(s.tmux_session_name)
                    except Exception as e:
                        print(f"    Warning: failed to kill session: {e}")
                self.state.active_sessions = []
                print("All sessions killed. Exiting...")
            else:
                print(f"Shutdown requested - waiting for {len(active)} active session(s):")
                for s in active:
                    print(f"  #{s.issue.number}: {s.issue.title} ({s.runtime_minutes} min)")
                print("\nPress Ctrl+C again to force kill all sessions.")
        else:
            print("Shutdown requested - no active sessions, exiting...")

        # NOTE: IPC server shutdown is handled by the caller (CLI/bootstrap)

    def request_refresh(self) -> None:
        """Request an immediate refresh of issues from GitHub.

        This triggers the orchestrator to fetch issues on the next loop iteration,
        bypassing the queue_refresh_seconds interval. Can be called from web UI
        or IPC to manually trigger a refresh.
        """
        self._refresh_requested = True
        logger.info("[REFRESH] Manual refresh requested")

    def pause(self) -> None:
        """Pause - don't start new sessions."""
        self.state.paused = True
        print("Orchestrator paused - will finish current sessions but not start new ones")
        self.events.publish(TraceEvent("orchestrator.paused"))

    def resume(self) -> None:
        """Resume after pause."""
        self.state.paused = False
        print("Orchestrator resumed")
        self.events.publish(TraceEvent("orchestrator.resumed"))

    def _apply_plan(self, plan: "Plan") -> None:
        """Apply the actions from a plan.

        The planner decides WHAT should happen.
        This method makes it happen via ActionApplier + state updates.

        Args:
            plan: Plan with actions to execute
        """
        for action in plan.actions:
            if self.state.paused:
                logger.debug("[PLAN] Stopping plan application - orchestrator paused")
                break

            try:
                result = self.action_applier.apply(action)
                if result.success:
                    self._update_state_after_action(action, result)
                else:
                    logger.warning("[PLAN] Action %s failed: %s", action.action_type.value, result.error)
            except Exception as e:
                logger.exception("Failed to apply action %s: %s", action, e)

    def _update_state_after_action(self, action: "Action", result: "ActionResult") -> None:
        """Update orchestrator state after a successful action.

        Dispatches to specific state update handlers based on action type.
        """
        from .control.actions import ActionType

        handlers = {
            ActionType.LAUNCH_SESSION: self._handle_launch_state_update,
            ActionType.ESCALATE_TO_HUMAN: self._handle_escalation_state_update,
            ActionType.QUEUE_REVIEW: self._handle_queue_review_state_update,
            ActionType.QUEUE_REWORK: self._handle_queue_rework_state_update,
            ActionType.QUEUE_TRIAGE: self._handle_queue_triage_state_update,
            ActionType.CREATE_TRIAGE_ISSUE: self._handle_create_triage_state_update,
            ActionType.CLEANUP_SESSION: self._handle_cleanup_state_update,
        }

        handler = handlers.get(action.action_type)
        if handler:
            handler(action, result)

    def _handle_launch_state_update(self, action: "Action", result: "ActionResult") -> None:
        """Log successful session launch."""
        from .control.actions import LaunchSessionAction
        assert isinstance(action, LaunchSessionAction)
        logger.info("[PLAN] Launched %s session for #%d", action.session_type, action.number)

    def _handle_create_triage_state_update(self, action: "Action", result: "ActionResult") -> None:
        """Update state after triage issue creation."""
        from .control.actions import CreateTriageIssueAction
        assert isinstance(action, CreateTriageIssueAction)
        issue_number = result.details.get("issue_number")
        if issue_number:
            self.state.pending_triage_reviews.append(
                PendingTriageReview(issue_number=issue_number, title=action.title)
            )
            print(f"📋 Created triage review issue #{issue_number} for {action.pr_count} PRs")

    def _handle_cleanup_state_update(self, action: "Action", result: "ActionResult") -> None:
        """Update state after cleanup action."""
        from .control.actions import CleanupSessionAction
        assert isinstance(action, CleanupSessionAction)
        self.state.pending_cleanups = [
            c for c in self.state.pending_cleanups if c.pr_number != action.pr_number
        ]

    def _handle_escalation_state_update(self, action: "Action", result: "ActionResult") -> None:
        """Update state after escalation action succeeds."""
        from .control.actions import EscalateToHumanAction
        assert isinstance(action, EscalateToHumanAction)
        logger.info("[PLAN] Escalated PR #%d to needs-human (cycle %d)",
                   action.pr_number, action.rework_cycles)

    def _handle_queue_review_state_update(self, action: "Action", result: "ActionResult") -> None:
        """Update state after queue review action succeeds."""
        from .control.actions import QueueReviewAction
        assert isinstance(action, QueueReviewAction)

        # Check if already queued (defensive)
        if any(r.pr_number == action.pr_number for r in self.state.pending_reviews):
            logger.debug("[PLAN] PR #%d already queued, skipping state update", action.pr_number)
            return

        # Add needs-code-review label as backup
        if self.config.code_review_label:
            try:
                self.repository_host.add_label(action.pr_number, self.config.code_review_label)
                logger.info("Added '%s' label to PR #%d", self.config.code_review_label, action.pr_number)
            except Exception as e:
                logger.warning("Failed to add review label to PR #%d: %s", action.pr_number, e)

        # Create pending review
        review = PendingReview(
            issue_number=action.issue_number,
            pr_number=action.pr_number,
            pr_url=action.pr_url,
            branch_name=action.branch_name,
        )
        self.state.pending_reviews.append(review)
        log_transition("review", action.pr_number, "CREATED", "QUEUED", f"from issue #{action.issue_number}")

        # Create review state machine
        review_machine = self._get_review_machine(action.pr_number, action.issue_number)
        logger.debug("[STATE_MACHINE] ReviewStateMachine for PR #%d in %s", action.pr_number, review_machine.state)
        logger.info("[PLAN] Queued review for PR #%d", action.pr_number)

    def _handle_queue_rework_state_update(self, action: "Action", result: "ActionResult") -> None:
        """Update state after queue rework action succeeds."""
        from .control.actions import QueueReworkAction
        assert isinstance(action, QueueReworkAction)

        # Check if already queued (defensive)
        queued_issue_ids = {int(r.issue_key.stable_id()) for r in self.state.pending_reworks}
        if action.issue_number in queued_issue_ids:
            logger.debug("[PLAN] Issue #%d already queued for rework, skipping state update", action.issue_number)
            return

        # Create IssueKey via repository
        issue_key = self.repository_host.create_issue_key(action.issue_number)

        # Need agent_type - try to find it from discovered reworks
        agent_type = ""
        for rework in self.state.discovered_reworks:
            if rework.issue_number == action.issue_number:
                agent_type = rework.agent_type
                break
        if not agent_type:
            logger.warning("[PLAN] No agent_type found for rework issue #%d, using default", action.issue_number)
            agent_type = "agent:developer"

        # Create pending rework
        rework = PendingRework(
            issue_key=issue_key,
            agent_type=agent_type,
            rework_cycle=action.rework_cycle,
        )
        self.state.pending_reworks.append(rework)
        log_transition("rework", action.issue_number, "CREATED", "QUEUED",
                      f"cycle {action.rework_cycle}")
        logger.info("[PLAN] Queued rework for issue #%d (cycle %d)", action.issue_number, action.rework_cycle)
        print(f"🔄 Queued issue #{action.issue_number} for rework (cycle {action.rework_cycle})")

    def _handle_queue_triage_state_update(self, action: "Action", result: "ActionResult") -> None:
        """Update state after queue triage action succeeds."""
        from .control.actions import QueueTriageAction
        assert isinstance(action, QueueTriageAction)

        # Check if already queued (defensive)
        if any(t.issue_number == action.issue_number for t in self.state.pending_triage_reviews):
            logger.debug("[PLAN] Issue #%d already queued for triage, skipping state update", action.issue_number)
            return

        # Add to pending triage reviews
        self.state.pending_triage_reviews.append(
            PendingTriageReview(
                issue_number=action.issue_number,
                title=action.title,
            )
        )
        logger.info("[PLAN] Queued triage for issue #%d", action.issue_number)
        print(f"[TRIAGE] Queued failure investigation for #{action.issue_number}")

    # _execute_launch_action removed - now handled via ActionApplier + _session_launcher_callback
    # Legacy _execute_* methods removed - now handled by ActionApplier + _handle_*_state_update

    def _fetch_all_issues(self) -> list[Issue]:
        """Fetch all issues from GitHub for configured agents.

        Returns:
            List of issues across all agent types
        """
        all_issues: list[Issue] = []
        for agent_label in self.config.agents.keys():
            labels = self._build_labels(agent_label)
            logger.debug("[ADAPTER] Using GitHubAdapter for list_issues")
            issues = self.repository_host.list_issues(
                labels=labels,
                milestone=self._get_milestone_filter(),
                limit=self.config.issue_fetch_limit,
            )
            all_issues.extend(issues)
            # Emit event for visibility
            self.events.publish(TraceEvent("issues.fetched", {
                "agent": agent_label,
                "labels": labels,
                "milestone": self._get_milestone_filter(),
                "count": len(issues),
                "issue_numbers": [i.number for i in issues],
            }))
        return all_issues

    def update_queue_cache(self) -> None:
        """Update the cached queue issues for instant dashboard pagination.

        This should be called after startup and periodically in run_loop.
        Emits queue.changed event when the queue composition changes.
        """
        from .audit import get_queue_issues
        try:
            queue_issues = get_queue_issues(self.config, self.state, issue_tracker=self.repository_host)

            # Track changes for events
            old_numbers = {i.number for i in self.state.cached_queue_issues}
            new_numbers = {i.number for i in queue_issues}

            added = new_numbers - old_numbers
            removed = old_numbers - new_numbers

            self.state.cached_queue_issues = queue_issues

            # Emit event if queue changed
            if added or removed:
                # Build issue info for added issues
                added_info = [
                    {"number": i.number, "title": i.title}
                    for i in queue_issues if i.number in added
                ]
                removed_info = [
                    {"number": num}
                    for num in removed
                ]

                self.events.publish(TraceEvent(
                    name="queue.changed",
                    data={
                        "added": added_info,
                        "removed": removed_info,
                        "total": len(queue_issues),
                    },
                ))
                logger.info(
                    "Queue changed: %d added, %d removed, %d total",
                    len(added), len(removed), len(queue_issues),
                )
            else:
                logger.debug("Updated queue cache with %d issues (no changes)", len(queue_issues))
        except Exception as e:
            logger.warning("Failed to update queue cache: %s", e)

    def _update_dependency_problems(
        self,
        dep_blocked: list[tuple["Issue", str]],
    ) -> None:
        """Update dependency problems state and emit events for changes.

        Compares current state with new blocked issues and emits events for:
        - dependency.blocked: when an issue becomes blocked
        - dependency.unblocked: when a blocked issue is no longer blocked

        Args:
            dep_blocked: List of (issue, reason) tuples from scheduler.
        """
        from .models import DependencyProblem

        # Build new problems dict
        new_problems: dict[int, DependencyProblem] = {}
        for issue, reason in dep_blocked:
            new_problems[issue.number] = DependencyProblem(
                issue_number=issue.number,
                issue_title=issue.title,
                blocked_by=[],  # We'll parse from reason if needed
                summary=reason,
            )

        # Find newly blocked issues (in new but not in current)
        current_blocked = set(self.state.dependency_problems.keys())
        new_blocked = set(new_problems.keys())

        newly_blocked = new_blocked - current_blocked
        newly_unblocked = current_blocked - new_blocked

        # Emit events for newly blocked
        for issue_num in newly_blocked:
            problem = new_problems[issue_num]
            self.events.publish(TraceEvent(
                name="dependency.blocked",
                data={
                    "issue_number": problem.issue_number,
                    "issue_title": problem.issue_title,
                    "summary": problem.summary,
                },
            ))

        # Emit events for newly unblocked
        for issue_num in newly_unblocked:
            problem = self.state.dependency_problems[issue_num]
            self.events.publish(TraceEvent(
                name="dependency.unblocked",
                data={
                    "issue_number": problem.issue_number,
                    "issue_title": problem.issue_title,
                },
            ))

        # Update state
        self.state.dependency_problems = new_problems

        if newly_blocked or newly_unblocked:
            logger.info(
                "Dependency status changed: %d newly blocked, %d unblocked",
                len(newly_blocked),
                len(newly_unblocked),
            )

    def queue_code_review(self, issue_number: int, pr_url: str, branch_name: str) -> None:
        """Queue a PR for code review.

        Called immediately after a work agent creates a PR.
        The review will be processed in the next loop iteration.
        Uses repository_host adapter for all GitHub operations.

        Also ensures the needs-code-review label is added to the PR as a backup
        (in case the agent didn't use agent-done and bypassed the label).
        """
        import re

        # Extract PR number from URL
        match = re.search(r"/pull/(\d+)", pr_url)
        if not match:
            print(f"Warning: Could not extract PR number from {pr_url}")
            return

        pr_number = int(match.group(1))

        # Check if already queued
        for review in self.state.pending_reviews:
            if review.pr_number == pr_number:
                return

        # Fetch PR to check verification (this is a new PR, may need verification)
        try:
            pr_info = self.repository_host.get_pr(pr_number)
            if pr_info:
                from .agent_done import extract_pr_verification_status
                has_marker, _ = extract_pr_verification_status(pr_info.body)
                if not has_marker:
                    logger.warning(f"PR #{pr_number}: No verification token - created outside agent-done")
                    print(f"  PR #{pr_number}: ⚠️  No verification token - created outside agent-done")
        except Exception as e:
            logger.warning(f"Could not check verification for PR #{pr_number}: {e}")

        # Add needs-code-review label as backup via adapter (idempotent - won't duplicate if already present)
        # Note: GitHub treats PRs as issues, so issue label operations work on PRs
        if self.config.code_review_label:
            try:
                self.repository_host.add_label(pr_number, self.config.code_review_label)
                logger.info(f"Added '{self.config.code_review_label}' label to PR #{pr_number}")
            except Exception as e:
                logger.warning(f"Failed to add review label to PR #{pr_number}: {e}")

        review = PendingReview(
            issue_number=issue_number,
            pr_number=pr_number,
            pr_url=pr_url,
            branch_name=branch_name,
        )
        self.state.pending_reviews.append(review)
        log_transition("review", pr_number, "CREATED", "QUEUED", f"from issue #{issue_number}")

        # Emit event for visibility
        self.events.publish(TraceEvent("review.queued", {
            "pr_number": pr_number,
            "issue_number": issue_number,
            "pr_url": pr_url,
        }))

        # Create review state machine (starts in PENDING state)
        review_machine = self._get_review_machine(pr_number, issue_number)
        logger.debug(f"[STATE_MACHINE] ReviewStateMachine for PR #{pr_number} in {review_machine.state}")

    def launch_review_session(self, review: PendingReview) -> Optional[Session]:
        """Launch a code review session for a PR.

        Delegates to SessionLauncher for the actual launch logic.
        The orchestrator owns state management (active_sessions, pending_reviews).
        """
        result = self._session_launcher.launch_review_session(
            review=review,
            active_sessions=self.state.active_sessions,
        )
        if result.success and result.session:
            self.state.active_sessions.append(result.session)
            # Remove from pending queue
            self.state.pending_reviews = [r for r in self.state.pending_reviews if r.pr_number != review.pr_number]
            return result.session
        elif not result.success:
            # Session skipped (already running or conflict) - remove from pending
            self.state.pending_reviews = [r for r in self.state.pending_reviews if r.pr_number != review.pr_number]
        return None

    def _launch_triage_session(self, triage_review: PendingTriageReview) -> None:
        """Launch a triage session to investigate a failure or review PRs.

        Creates a worktree, launches the triage agent, and tracks the session.
        """
        triage_agent_name = self.config.triage_review_agent
        if not triage_agent_name:
            raise ValueError("No triage review agent configured")

        agent_config = self.config.agents.get(triage_agent_name)
        if not agent_config:
            raise ValueError(f"No agent config for {triage_agent_name}")

        # Create a synthetic Issue for the triage session
        # This allows reusing the normal session launch flow
        triage_issue = Issue(
            number=triage_review.issue_number,
            title=triage_review.title,
            labels=[triage_agent_name],
        )

        # Launch using normal flow
        session = self.launch_session(triage_issue)
        if session:
            print(f"[TRIAGE] Launched investigation session for #{triage_review.issue_number}")

    def process_deferred_cleanups(self) -> None:
        """Process deferred cleanups for sessions awaiting review completion.

        Delegates to CleanupManager for the actual cleanup logic.
        """
        self.state.pending_cleanups = self._cleanup_manager.process_deferred_cleanups(
            self.state.pending_cleanups
        )

    def _recover_orphaned_cleanups(self) -> None:
        """Recover and process orphaned cleanups from before restart.

        Delegates to CleanupManager for the actual cleanup logic.
        """
        def set_startup_message(msg: str) -> None:
            self.state.startup_message = msg

        self._cleanup_manager.recover_orphaned_cleanups(set_startup_message)

    def scan_needs_code_review_prs(self) -> None:
        """Scan for PRs with needs-code-review label and store as discovered.

        Called periodically to pick up PRs that need review but aren't queued.
        Uses PRScanner controller for discovery logic.

        NOTE: This method stores discovered reviews for the Planner to decide.
        The Planner produces QueueReviewAction, which the orchestrator applies.
        """
        from .models import DiscoveredReview

        # Get active session names for filtering
        active_sessions = [s.tmux_session_name for s in self.state.active_sessions]

        # Use scanner to find orphaned reviews
        reviews = self._pr_scanner.scan_for_reviews(
            already_queued=self.state.pending_reviews,
            active_sessions=active_sessions,
        )

        # Store as discovered reviews for Planner to decide
        for review in reviews:
            # Convert PendingReview to DiscoveredReview for Planner
            discovered = DiscoveredReview(
                issue_number=review.issue_number,
                pr_number=review.pr_number,
                pr_url=review.pr_url,
                branch_name=review.branch_name,
            )
            self.state.discovered_reviews.append(discovered)
            logger.info("[SCAN] Discovered orphaned PR #%d for code review - Planner will decide",
                       review.pr_number)

    def scan_needs_rework_prs(self) -> None:
        """Scan for PRs with needs-rework label and store as discovered.

        Called periodically to pick up PRs where reviewers requested changes.
        Uses PRScanner controller for discovery logic.

        NOTE: This method stores discovered reworks/escalations for the Planner to decide.
        The Planner produces QueueReworkAction/EscalateToHumanAction, which the orchestrator applies.
        """
        from .models import DiscoveredRework, DiscoveredEscalation

        # Get active issue numbers for filtering
        active_issues = [s.issue.number for s in self.state.active_sessions]

        # Use scanner to find reworks and escalations
        reworks, escalations = self._pr_scanner.scan_for_reworks(
            already_queued=self.state.pending_reworks,
            active_sessions=active_issues,
        )

        # Store escalations as discovered facts for Planner
        for pr_number, issue_number, rework_cycle in escalations:
            discovered = DiscoveredEscalation(
                issue_number=issue_number,
                pr_number=pr_number,
                rework_cycle=rework_cycle,
            )
            self.state.discovered_escalations.append(discovered)
            logger.info("[SCAN] Discovered escalation for PR #%d - Planner will decide",
                       pr_number)

        # Store reworks as discovered facts for Planner
        for rework in reworks:
            discovered = DiscoveredRework(
                issue_number=int(rework.issue_key.stable_id()),
                pr_number=0,  # PR number not directly available from PendingRework
                branch_name="",  # Branch not directly available
                agent_type=rework.agent_type,
                rework_cycle=rework.rework_cycle,
            )
            self.state.discovered_reworks.append(discovered)
            logger.info("[SCAN] Discovered rework for issue #%d - Planner will decide",
                       int(rework.issue_key.stable_id()))

    def reconcile_orphaned_pr_labels(self) -> int:
        """Reconcile labels on agent-created PRs that are missing review labels.

        Called on startup to catch PRs where label addition failed due to
        orchestrator crash/restart or other failures.

        Returns the number of PRs that were fixed.
        """
        if not self.config.code_review_label or not self.config.repo or not self.label_sync:
            return 0

        return self.label_sync.reconcile_orphaned_pr_labels(
            code_review_label=self.config.code_review_label,
            code_reviewed_label=self.config.code_reviewed_label,
            orchestrator_marker=ORCHESTRATOR_PR_MARKER,
        )

    def launch_rework_session(self, rework: PendingRework) -> Optional[Session]:
        """Launch a rework session to fix issues found in review.

        Delegates to SessionLauncher for the actual launch logic.
        The orchestrator owns state management (active_sessions, pending_reworks).
        """
        result = self._session_launcher.launch_rework_session(
            rework=rework,
            active_sessions=self.state.active_sessions,
        )
        if result.success and result.session:
            self.state.active_sessions.append(result.session)
            # Remove from pending queue
            self.state.pending_reworks = [r for r in self.state.pending_reworks if r.issue_key != rework.issue_key]
            return result.session
        elif not result.success:
            # Session skipped (already running or conflict) - remove from pending
            self.state.pending_reworks = [r for r in self.state.pending_reworks if r.issue_key != rework.issue_key]
        return None

    def prioritize(self, issue_number: int) -> None:
        """Add an issue to the priority queue."""
        if issue_number not in self.state.priority_queue:
            self.state.priority_queue.insert(0, issue_number)
            print(f"Issue #{issue_number} added to priority queue")


async def run_orchestrator(config_path: Optional[Path] = None) -> None:
    """Entry point to run the orchestrator."""
    from .bootstrap import build_orchestrator

    # Load config
    if config_path:
        config = Config.load(config_path)
    else:
        config = Config.find_and_load()

    orchestrator = build_orchestrator(config)

    # Setup signal handlers with force kill on second Ctrl+C
    def handle_signal(signum, frame):
        if orchestrator._shutdown_requested:
            # Second signal - force kill
            orchestrator.request_shutdown(force=True)
        else:
            # First signal - graceful shutdown
            orchestrator.request_shutdown()

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    # Run startup checks
    await orchestrator.startup()

    # Run main loop
    await orchestrator.run_loop()

    print("Orchestrator stopped")
