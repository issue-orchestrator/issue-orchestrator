"""Main orchestrator - ties everything together."""

import asyncio
import logging
import signal
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from .control.planner import Planner, Plan, OrchestratorSnapshot
    from .control.session_manager import SessionManager
    from .control.actions import LaunchSessionAction, EscalateToHumanAction

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
from .github import (
    # Core functions (adapter-preferred but kept for backward compatibility and tests)
    list_issues, add_label, remove_label, get_issue_labels, get_open_prs_for_branch,
    # Functions still used directly (no adapter equivalent yet)
    get_latest_blocked_info, get_latest_needs_human_info,
    list_prs_with_label, create_issue,
)
# Lock files removed - using direct iTerm/active_sessions checks instead
from .models import Issue, Session, SessionStatus, OrchestratorState, PendingReview, PendingRework, PendingTriageReview, PendingCleanup, AgentConfig, ORCHESTRATOR_PR_MARKER
from .observation.observer import SessionObserver
from .control.scheduler import Scheduler
from .control.dependency_evaluator import DependencyEvaluator
# Terminal backend handled via adapters (see _terminal_adapter property)
from .worktree import create_worktree, remove_worktree, has_uncommitted_changes, extract_issue_number_from_branch
# State machine infrastructure
from .domain.events import EventBus, IssueEvent, SessionEvent, ReviewEvent
from .domain.state_machines.issue_machine import IssueStateMachine, IssueState
from .domain.state_machines.session_machine import SessionStateMachine, SessionState
from .domain.state_machines.review_machine import ReviewStateMachine, ReviewState
from .control.completion_processor import CompletionProcessor, ProcessingResult
from .control.session_controller import SessionController, SessionDecision
from .models import CompletionOutcome
from .observation.observation import SessionObservation, SessionObservationResult
# Port imports (protocols only - no concrete implementations in core)
from .ports import EventSink, SessionRunner, TraceEvent, NullEventSink, NullSessionRunner, RepositoryHost
# TODO: Inject WorkingCopy via bootstrap instead of importing concrete adapter
from .execution.git_working_copy import GitWorkingCopy


def detect_existing_work(worktree_path: Path) -> Optional[str]:
    """Check if worktree has commits ahead of main and return context for agent.

    Returns:
        Context string if existing work found, None otherwise.
    """
    try:
        # Get commits ahead of main
        result = subprocess.run(
            ["git", "-C", str(worktree_path), "log", "--oneline", "main..HEAD"],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode != 0 or not result.stdout.strip():
            return None

        commits = result.stdout.strip().split('\n')
        num_commits = len(commits)

        if num_commits == 0:
            return None

        # Get branch name
        branch_result = subprocess.run(
            ["git", "-C", str(worktree_path), "branch", "--show-current"],
            capture_output=True, text=True, timeout=5
        )
        branch = branch_result.stdout.strip() if branch_result.returncode == 0 else "unknown"

        # Build context message
        commit_list = '\n'.join(f"  - {c}" for c in commits[:10])
        if num_commits > 10:
            commit_list += f"\n  ... and {num_commits - 10} more"

        return (
            f"This worktree has {num_commits} existing commit(s) from a previous session. "
            f"Branch: {branch}. "
            f"Commits: {commit_list}. "
            f"EVALUATE this existing work BEFORE starting fresh: "
            f"(1) Check git log and diff to see what was done. "
            f"(2) Run tests. "
            f"(3) If complete and tests pass, just push via agent-done. "
            f"(4) If minor fixes needed, fix and complete. "
            f"(5) Only restart if fundamentally broken."
        )
    except Exception as e:
        logger.warning("Failed to detect existing work: %s", e)
        return None


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
    # Internal state
    state: OrchestratorState = field(default_factory=OrchestratorState)
    scheduler: Scheduler = field(init=False)
    observer: SessionObserver = field(init=False)
    _shutdown_requested: bool = field(default=False, init=False)
    _refresh_requested: bool = field(default=False, init=False)

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

        # Note: Observer is initialized without session_machines initially
        # We'll update the reference after session_machines is created
        # Pass events for observability (tests can subscribe to observe behavior)
        self.observer = SessionObserver(self.config, events=self.events)

        # State machine infrastructure
        self.event_bus = EventBus()
        self.issue_machines: dict[int, IssueStateMachine] = {}
        self.session_machines: dict[str, SessionStateMachine] = {}
        self.review_machines: dict[int, ReviewStateMachine] = {}  # keyed by PR number

        # Set up event handlers
        self._setup_event_handlers()

        # Update observer's reference to session machines
        self.observer.session_machines = self.session_machines

    @property
    def repository_host(self) -> RepositoryHost:
        """Get the repository host (always initialized after __post_init__)."""
        assert self._repository_host is not None, "RepositoryHost not initialized"
        return self._repository_host

    @property
    def _using_iterm2(self) -> bool:
        """Check if we're using iTerm2 mode (or web mode, which also uses iTerm2 tabs)."""
        # Check explicit terminal_adapter first, then fall back to ui_mode
        if self.config.terminal_adapter:
            return "iterm" in self.config.terminal_adapter.lower()
        return self.config.ui_mode in ("iterm2", "web")

    @property
    def _completion_processor(self) -> CompletionProcessor:
        """Get the completion processor with proper adapters.

        Creates a CompletionProcessor with:
        - RepositoryHost for labels and PR operations
        - WorkingCopy for git push operations
        - EventBus for event emission
        - Config-based label mapping
        """
        return CompletionProcessor(
            label_adapter=self.repository_host,
            pr_adapter=self.repository_host,
            git_adapter=GitWorkingCopy(),
            event_bus=self.event_bus,
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

    def _process_session_exit(self, session: Session) -> tuple[SessionStatus, ProcessingResult | None]:
        """Process completion.json when a session exits.

        This is the key integration point between agents and orchestrator.
        When an agent calls agent-done, it writes completion.json with
        requested_actions. This method reads that file and executes the
        actions (push, create PR, add labels, etc.).

        Args:
            session: The session that has exited.

        Returns:
            Tuple of (SessionStatus, ProcessingResult or None if no completion record).
        """
        worktree = session.worktree_path
        issue_number = session.issue.number

        # Read and process completion record
        processor = self._completion_processor
        record = processor.read_completion_record(worktree)

        if record is None:
            # No completion record - session died without calling agent-done
            self.events.publish(TraceEvent("completion.missing", {
                "issue_number": issue_number,
                "session_id": session.tmux_session_name,
                "worktree": str(worktree),
            }))
            return SessionStatus.FAILED, None

        # Emit event for completion processing start
        self.events.publish(TraceEvent("completion.processing", {
            "issue_number": issue_number,
            "outcome": record.outcome.value,
            "requested_actions": [a.value for a in record.requested_actions],
        }))

        # Process the completion record (executes push, PR, labels, etc.)
        result = processor.process(worktree, issue_number, session.issue.title)

        # Map outcome to SessionStatus
        outcome_to_status = {
            CompletionOutcome.COMPLETED: SessionStatus.COMPLETED,
            CompletionOutcome.BLOCKED: SessionStatus.BLOCKED,
            CompletionOutcome.NEEDS_HUMAN: SessionStatus.NEEDS_HUMAN,
            # Review outcomes map to COMPLETED (review session completed its job)
            CompletionOutcome.REVIEW_APPROVED: SessionStatus.COMPLETED,
            CompletionOutcome.REVIEW_CHANGES_REQUESTED: SessionStatus.COMPLETED,
        }
        status = outcome_to_status.get(record.outcome, SessionStatus.FAILED)

        # Emit event for processing result
        if result.success:
            self.events.publish(TraceEvent("completion.succeeded", {
                "issue_number": issue_number,
                "outcome": record.outcome.value,
                "status": status.value,
                "pr_url": result.pr_url,
                "actions_taken": result.actions_taken,
            }))
        else:
            self.events.publish(TraceEvent("completion.failed", {
                "issue_number": issue_number,
                "outcome": record.outcome.value,
                "status": status.value,
                "errors": result.errors,
                "actions_taken": result.actions_taken,
            }))

        return status, result

    # NOTE: Plugin management (pluggy) has been moved to bootstrap.py
    # The orchestrator receives EventSink and SessionRunner via constructor injection.
    # IPC/SSE plugin lifecycle is managed by the caller (CLI/bootstrap).

    async def _verify_hooks_on_startup(self) -> None:
        """Verify AI meta-agent hooks are installed and effective.

        This check ensures that agents cannot bypass safety guardrails
        like --no-verify. If verification fails and skip_verification
        is not enabled, startup will be blocked.

        Optimization: First checks for a valid verification marker from a
        previous run. Only runs full verification if marker is missing/invalid.
        """
        from .hooks import (
            detect_agents_from_config,
            get_adapter,
            check_verification_status,
            UnsupportedMetaAgentError,
            MetaAgentType,
        )

        # Check if verification should be skipped
        if self.config.dangerous.skip_verification:
            logger.warning(
                "[DANGEROUS] Hook verification skipped - safety guardrails may not be effective!"
            )
            print("[WARNING] Hook verification skipped (dangerous.skip_verification=true)")
            print("[WARNING] Agents may be able to bypass --no-verify protection!")
            return

        # First check if we have a valid verification marker
        is_valid, status_msg = check_verification_status(self.config.repo_root, self.config)
        if is_valid:
            logger.info("Using cached verification: %s", status_msg)
            print(f"[OK] Hooks verified (cached): {status_msg}")
            return

        # No valid marker - need to run full verification
        logger.info("No valid verification marker found - running full verification")

        # Detect which meta-agents are configured
        agent_types = detect_agents_from_config(self.config)
        unique_types = set(agent_types.values())

        logger.info("Verifying hooks for meta-agents: %s", [t.value for t in unique_types])

        all_verified = True
        unsupported = []

        for agent_type in unique_types:
            try:
                adapter = get_adapter(agent_type)

                # Check if hooks are installed
                if not adapter.is_installed(self.config.repo_root):
                    logger.error(
                        "Hooks not installed for %s. Run 'issue-orchestrator setup-hooks'",
                        agent_type.value
                    )
                    print(f"[ERROR] Hooks not installed for {agent_type.value}")
                    print("        Run 'issue-orchestrator setup-hooks' to install them")
                    all_verified = False
                    continue

                # Verify hooks are working
                result = adapter.verify_hooks(self.config.repo_root)
                if result.success:
                    logger.info("Hooks verified for %s (%d checks)", agent_type.value, len(result.checks_passed))
                    print(f"[OK] Hooks verified for {agent_type.value}")
                else:
                    logger.error("Hook verification failed for %s: %s", agent_type.value, result.checks_failed)
                    print(f"[ERROR] Hook verification failed for {agent_type.value}")
                    for failure in result.checks_failed:
                        print(f"        - {failure}")
                    all_verified = False

            except UnsupportedMetaAgentError as e:
                unsupported.append((agent_type, str(e)))
                if not self.config.dangerous.allow_unsupported_agents:
                    logger.error("Unsupported meta-agent: %s", e)
                    all_verified = False

        # Handle unsupported agents
        if unsupported:
            if self.config.dangerous.allow_unsupported_agents:
                for agent_type, reason in unsupported:
                    logger.warning("[DANGEROUS] Allowing unsupported agent %s: %s", agent_type.value, reason)
                    print(f"[WARNING] Unsupported agent {agent_type.value} allowed (dangerous mode)")
            else:
                for agent_type, reason in unsupported:
                    print(f"[ERROR] Unsupported meta-agent: {agent_type.value}")
                    print(f"        {reason}")
                print("\nTo allow unsupported agents, set dangerous.allow_unsupported_agents: true")

        # Block startup if verification failed
        if not all_verified:
            print("\n" + "=" * 60)
            print("STARTUP BLOCKED: Hook verification failed")
            print("=" * 60)
            print("\nWithout verified hooks, agents can bypass --no-verify")
            print("and push code without running pre-push tests/checks.")
            print("\nOptions:")
            print("  1. Run 'issue-orchestrator setup-hooks' to install hooks")
            print("  2. Run 'issue-orchestrator verify' to diagnose issues")
            print("  3. Set 'dangerous.skip_verification: true' in config (NOT RECOMMENDED)")
            print()
            raise RuntimeError("Hook verification failed - cannot start orchestrator safely")

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

    # ==================== State Machine Helpers ====================

    def _get_issue_machine(self, issue_number: int) -> IssueStateMachine:
        """Get or create issue state machine.

        Args:
            issue_number: The GitHub issue number

        Returns:
            IssueStateMachine for the given issue
        """
        if issue_number not in self.issue_machines:
            self.issue_machines[issue_number] = IssueStateMachine(issue_number, self.event_bus)
            logger.debug(f"Created IssueStateMachine for issue #{issue_number}")
        return self.issue_machines[issue_number]

    def _get_session_machine(
        self, session_name: str, issue_number: int, timeout_minutes: int
    ) -> SessionStateMachine:
        """Get or create session state machine.

        Args:
            session_name: Terminal session name (e.g., "issue-123")
            issue_number: The GitHub issue number
            timeout_minutes: Session timeout in minutes

        Returns:
            SessionStateMachine for the given session
        """
        if session_name not in self.session_machines:
            self.session_machines[session_name] = SessionStateMachine(
                session_name, issue_number, self.event_bus, timeout_minutes=timeout_minutes
            )
            logger.debug(f"Created SessionStateMachine for session {session_name}")
        return self.session_machines[session_name]

    def _get_review_machine(self, pr_number: int, issue_number: int) -> ReviewStateMachine:
        """Get or create review state machine for a PR.

        Args:
            pr_number: The GitHub PR number
            issue_number: The associated GitHub issue number

        Returns:
            ReviewStateMachine for the given PR
        """
        if pr_number not in self.review_machines:
            self.review_machines[pr_number] = ReviewStateMachine(
                pr_number, issue_number, self.event_bus, max_rework_cycles=self.config.max_rework_cycles
            )
            logger.debug(f"Created ReviewStateMachine for PR #{pr_number}")
        return self.review_machines[pr_number]

    def _setup_event_handlers(self) -> None:
        """Set up event handlers for state machine events.

        Connects state machine events to existing _emit_event for dashboard integration.
        """
        # Issue lifecycle events
        self.event_bus.subscribe(IssueEvent.CLAIMED, self._on_issue_claimed)
        self.event_bus.subscribe(IssueEvent.SESSION_STARTED, self._on_issue_session_started)
        self.event_bus.subscribe(IssueEvent.PR_CREATED, self._on_issue_pr_created)
        self.event_bus.subscribe(IssueEvent.COMPLETED, self._on_issue_completed)

        # Session lifecycle events
        self.event_bus.subscribe(SessionEvent.LAUNCHED, self._on_session_launched)
        self.event_bus.subscribe(SessionEvent.STARTED, self._on_session_started)
        self.event_bus.subscribe(SessionEvent.COMPLETED, self._on_session_completed)

        # Review lifecycle events
        self.event_bus.subscribe(ReviewEvent.APPROVED, self._on_review_approved)
        self.event_bus.subscribe(ReviewEvent.CHANGES_REQUESTED, self._on_review_changes_requested)

        # Label automation based on state changes
        self.event_bus.subscribe(IssueEvent.CLAIMED, self._sync_label_in_progress)
        self.event_bus.subscribe(IssueEvent.BLOCKED, self._sync_label_blocked)
        self.event_bus.subscribe(IssueEvent.NEEDS_HUMAN, self._sync_label_needs_human)
        self.event_bus.subscribe(IssueEvent.UNBLOCKED, self._sync_label_unblocked)
        self.event_bus.subscribe(IssueEvent.COMPLETED, self._sync_label_completed)
        self.event_bus.subscribe(IssueEvent.RELEASED, self._sync_label_released)

        logger.info("Event handlers configured for state machine integration")

    def _on_issue_claimed(self, event) -> None:
        """Handle issue claimed event from state machine."""
        logger.debug(f"[STATE_MACHINE] Issue #{event.entity_id} claimed")
        self.events.publish(TraceEvent("issue.claimed", {"issue_number": event.entity_id}))

    def _on_issue_session_started(self, event) -> None:
        """Handle issue session started event from state machine."""
        logger.debug(f"[STATE_MACHINE] Issue #{event.entity_id} session started")
        # Note: session.started is emitted in launch_session with more context

    def _on_issue_pr_created(self, event) -> None:
        """Handle issue PR created event from state machine."""
        logger.debug(f"[STATE_MACHINE] Issue #{event.entity_id} PR created")
        self.events.publish(TraceEvent("pr.created", {"issue_number": event.entity_id}))

    def _on_issue_completed(self, event) -> None:
        """Handle issue completed event from state machine."""
        logger.debug(f"[STATE_MACHINE] Issue #{event.entity_id} completed")
        # Note: session.completed is emitted in handle_session_completion with more context

    def _on_session_launched(self, event) -> None:
        """Handle session launched event from state machine."""
        logger.debug(f"[STATE_MACHINE] Session launched for issue #{event.entity_id}")

    def _on_session_started(self, event) -> None:
        """Handle session started event from state machine."""
        logger.debug(f"[STATE_MACHINE] Session started for issue #{event.entity_id}")

    def _on_session_completed(self, event) -> None:
        """Handle session completed event from state machine."""
        logger.debug(f"[STATE_MACHINE] Session completed for issue #{event.entity_id}")

    def _on_review_approved(self, event) -> None:
        """Handle review approved event from state machine."""
        pr_number = event.entity_id
        logger.info(f"[STATE_MACHINE] PR #{pr_number} approved")
        self.events.publish(TraceEvent("review.approved", {"pr_number": pr_number}))

    def _on_review_changes_requested(self, event) -> None:
        """Handle review changes requested event from state machine."""
        pr_number = event.entity_id
        rework_count = event.data.get("rework_count", 0)
        logger.info(f"[STATE_MACHINE] PR #{pr_number} changes requested (rework cycle {rework_count})")
        self.events.publish(TraceEvent("review.changes_requested", {"pr_number": pr_number, "rework_count": rework_count}))

    # ==================== Label Sync Handlers ====================

    def _sync_label_in_progress(self, event) -> None:
        """Add in-progress label when issue is claimed."""
        try:
            logger.debug("[ADAPTER] Using GitHubAdapter for add_label")
            self.repository_host.add_label(event.entity_id, "in-progress")
            logger.debug(f"[LABEL_SYNC] Added 'in-progress' to #{event.entity_id}")
        except Exception as e:
            logger.warning(f"[LABEL_SYNC] Failed to add label: {e}")

    def _sync_label_blocked(self, event) -> None:
        """Add blocked label when issue is blocked."""
        reason = event.data.get('reason', '')
        label = f"blocked-{reason}" if reason else "blocked"
        try:
            logger.debug("[ADAPTER] Using GitHubAdapter for add_label")
            self.repository_host.add_label(event.entity_id, label)
            logger.debug(f"[LABEL_SYNC] Added '{label}' to #{event.entity_id}")
        except Exception as e:
            logger.warning(f"[LABEL_SYNC] Failed to add label: {e}")

    def _sync_label_needs_human(self, event) -> None:
        """Add needs-human label."""
        try:
            logger.debug("[ADAPTER] Using GitHubAdapter for add_label")
            self.repository_host.add_label(event.entity_id, "blocked-needs-human")
            logger.debug(f"[LABEL_SYNC] Added 'blocked-needs-human' to #{event.entity_id}")
        except Exception as e:
            logger.warning(f"[LABEL_SYNC] Failed to add label: {e}")

    def _sync_label_unblocked(self, event) -> None:
        """Remove blocking labels when unblocked."""
        try:
            logger.debug("[ADAPTER] Using GitHubAdapter for get_issue_labels and remove_label")
            labels = self.repository_host.get_issue_labels(event.entity_id)
            for label in labels:
                if label.startswith("blocked"):
                    self.repository_host.remove_label(event.entity_id, label)
                    logger.debug(f"[LABEL_SYNC] Removed '{label}' from #{event.entity_id}")
        except Exception as e:
            logger.warning(f"[LABEL_SYNC] Failed to remove labels: {e}")

    def _sync_label_completed(self, event) -> None:
        """Remove in-progress label when completed."""
        try:
            logger.debug("[ADAPTER] Using GitHubAdapter for remove_label")
            self.repository_host.remove_label(event.entity_id, "in-progress")
            logger.debug(f"[LABEL_SYNC] Removed 'in-progress' from #{event.entity_id}")
        except Exception as e:
            logger.warning(f"[LABEL_SYNC] Failed to remove label: {e}")

    def _sync_label_released(self, event) -> None:
        """Remove in-progress label when released."""
        try:
            logger.debug("[ADAPTER] Using GitHubAdapter for remove_label")
            self.repository_host.remove_label(event.entity_id, "in-progress")
            logger.debug(f"[LABEL_SYNC] Removed 'in-progress' from #{event.entity_id}")
        except Exception as e:
            logger.warning(f"[LABEL_SYNC] Failed to remove label: {e}")

    # ==================== End State Machine Helpers ====================

    async def _restore_running_sessions(self, running: list[dict]) -> None:
        """Restore tracking for sessions that are still running after orchestrator restart.

        Args:
            running: List of dicts from discover_running_sessions() with
                     {issue_number, tab_name, is_review}
        """
        import re

        for session_info in running:
            issue_number = session_info["issue_number"]
            tab_name = session_info["tab_name"]
            is_review = session_info["is_review"]

            try:
                # Determine session type and session_name
                if is_review:
                    # Extract PR number from tab name like "#123 Review PR #456"
                    pr_match = re.search(r'Review PR #(\d+)', tab_name)
                    pr_number = int(pr_match.group(1)) if pr_match else issue_number
                    session_name = f"review-{pr_number}"
                else:
                    session_name = f"issue-{issue_number}"

                # Skip if already tracking this session
                if any(s.tmux_session_name == session_name for s in self.state.active_sessions):
                    logger.info("Session %s already tracked - skipping restore", session_name)
                    continue

                # Find the worktree
                worktree_path = None
                branch_name = "unknown"

                # Check all agent repo_roots for the worktree
                for agent_label, agent_config in self.config.agents.items():
                    repo_root = agent_config.repo_root or self.config.repo_root
                    candidate_path = repo_root.parent / f"{repo_root.name}-{issue_number}"
                    if candidate_path.exists():
                        worktree_path = candidate_path
                        # Get branch name from worktree
                        result = subprocess.run(
                            ["git", "-C", str(candidate_path), "branch", "--show-current"],
                            capture_output=True, text=True, timeout=5
                        )
                        if result.returncode == 0 and result.stdout.strip():
                            branch_name = result.stdout.strip()
                        break

                if not worktree_path:
                    logger.warning("Could not find worktree for session %s - cleaning up orphaned issue", session_name)
                    # Clean up the orphaned in-progress label so this issue can be re-processed
                    try:
                        self.repository_host.remove_label(issue_number, "in-progress")
                        logger.info("Removed in-progress label from orphaned issue #%d", issue_number)
                        print(f"  Cleaned up orphaned issue #{issue_number} (removed in-progress label)")
                    except Exception as e:
                        logger.warning("Failed to cleanup orphaned issue #%d: %s", issue_number, e)
                    continue

                # Fetch single issue details to get agent type
                issue_obj = self.repository_host.get_issue(issue_number)
                agent_config = None

                if issue_obj and issue_obj.agent_type:
                    agent_config = self.config.agents.get(issue_obj.agent_type)

                if not issue_obj:
                    # Create minimal issue object for reviews or if issue not found
                    from .models import Issue
                    issue_obj = Issue(
                        number=issue_number,
                        title=tab_name.replace("#", "").strip(),
                        labels=[],
                    )

                if not agent_config:
                    # Use first available agent config as fallback
                    agent_config = next(iter(self.config.agents.values()), None)

                if not agent_config:
                    logger.warning("No agent config available for session %s - skipping", session_name)
                    continue

                # Create session object
                session = Session(
                    issue=issue_obj,
                    agent_config=agent_config,
                    tmux_session_name=session_name,
                    worktree_path=worktree_path,
                    branch_name=branch_name,
                )

                self.state.active_sessions.append(session)
                logger.info("Restored tracking for session %s (issue #%d)", session_name, issue_number)
                print(f"  Restored: {session_name} (#{issue_number})")

            except Exception as e:
                logger.exception("Failed to restore session for issue #%d: %s", issue_number, e)
                print(f"  Warning: Failed to restore session for #{issue_number}: {e}")

    def _extract_session_number(self, session_name: str) -> int:
        """Extract the number from a session name like 'issue-42' or 'review-123'."""
        import re
        match = re.search(r"-(\d+)$", session_name)
        if match:
            return int(match.group(1))
        raise ValueError(f"Could not extract number from session name: {session_name}")

    def _create_session(self, session_name: str, command: str, working_dir: Path, title: str | None = None) -> bool:
        """Create a session using the injected SessionRunner.

        Returns:
            True if session was created successfully, False otherwise.
        """
        session_id = self._extract_session_number(session_name)
        return self.runner.create_session(
            session_id=session_id,
            command=command,
            working_dir=str(working_dir),
            title=title,
        )

    def _session_exists(self, session_name: str) -> bool:
        """Check if a session exists using the injected SessionRunner."""
        session_id = self._extract_session_number(session_name)
        return self.runner.session_exists(session_id)

    def _kill_session(self, session_name: str) -> None:
        """Kill a session using the injected SessionRunner."""
        session_id = self._extract_session_number(session_name)
        self.runner.kill_session(session_id)

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

    async def startup(self) -> None:
        """Handle startup - check for stale in-progress issues."""
        from .analysis import analyze_issue, get_issue_branches

        startup_start = time.time()
        self.state.startup_status = "running"

        # NOTE: IPC/SSE plugins are now registered by bootstrap.py before calling startup()

        # Verify AI meta-agent hooks are installed and working
        self.state.startup_message = "Verifying hook enforcement..."
        await self._verify_hooks_on_startup()

        self.state.startup_message = "Cleaning up stale claims..."
        logger.info("Starting up - checking for stale in-progress issues...")
        print("Checking for stale in-progress issues...")

        # Clean up idle terminal sessions (tabs at shell prompt where Claude has exited)
        self.state.startup_message = "Cleaning up idle terminal sessions..."
        closed_tabs = self.runner.cleanup_idle_sessions()
        if closed_tabs:
            logger.info("Closed %d idle terminal sessions", closed_tabs)
            print(f"  Closed {closed_tabs} idle terminal sessions")

        # Discover and restore tracking for running sessions
        self.state.startup_message = "Discovering running sessions..."
        running = self.runner.discover_running_sessions()
        if running:
            logger.info("Found %d running sessions to restore tracking", len(running))
            print(f"  Found {len(running)} running sessions to restore tracking")
            await self._restore_running_sessions(running)

        # Get existing branches for issue detection
        self.state.startup_message = "Scanning local branches..."
        issue_branches = get_issue_branches(self.config.repo_root)

        # Collect issues that need to be resumed (have partial work)
        issues_to_resume: list[tuple[Issue, str]] = []  # (issue, agent_label)

        # Get all in-progress issues for our agent types
        self.state.startup_message = "Checking in-progress issues on GitHub..."
        for agent_label in self.config.agents.keys():
            api_start = time.time()
            logger.debug("[ADAPTER] Using GitHubAdapter for list_issues")
            issues = self.repository_host.list_issues(
                labels=self._build_labels(agent_label, self.config.get_label_in_progress()),
                milestone=self._get_milestone_filter(),
                limit=self.config.issue_fetch_limit,
            )
            elapsed = time.time() - api_start
            logger.debug("Fetched %d in-progress issues for %s in %.1fs", len(issues), agent_label, elapsed)
            print(f"[startup] Fetched {len(issues)} in-progress issues for {agent_label} in {elapsed:.1f}s")

            for issue in issues:
                self.state.startup_message = f"Analyzing issue #{issue.number}..."
                # Use shared analysis logic
                state = analyze_issue(
                    issue=issue,
                    repo=self.config.repo,
                    issue_branches=issue_branches,
                    check_session_fn=lambda n: self._session_exists(f"issue-{n}"),
                )

                # Check if blocked - skip these, waiting for intervention
                if issue.is_blocked:
                    print(f"  #{issue.number}: Blocked - waiting for intervention")
                    continue

                if state.has_session:
                    print(f"  #{issue.number}: Active session found - resuming monitoring")
                    # TODO: recreate Session object and add to state
                elif state.has_open_pr:
                    print(f"  #{issue.number}: Has open PR ({state.pr_url or 'unknown'}) - skipping")
                    # Don't clear label - PR is pending review
                elif state.has_partial_work:
                    # Keep in-progress label - we still own this issue
                    # Queue for immediate resume when main loop starts
                    print(f"  #{issue.number}: Has branch '{state.branch}' with commits - queuing for resume")
                    issues_to_resume.append((issue, agent_label))
                elif state.is_orphaned_label:
                    # No work done at all - claim was made but nothing happened
                    # Clear label so issue becomes available again
                    print(f"  #{issue.number}: No session or branch - clearing stale label")
                    logger.debug("[ADAPTER] Using GitHubAdapter for remove_label")
                    self.repository_host.remove_label(issue.number, self.config.get_label_in_progress())

        # Check for PRs needing code review (recovery after crash/restart)
        if self.config.code_review_agent and self.config.code_review_label:
            self.state.startup_message = "Checking PRs needing code review..."
            print("\nChecking for PRs needing code review...")
            prs = list_prs_with_label(self.config.repo, self.config.code_review_label)
            for pr in prs:
                pr_number = pr.get("number")
                if pr_number is None:
                    continue
                pr_url = pr.get("url", "")
                pr_body = pr.get("body", "")

                # Extract issue number from "Closes #N" in PR body
                import re
                issue_match = re.search(r'Closes #(\d+)', pr_body, re.IGNORECASE)
                issue_number: int = int(issue_match.group(1)) if issue_match else pr_number

                # Check if PR was created by orchestrator (has marker)
                # Note: In the new architecture, PRs are created by CompletionProcessor
                from .models import ORCHESTRATOR_PR_MARKER
                if ORCHESTRATOR_PR_MARKER not in pr_body:
                    logger.debug(f"PR #{pr_number}: Not created by orchestrator (no marker)")

                # Check if review is already in progress
                if not self._session_exists(f"review-{pr_number}"):
                    # Queue for review
                    review = PendingReview(
                        issue_number=issue_number,
                        pr_number=pr_number,
                        pr_url=str(pr_url),
                        branch_name=pr.get("headRefName", ""),
                    )
                    if review not in self.state.pending_reviews:
                        self.state.pending_reviews.append(review)
                        print(f"  PR #{pr_number}: Queued for code review")
                else:
                    print(f"  PR #{pr_number}: Review already in progress")

        # Check for pending triage review issues (recovery after crash/restart)
        if self.config.triage_review_agent:
            self.state.startup_message = "Checking for pending triage review issues..."
            print("\nChecking for pending triage review issues...")
            logger.debug("[ADAPTER] Using GitHubAdapter for list_issues")
            triage_issues = self.repository_host.list_issues(
                labels=[self.config.triage_review_agent],
                limit=20,
            )
            for triage_issue in triage_issues:
                # Skip if already in active session
                session_name = f"issue-{triage_issue.number}"
                if self._session_exists(session_name):
                    print(f"  triage issue #{triage_issue.number}: Already running")
                    continue

                # Skip if already queued
                if any(r.issue_number == triage_issue.number for r in self.state.pending_triage_reviews):
                    print(f"  triage issue #{triage_issue.number}: Already queued")
                    continue

                # Queue for processing
                self.state.pending_triage_reviews.append(
                    PendingTriageReview(
                        issue_number=triage_issue.number,
                        title=triage_issue.title,
                    )
                )
                print(f"  triage issue #{triage_issue.number}: Queued ({triage_issue.title})")

            if self.state.pending_triage_reviews:
                print(f"  Found {len(self.state.pending_triage_reviews)} triage review(s) to process")

        # Recover orphaned cleanups (worktrees for PRs that were reviewed but not cleaned up)
        self._recover_orphaned_cleanups()

        # Resume issues with partial work (have in-progress label and commits but no session)
        if issues_to_resume:
            self.state.startup_message = f"Resuming {len(issues_to_resume)} in-progress issue(s)..."
            print(f"\n🔄 Resuming {len(issues_to_resume)} in-progress issue(s) with partial work...")
            for issue, agent_label in issues_to_resume:
                # Check capacity before starting
                if len(self.state.active_sessions) >= self.config.max_concurrent_sessions:
                    print(f"  #{issue.number}: At max capacity, will resume when slot available")
                    # Add to priority queue so it gets picked up first
                    if issue.number not in self.state.priority_queue:
                        self.state.priority_queue.insert(0, issue.number)
                    continue

                print(f"  #{issue.number}: Starting session to resume work...")
                session = self.launch_session(issue)
                if session:
                    print(f"  #{issue.number}: ✅ Session started")
                else:
                    print(f"  #{issue.number}: ❌ Failed to start session")

        # Run queue audit to show what will be processed
        self.state.startup_message = "Auditing queue..."
        from .audit import audit_queue, print_audit
        audit_entries = audit_queue(self.config, self.state)
        print_audit(audit_entries)

        # Cache queue issues for instant dashboard pagination
        self.state.startup_message = "Caching queue..."
        self.update_queue_cache()

        self.state.startup_status = "complete"
        self.state.startup_message = ""
        elapsed = time.time() - startup_start
        logger.info("Startup complete in %.1fs", elapsed)
        print(f"[startup] Total startup time: {elapsed:.1f}s")
        self.events.publish(TraceEvent("orchestrator.ready"))

    def launch_session(self, issue: Issue) -> Optional[Session]:
        """Launch a new session for an issue."""
        launch_start = time.time()
        logger.info("Launching session for issue #%d: %s", issue.number, issue.title)

        if issue.agent_type is None:
            raise ValueError(f"Issue #{issue.number} has no agent type label")
        agent_config = self.config.agents.get(issue.agent_type)
        if not agent_config:
            raise ValueError(f"No agent config for {issue.agent_type}")

        # Check if already being worked on (no lock files - direct reality check)
        session_name = f"issue-{issue.number}"
        if any(s.issue.number == issue.number for s in self.state.active_sessions):
            log_transition("issue", issue.number, "AVAILABLE", "SKIP", "already in active_sessions")
            print(f"Issue #{issue.number} already in active sessions - skipping")
            return None

        if self._session_exists(session_name):
            log_transition("issue", issue.number, "AVAILABLE", "SKIP", "iTerm tab already running")
            print(f"Issue #{issue.number} already has running iTerm tab - skipping")
            return None

        # CAS check: Re-verify dependencies before launching (issue body may have changed)
        dep_eval = getattr(self.scheduler, 'dependency_evaluator', None)
        if dep_eval:
            fresh_issue = self._refresh_issue(issue.number)
            if fresh_issue and fresh_issue.body:
                report = dep_eval.evaluate(
                    issue_number=issue.number,
                    issue_body=fresh_issue.body,
                )
                if not report.runnable:
                    log_transition(
                        "issue", issue.number, "AVAILABLE", "SKIP",
                        f"dependencies changed: {report.summary()}"
                    )
                    print(f"Issue #{issue.number} now has unsatisfied dependencies - skipping")
                    self.events.publish(TraceEvent(
                        name="issue.dependency_blocked",
                        data={
                            "issue_number": issue.number,
                            "issue_title": issue.title,
                            "reason": report.summary(),
                        },
                    ))
                    return None

        log_transition("issue", issue.number, "AVAILABLE", "LAUNCHING", "no conflicts")

        # Use agent's repo_root if set, otherwise fall back to config.repo_root
        repo_root = agent_config.repo_root or self.config.repo_root
        logger.info("Using repo_root=%s (agent=%s, config=%s)", repo_root, agent_config.repo_root, self.config.repo_root)

        # Create worktree (sibling to repo, named {repo}-{issue_number})
        step_start = time.time()
        logger.debug("Creating worktree for issue #%d", issue.number)
        print(f"[launch] Creating worktree for issue #{issue.number}...")
        worktree_path, branch_name = create_worktree(
            repo_root=repo_root,
            issue_number=issue.number,
            issue_title=issue.title,
            worktree_base=agent_config.worktree_base,
            enforce_hooks=self.config.enforce_hooks,
            pre_push_hook=self.config.pre_push_hook,
        )
        worktree_time = time.time() - step_start
        logger.debug("Worktree created in %.1fs", worktree_time)
        print(f"[launch] Worktree created in {worktree_time:.1f}s")

        # Run setup commands in worktree (e.g., npm install)
        if self.config.setup_worktree:
            step_start = time.time()
            for cmd in self.config.setup_worktree:
                logger.debug("Running setup command: %s", cmd)
                print(f"[launch] Running setup: {cmd}")
                result = subprocess.run(
                    cmd,
                    shell=True,
                    cwd=str(worktree_path),
                    capture_output=True,
                    text=True,
                )
                if result.returncode != 0:
                    logger.warning("Setup command failed: %s\n%s", cmd, result.stderr)
                    print(f"[launch] Warning: setup command failed: {cmd}")
            setup_time = time.time() - step_start
            logger.debug("Setup commands completed in %.1fs", setup_time)
            print(f"[launch] Setup completed in {setup_time:.1f}s")

        # Mark issue as in-progress
        step_start = time.time()
        self.repository_host.add_label(issue.number, self.config.get_label_in_progress())
        label_time = time.time() - step_start
        logger.debug("Label added in %.1fs", label_time)
        print(f"[launch] Label added in {label_time:.1f}s")

        # Check for existing work from previous interrupted session
        existing_work = detect_existing_work(worktree_path)
        if existing_work:
            logger.info("Detected existing work in worktree for issue #%d", issue.number)
            print(f"[launch] Found existing work - agent will evaluate before starting fresh")

        # Build command
        command = agent_config.get_command(
            issue_number=issue.number,
            issue_title=issue.title,
            worktree=worktree_path,
            existing_work=existing_work,
        )

        # Create session (tmux or iTerm2 tab) - command includes the initial prompt as a CLI argument
        session_name = f"issue-{issue.number}"
        step_start = time.time()
        session_created = self._create_session(session_name, command, worktree_path, title=issue.title)
        session_time = time.time() - step_start

        if not session_created:
            # Session creation failed - clean up and return None
            log_transition("issue", issue.number, "LAUNCHING", "FAILED", "session creation failed")
            print(f"[launch] ERROR: Failed to create session for issue #{issue.number}")
            print("[launch] Is iTerm2 running with a window open?")
            # Remove the in-progress label (no lock to release)
            logger.debug("[ADAPTER] Using GitHubAdapter for remove_label")
            self.repository_host.remove_label(issue.number, self.config.get_label_in_progress())
            return None

        log_transition("issue", issue.number, "LAUNCHING", "ACTIVE", "session launched", {"agent": issue.agent_type})
        logger.debug("Session created in %.1fs", session_time)
        print(f"[launch] Session created in {session_time:.1f}s")

        # Create session object
        session = Session(
            issue=issue,
            agent_config=agent_config,
            tmux_session_name=session_name,
            worktree_path=worktree_path,
            branch_name=branch_name,
        )

        self.state.active_sessions.append(session)
        total_time = time.time() - launch_start
        logger.info("Session launched for issue #%d in %.1fs (worktree=%.1fs, label=%.1fs, session=%.1fs)",
                    issue.number, total_time, worktree_time, label_time, session_time)
        print(f"Launched session for issue #{issue.number}: {issue.title}")

        # Emit trace event via EventSink (SSE, IPC, etc.)
        self.events.publish(TraceEvent("session.started", {
            "issue_number": issue.number,
            "session_id": session_name,
            "worktree_path": str(worktree_path),
            "branch_name": branch_name,
        }))

        # Trigger state machine transitions
        # Note: claim/start/launch/started are dynamically added by transitions library
        logger.debug(f"[STATE_MACHINE] Triggering transitions for issue #{issue.number}")
        issue_machine = self._get_issue_machine(issue.number)
        if issue_machine.state == IssueState.AVAILABLE.value:
            logger.debug(f"[STATE_MACHINE] Issue #{issue.number}: AVAILABLE -> CLAIMED")
            issue_machine.claim()  # type: ignore[attr-defined]
            logger.debug(f"[STATE_MACHINE] Issue #{issue.number}: CLAIMED -> IN_PROGRESS")
            issue_machine.start()  # type: ignore[attr-defined]

        # Create session state machine
        session_machine = self._get_session_machine(
            session_name, issue.number, agent_config.timeout_minutes
        )
        logger.debug(f"[STATE_MACHINE] Session {session_name}: PENDING -> STARTING")
        session_machine.launch()  # type: ignore[attr-defined]
        logger.debug(f"[STATE_MACHINE] Session {session_name}: STARTING -> RUNNING")
        session_machine.started()  # type: ignore[attr-defined]

        return session

    def handle_session_completion(self, session: Session, status: SessionStatus) -> None:
        """Handle a completed session."""
        from .models import SessionHistoryEntry

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

        # Remove from active sessions (no lock to release)
        self.state.active_sessions = [
            s for s in self.state.active_sessions
            if s.issue.number != session.issue.number
        ]

        # Let observer handle label updates
        self.observer.handle_completion(session, status)

        # Track completion
        if status == SessionStatus.COMPLETED:
            self.state.completed_today.append(session.issue.number)

        # Record in session history
        pr_url = None
        prs = None
        if status == SessionStatus.COMPLETED:
            logger.debug("[ADAPTER] Using GitHubAdapter for get_prs_for_branch")
            pr_infos = self.repository_host.get_prs_for_branch(session.branch_name)
            if pr_infos:
                pr_url = pr_infos[0].url
                # Convert PRInfo to dict for backward compatibility
                prs = [{"url": pi.url, "number": pi.number, "title": pi.title} for pi in pr_infos]

        # Generate human-readable status reason
        status_reasons = {
            SessionStatus.COMPLETED: "PR created successfully",
            SessionStatus.BLOCKED: "Agent marked issue as blocked",
            SessionStatus.NEEDS_HUMAN: "Agent requested human input",
            SessionStatus.TIMED_OUT: f"Exceeded {session.agent_config.timeout_minutes} min timeout",
            SessionStatus.FAILED: "Session ended without PR or status update",
        }
        status_reason = status_reasons.get(status, "Unknown")

        history_entry = SessionHistoryEntry(
            issue_number=session.issue.number,
            title=session.issue.title,
            agent_type=session.issue.agent_type or "unknown",
            status=status.value,
            runtime_minutes=session.runtime_minutes,
            pr_url=pr_url,
            status_reason=status_reason,
        )
        self.state.session_history.append(history_entry)

        # Emit trace events via EventSink (SSE, IPC, etc.)
        if status == SessionStatus.COMPLETED:
            self.events.publish(TraceEvent("session.completed", {
                "issue_number": session.issue.number,
                "session_id": session.tmux_session_name,
                "pr_url": pr_url,
                "runtime_minutes": session.runtime_minutes,
            }))
        elif status == SessionStatus.FAILED or status == SessionStatus.TIMED_OUT:
            self.events.publish(TraceEvent("session.failed", {
                "issue_number": session.issue.number,
                "session_id": session.tmux_session_name,
                "error": status_reason,
                "runtime_minutes": session.runtime_minutes,
            }))
        elif status == SessionStatus.BLOCKED:
            self.events.publish(TraceEvent("issue.blocked", {
                "issue_number": session.issue.number,
                "reason": status_reason,
            }))
        elif status == SessionStatus.NEEDS_HUMAN:
            self.events.publish(TraceEvent("issue.needs_human", {
                "issue_number": session.issue.number,
                "reason": status_reason,
            }))

        # Trigger state machine transitions based on session status
        logger.debug(f"[STATE_MACHINE] Triggering transitions for session {session.tmux_session_name}")

        # 1. Update session state machine
        session_machine = self.session_machines.get(session.tmux_session_name)
        if session_machine:
            logger.debug(f"[STATE_MACHINE] Found session machine for {session.tmux_session_name}")
            if status == SessionStatus.COMPLETED:
                logger.info(f"[STATE_MACHINE] Session {session.tmux_session_name}: RUNNING -> COMPLETED")
                session_machine.complete()  # type: ignore[attr-defined]
            elif status == SessionStatus.FAILED:
                logger.info(f"[STATE_MACHINE] Session {session.tmux_session_name}: RUNNING -> FAILED (reason: {status_reason})")
                session_machine.fail(data={'reason': status_reason})  # type: ignore[attr-defined]
            elif status == SessionStatus.TIMED_OUT:
                logger.info(f"[STATE_MACHINE] Session {session.tmux_session_name}: RUNNING -> TIMED_OUT")
                session_machine.timeout()  # type: ignore[attr-defined]
            elif status == SessionStatus.BLOCKED:
                logger.info(f"[STATE_MACHINE] Session {session.tmux_session_name}: RUNNING -> BLOCKED")
                session_machine.block()  # type: ignore[attr-defined]
            elif status == SessionStatus.NEEDS_HUMAN:
                logger.info(f"[STATE_MACHINE] Session {session.tmux_session_name}: RUNNING -> NEEDS_HUMAN")
                session_machine.needs_human()  # type: ignore[attr-defined]
        else:
            logger.debug(f"[STATE_MACHINE] No session machine found for {session.tmux_session_name} (may be restored session)")

        # 2. Update issue state machine
        issue_machine = self.issue_machines.get(session.issue.number)
        if issue_machine:
            logger.debug(f"[STATE_MACHINE] Found issue machine for issue #{session.issue.number}")
            if status == SessionStatus.COMPLETED and pr_url:
                logger.info(f"[STATE_MACHINE] Issue #{session.issue.number}: IN_PROGRESS -> PR_PENDING (PR: {pr_url})")
                issue_machine.pr_created(data={'pr_url': pr_url})  # type: ignore[attr-defined]
            elif status == SessionStatus.BLOCKED:
                logger.info(f"[STATE_MACHINE] Issue #{session.issue.number}: IN_PROGRESS -> BLOCKED")
                issue_machine.block()  # type: ignore[attr-defined]
            elif status == SessionStatus.NEEDS_HUMAN:
                logger.info(f"[STATE_MACHINE] Issue #{session.issue.number}: IN_PROGRESS -> NEEDS_HUMAN")
                issue_machine.needs_human()  # type: ignore[attr-defined]
        else:
            logger.debug(f"[STATE_MACHINE] No issue machine found for issue #{session.issue.number} (may be restored session)")

        # 3. Update review state machine (if this is a review session)
        if is_review and status == SessionStatus.COMPLETED:
            # Extract PR number from review session name (e.g., "review-123")
            import re
            match = re.match(r"review-(\d+)", session.tmux_session_name)
            if match:
                pr_number_review = int(match.group(1))
                review_machine = self.review_machines.get(pr_number_review)
                if review_machine:
                    logger.debug(f"[STATE_MACHINE] Found review machine for PR #{pr_number_review}")
                    # Check PR labels to determine outcome
                    # The agent-done script adds either code-reviewed or needs-rework label
                    try:
                        result = subprocess.run(
                            ["gh", "pr", "view", str(pr_number_review), "--json", "labels", "-q", ".labels[].name"],
                            capture_output=True, text=True, timeout=10
                        )
                        if result.returncode == 0:
                            labels = result.stdout.strip().split('\n')
                            if self.config.code_reviewed_label and self.config.code_reviewed_label in labels:
                                # Review was approved
                                logger.info(f"[STATE_MACHINE] PR #{pr_number_review}: IN_REVIEW -> APPROVED")
                                review_machine.approve()  # type: ignore[attr-defined]
                                # Could also trigger merge here if appropriate
                                # review_machine.merge()  # type: ignore[attr-defined]
                            elif self.config.get_label_needs_rework() in labels:
                                # Changes requested
                                logger.info(f"[STATE_MACHINE] PR #{pr_number_review}: IN_REVIEW -> CHANGES_REQUESTED")
                                review_machine.request_changes()  # type: ignore[attr-defined]
                                logger.info(f"[STATE_MACHINE] PR #{pr_number_review}: CHANGES_REQUESTED -> REWORK_PENDING")
                                review_machine.queue_rework()  # type: ignore[attr-defined]
                    except Exception as e:
                        logger.warning(f"Failed to check PR labels for review outcome: {e}")
                else:
                    logger.debug(f"[STATE_MACHINE] No review machine found for PR #{pr_number_review}")

        # Determine cleanup strategy based on config and status
        # Non-completed sessions: never cleanup (leave for investigation)
        # Completed sessions: may defer cleanup until review completes
        is_work_session = not session.tmux_session_name.startswith(("review-", "rework-"))
        should_defer_cleanup = False
        pr_number = prs[0].get("number") if prs else None

        if status == SessionStatus.COMPLETED and is_work_session and pr_url and pr_number:
            # Check if we should defer cleanup based on review workflow
            if self.config.triage_review_agent:
                # Triage workflow: defer until triage review passes
                should_defer_cleanup = self.config.cleanup.with_triage.close_ai_session_tabs
            elif self.config.code_review_agent:
                # Code review only: defer if configured to wait
                should_defer_cleanup = (
                    self.config.cleanup.without_triage.wait_for_code_review
                    and self.config.cleanup.without_triage.close_ai_session_tabs
                )
            # No review workflow: cleanup immediately (should_defer_cleanup stays False)

        if should_defer_cleanup:
            # Defer cleanup until review completes
            # pr_number and pr_url are guaranteed non-None here (checked in condition on line 733)
            assert pr_number is not None
            assert pr_url is not None
            pending = PendingCleanup(
                issue_number=session.issue.number,
                pr_number=pr_number,
                pr_url=pr_url,
                branch_name=session.branch_name,
                terminal_session_name=session.tmux_session_name,
                worktree_path=session.worktree_path,
            )
            self.state.pending_cleanups.append(pending)
            logger.info(f"[CLEANUP] Deferred cleanup for #{session.issue.number} until review completes")
        else:
            # Immediate cleanup
            if status == SessionStatus.COMPLETED:
                # Remove worktree for completed sessions
                if self.config.cleanup.without_triage.close_ai_session_tabs or not self.config.code_review_agent:
                    try:
                        remove_worktree(session.worktree_path)
                    except Exception as e:
                        print(f"Warning: failed to remove worktree: {e}")

            # Close the terminal session/tab
            # Uses _kill_session which handles both iTerm2 and tmux backends
            try:
                self._kill_session(session.tmux_session_name)
                logger.info(f"Closed session for #{session.issue.number}")
            except Exception as e:
                logger.warning(f"Failed to close session for #{session.issue.number}: {e}")

        # Trigger code review immediately if configured
        # Skip if agent has skip_review set (e.g., domain-expert agents)
        # Skip if this was a review session (not a work session)
        is_review_session = session.tmux_session_name.startswith("review-")
        if pr_url and self.config.code_review_agent and not session.agent_config.skip_review and not is_review_session:
            logger.info(f"[REVIEW] Session #{session.issue.number} completed with PR, queuing code review")
            self.queue_code_review(
                issue_number=session.issue.number,
                pr_url=pr_url,
                branch_name=session.branch_name,
            )
        elif pr_url and is_review_session:
            logger.info(f"[REVIEW] Review session {session.tmux_session_name} completed - no re-queue needed")
        elif pr_url and not self.config.code_review_agent:
            logger.info(f"[REVIEW] Session #{session.issue.number} completed but code review not configured")
        elif pr_url and session.agent_config.skip_review:
            logger.info(f"[REVIEW] Session #{session.issue.number} skipping review (skip_review=true)")
        elif not pr_url:
            logger.info(f"[REVIEW] Session #{session.issue.number} completed but no PR found")

        # Trigger triage review on failure if configured
        if status in (SessionStatus.FAILED, SessionStatus.TIMED_OUT):
            self._queue_triage_failure_review(session, status)

    def _queue_triage_failure_review(self, session: Session, status: SessionStatus) -> None:
        """Queue a triage review to investigate a session failure.

        Only queues if:
        - triage_review_on_failure is enabled (default: True)
        - triage_review_agent is configured
        - No triage review is already pending for this issue
        """
        if not self.config.triage_review_on_failure:
            logger.debug("[TRIAGE] Skipping failure review - triage_review_on_failure disabled")
            return

        if not self.config.triage_review_agent:
            logger.debug("[TRIAGE] Skipping failure review - triage_review_agent not configured")
            return

        # Check if already queued
        already_queued = any(
            r.issue_number == session.issue.number
            for r in self.state.pending_triage_reviews
        )
        if already_queued:
            logger.debug("[TRIAGE] Skipping failure review - already queued for #%d", session.issue.number)
            return

        logger.info("[TRIAGE] Queuing failure review for issue #%d (%s)",
                   session.issue.number, status.value)
        print(f"[TRIAGE] Queuing failure investigation for #{session.issue.number}")

        self.state.pending_triage_reviews.append(
            PendingTriageReview(
                issue_number=session.issue.number,
                title=f"Investigate: {session.issue.title} ({status.value})",
            )
        )

    async def run_loop(self) -> None:
        """Main orchestration loop."""
        print("Starting orchestration loop...")

        # Reconcile any orphaned PR labels on startup
        self.reconcile_orphaned_pr_labels()

        last_issue_fetch = 0.0  # Force immediate fetch on first iteration
        last_ui_update = time.time()
        ui_update_interval = 30  # Emit state_changed every 30 seconds for UI refresh

        loop_iteration = 0
        while not self._shutdown_requested:
            loop_iteration += 1
            logger.info("[LOOP] Iteration %d - active=%d, pending_reviews=%d, paused=%s",
                       loop_iteration, len(self.state.active_sessions),
                       len(self.state.pending_reviews), self.state.paused)

            try:
                # Check status of all active sessions using proper observer/controller separation
                controller = self._session_controller
                for session in list(self.state.active_sessions):
                    # Step 1: Observer gathers facts (does not decide outcome)
                    observation = self.observer.observe_session(session)

                    if observation.observation == SessionObservation.RUNNING:
                        continue  # Still running, nothing to do

                    # Step 2: Controller decides outcome based on observation + completion.json
                    # This is the key architectural change: completion.json is the source of truth
                    # for agent intent, regardless of whether session timed out or exited cleanly
                    decision = controller.decide_outcome(
                        observation=observation,
                        worktree_path=session.worktree_path,
                        issue_number=session.issue.number,
                        issue_title=session.issue.title,
                        session_name=session.tmux_session_name,
                    )

                    if decision.recovered_from_timeout:
                        logger.info(
                            "Session %s: timeout recovered - agent completed work",
                            session.tmux_session_name,
                        )

                    # Step 3: Handle the decided outcome
                    self.handle_session_completion(session, decision.status)

                # Scan for PRs needing code review (populates pending_reviews queue)
                self.scan_needs_code_review_prs()

                # Scan for PRs needing rework (populates pending_reworks queue)
                self.scan_needs_rework_prs()

                # Check if triage review should be triggered (may add to pending_triage_reviews)
                self.check_triage_review_trigger()

                # Process deferred cleanups (sessions waiting for review to complete)
                self.process_deferred_cleanups()

                # NOTE: process_pending_reviews, process_pending_reworks, process_pending_triage_reviews
                # are now handled by the planner below

                # === PLANNER-BASED DECISION MAKING ===
                # The planner decides WHAT to do, the orchestrator does HOW
                #
                # Priority order (handled by planner):
                # 1. Reviews (highest priority - complete existing work)
                # 2. Reworks (fix rejected PRs)
                # 3. Triage (investigate failures)
                # 4. New issues (only if no pending work above)

                # Skip fetching and planning if paused or at capacity
                if self.state.paused:
                    logger.debug("[PLAN] Skipping - orchestrator paused")
                elif len(self.state.active_sessions) >= self.config.max_concurrent_sessions:
                    logger.debug("[PLAN] Skipping - at capacity")
                else:
                    # Only fetch issues from GitHub when refresh interval has passed
                    # or manual refresh was requested (reduces API calls significantly)
                    issue_fetch_age = time.time() - last_issue_fetch
                    should_fetch = (
                        issue_fetch_age >= self.config.queue_refresh_seconds
                        or self._refresh_requested
                    )

                    if should_fetch:
                        if self._refresh_requested:
                            logger.info("[FETCH] Manual refresh triggered")
                            self._refresh_requested = False
                        else:
                            logger.info("[FETCH] Scheduled refresh (every %ds)",
                                       self.config.queue_refresh_seconds)

                        # Fetch issues for planning
                        all_issues = self._fetch_all_issues()
                        last_issue_fetch = time.time()

                        # Update dependency problems state
                        _, dep_blocked = self.scheduler.get_available_issues(all_issues)
                        self._update_dependency_problems(dep_blocked)

                        # Filter issues for planning
                        history_numbers = {e.issue_number for e in self.state.session_history}
                        active_numbers = {s.issue.number for s in self.state.active_sessions}
                        exclude_numbers = history_numbers | active_numbers
                        filtered_issues = [i for i in all_issues if i.number not in exclude_numbers]

                        # Filter to single issue if specified
                        if self.config.filter_issue:
                            filtered_issues = [i for i in filtered_issues if i.number == self.config.filter_issue]

                        # Update cached queue for dashboard and plan execution
                        self.state.cached_queue_issues = filtered_issues
                    else:
                        # Use cached issues for planning
                        filtered_issues = self.state.cached_queue_issues

                    # Create snapshot and plan (uses cached or fresh issues)
                    snapshot = self._create_snapshot(filtered_issues)
                    assert self.planner is not None, "Planner not initialized"
                    plan = self.planner.plan(snapshot)

                    # Log plan summary
                    if plan.action_count > 0:
                        logger.info("[PLAN] Planning %d action(s): %s",
                                   plan.action_count,
                                   ", ".join(f"{a.action_type.value}:{getattr(a, 'number', '?')}"
                                            for a in plan.actions))

                    # Apply the plan
                    self._apply_plan(plan)

                # Periodically emit state_changed for UI to update runtimes
                # This ensures "Starting" transitions to "Active" as time passes
                ui_age = time.time() - last_ui_update
                if ui_age >= ui_update_interval and self.state.active_sessions:
                    self.events.publish(TraceEvent("orchestrator.state_changed", {
                        "active_count": len(self.state.active_sessions),
                        "sessions": [s.issue.number for s in self.state.active_sessions],
                    }))
                    last_ui_update = time.time()

            except Exception as e:
                logger.exception("[LOOP] Error in iteration %d: %s", loop_iteration, e)
                print(f"[LOOP] Error in iteration {loop_iteration}: {e}")

            # Wait before next check
            await asyncio.sleep(10)

    def request_shutdown(self, force: bool = False) -> None:
        """Request graceful shutdown.

        Args:
            force: If True, kill active sessions immediately instead of waiting.
        """
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

    def _create_snapshot(self, issues: list[Issue]) -> "OrchestratorSnapshot":
        """Create an immutable snapshot for planning.

        Args:
            issues: Current list of issues from GitHub

        Returns:
            Immutable snapshot of orchestrator state
        """
        from .control.planner import OrchestratorSnapshot

        return OrchestratorSnapshot(
            issues=tuple(issues),
            active_sessions=tuple(self.state.active_sessions),
            pending_reviews=tuple(self.state.pending_reviews),
            pending_reworks=tuple(self.state.pending_reworks),
            pending_triage=tuple(self.state.pending_triage_reviews),
            paused=self.state.paused,
            priority_queue=tuple(self.state.priority_queue),
            issues_started_count=self.state.issues_started_count,
            max_issues_to_start=self.config.max_issues_to_start if self.config.max_issues_to_start > 0 else None,
        )

    def _apply_plan(self, plan: "Plan") -> None:
        """Apply the actions from a plan.

        The planner decides WHAT should happen.
        This method makes it happen.

        Args:
            plan: Plan with actions to execute
        """
        from .control.planner import Plan
        from .control.actions import ActionType, LaunchSessionAction, EscalateToHumanAction

        for action in plan.actions:
            # Respect pause mid-batch: stop applying actions if paused
            if self.state.paused:
                logger.debug("[PLAN] Stopping plan application - orchestrator paused")
                break

            try:
                if action.action_type == ActionType.LAUNCH_SESSION:
                    assert isinstance(action, LaunchSessionAction)
                    self._execute_launch_action(action)
                elif action.action_type == ActionType.ESCALATE_TO_HUMAN:
                    assert isinstance(action, EscalateToHumanAction)
                    self._execute_escalate_action(action)
                # Add more action types as needed
            except Exception as e:
                logger.exception("Failed to apply action %s: %s", action, e)

    def _execute_launch_action(self, action: "LaunchSessionAction") -> None:
        """Execute a launch session action.

        Args:
            action: The launch action to execute
        """
        from .control.actions import LaunchSessionAction

        if action.session_type == "issue":
            # Find the issue and launch
            issue = next(
                (i for i in self.state.cached_queue_issues if i.number == action.number),
                None
            )
            if issue:
                session = self.launch_session(issue)
                if session:
                    self.state.issues_started_count += 1
                    logger.info("[PLAN] Launched issue session for #%d", action.number)
            else:
                logger.warning("[PLAN] Issue #%d not found in cache", action.number)

        elif action.session_type == "review":
            # Find the pending review and launch
            review = next(
                (r for r in self.state.pending_reviews if r.pr_number == action.number),
                None
            )
            if review:
                self.launch_review_session(review)
                logger.info("[PLAN] Launched review session for PR #%d", action.number)
            else:
                logger.warning("[PLAN] Review for PR #%d not found", action.number)

        elif action.session_type == "rework":
            # Find the pending rework by issue number (from issue_key.stable_id())
            rework = next(
                (r for r in self.state.pending_reworks if int(r.issue_key.stable_id()) == action.number),
                None
            )
            if rework:
                self.launch_rework_session(rework)
                logger.info("[PLAN] Launched rework session for issue #%d", action.number)
            else:
                logger.warning("[PLAN] Rework for issue #%d not found", action.number)

        elif action.session_type == "triage":
            # Find the pending triage and launch
            triage = next(
                (t for t in self.state.pending_triage_reviews if t.issue_number == action.number),
                None
            )
            if triage:
                self._launch_triage_session(triage)
                logger.info("[PLAN] Launched triage session for #%d", action.number)
            else:
                logger.warning("[PLAN] Triage for #%d not found", action.number)

    def _execute_escalate_action(self, action: "EscalateToHumanAction") -> None:
        """Execute an escalation action.

        Args:
            action: The escalation action to execute
        """
        from .control.actions import EscalateToHumanAction

        self._escalate_to_needs_human(
            pr_number=action.pr_number,
            issue_number=action.issue_number,
            rework_cycle=action.rework_cycles,
        )
        logger.info("[PLAN] Escalated PR #%d to needs-human (cycle %d)",
                   action.pr_number, action.rework_cycles)

    def _fetch_all_issues(self) -> list[Issue]:
        """Fetch all issues from GitHub for configured agents.

        Returns:
            List of issues across all agent types
        """
        all_issues: list[Issue] = []
        for agent_label in self.config.agents.keys():
            logger.debug("[ADAPTER] Using GitHubAdapter for list_issues")
            issues = self.repository_host.list_issues(
                labels=self._build_labels(agent_label),
                milestone=self._get_milestone_filter(),
                limit=self.config.issue_fetch_limit,
            )
            all_issues.extend(issues)
        return all_issues

    def update_queue_cache(self) -> None:
        """Update the cached queue issues for instant dashboard pagination.

        This should be called after startup and periodically in run_loop.
        Emits queue.changed event when the queue composition changes.
        """
        from .audit import get_queue_issues
        try:
            queue_issues = get_queue_issues(self.config, self.state)

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

        Also ensures the needs-code-review label is added to the PR as a backup
        (in case the agent didn't use agent-done and bypassed the label).
        """
        import re
        import subprocess

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

        # Fetch PR body to check verification (this is a new PR, may need verification)
        try:
            result = subprocess.run(
                ["gh", "pr", "view", str(pr_number), "--json", "body", "-q", ".body",
                 "--repo", self.config.repo] if self.config.repo else
                ["gh", "pr", "view", str(pr_number), "--json", "body", "-q", ".body"],
                capture_output=True, text=True
            )
            if result.returncode == 0:
                pr_body = result.stdout.strip()
                from .agent_done import extract_pr_verification_status
                has_marker, _ = extract_pr_verification_status(pr_body)
                if not has_marker:
                    logger.warning(f"PR #{pr_number}: No verification token - created outside agent-done")
                    print(f"  PR #{pr_number}: ⚠️  No verification token - created outside agent-done")
        except Exception as e:
            logger.warning(f"Could not check verification for PR #{pr_number}: {e}")

        # Add needs-code-review label as backup (idempotent - won't duplicate if already present)
        if self.config.code_review_label and self.config.repo:
            try:
                result = subprocess.run(
                    ["gh", "pr", "edit", str(pr_number), "--add-label", self.config.code_review_label,
                     "--repo", self.config.repo],
                    capture_output=True, text=True
                )
                if result.returncode == 0:
                    logger.info(f"Added '{self.config.code_review_label}' label to PR #{pr_number}")
                else:
                    logger.warning(f"Could not add label to PR #{pr_number}: {result.stderr}")
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
        print(f"📝 Queued PR #{pr_number} for code review")

        # Create review state machine (starts in PENDING state)
        review_machine = self._get_review_machine(pr_number, issue_number)
        logger.debug(f"[STATE_MACHINE] ReviewStateMachine for PR #{pr_number} in {review_machine.state}")

    def launch_review_session(self, review: PendingReview) -> Optional[Session]:
        """Launch a code review session for a PR.

        Similar to launch_session but for reviewing PRs instead of implementing issues.
        """
        agent_label = self.config.code_review_agent
        if not agent_label:
            return None

        agent_config = self.config.agents.get(agent_label)
        if not agent_config:
            print(f"Warning: No agent config for {agent_label}")
            return None

        # Check if already being worked on (no lock files - direct reality check)
        session_name = f"review-{review.pr_number}"
        if any(s.tmux_session_name == session_name for s in self.state.active_sessions):
            log_transition("review", review.pr_number, "QUEUED", "SKIP", "already in active_sessions")
            self.state.pending_reviews = [r for r in self.state.pending_reviews if r.pr_number != review.pr_number]
            return None

        if self._session_exists(session_name):
            log_transition("review", review.pr_number, "QUEUED", "SKIP", "iTerm tab already running")
            self.state.pending_reviews = [r for r in self.state.pending_reviews if r.pr_number != review.pr_number]
            return None

        log_transition("review", review.pr_number, "QUEUED", "LAUNCHING", "no conflicts")

        # Use agent's repo_root if set, otherwise fall back to config.repo_root
        repo_root = agent_config.repo_root or self.config.repo_root

        # Create worktree for the review (checks out the PR branch)
        from .worktree import create_worktree
        worktree_path, _ = create_worktree(
            repo_root=repo_root,
            issue_number=review.issue_number,
            issue_title=f"Review PR #{review.pr_number}",
            branch_name=review.branch_name,  # Use existing PR branch
            enforce_hooks=False,  # Reviewer doesn't need pre-push hooks
        )

        # Build command - review agent gets PR context
        command = agent_config.get_command(
            issue_number=review.issue_number,
            issue_title=f"Review PR #{review.pr_number}",
            worktree=worktree_path,
            pr_number=review.pr_number,
        )

        # Create session
        session_name = f"review-{review.pr_number}"
        self._create_session(session_name, command, worktree_path, title=f"Review PR #{review.pr_number}")

        # Create a pseudo-issue for the session
        from .models import Issue
        pseudo_issue = Issue(
            number=review.issue_number,
            title=f"Review PR #{review.pr_number}",
            labels=[agent_label],
        )

        session = Session(
            issue=pseudo_issue,
            agent_config=agent_config,
            tmux_session_name=session_name,
            worktree_path=worktree_path,
            branch_name=review.branch_name,
        )

        self.state.active_sessions.append(session)
        log_transition("review", review.pr_number, "LAUNCHING", "ACTIVE", "session launched")
        print(f"🔍 Launched review session for PR #{review.pr_number}")

        # Trigger state machine transition
        review_machine = self._get_review_machine(review.pr_number, review.issue_number)
        if review_machine.state == ReviewState.PENDING.value:
            logger.debug(f"[STATE_MACHINE] PR #{review.pr_number}: PENDING -> IN_REVIEW")
            review_machine.start_review()  # type: ignore[attr-defined]

        # Remove from pending queue
        self.state.pending_reviews = [r for r in self.state.pending_reviews if r.pr_number != review.pr_number]

        return session

    def process_pending_reviews(self) -> None:
        """Process any pending code reviews.

        Called each loop iteration to launch review sessions.
        Respects max_concurrent_sessions and paused state.
        """
        if not self.config.code_review_agent:
            logger.info("[REVIEW] No code_review_agent configured - skipping")
            return

        if not self.state.pending_reviews:
            return  # Normal case when queue is empty, no logging needed

        # Don't start reviews while paused
        if self.state.paused:
            logger.info("[REVIEW] Skipping reviews - orchestrator paused")
            return

        # Check capacity
        available_slots = self.config.max_concurrent_sessions - len(self.state.active_sessions)
        if available_slots <= 0:
            logger.info("[REVIEW] Skipping reviews - no capacity (active=%d, max=%d)",
                       len(self.state.active_sessions), self.config.max_concurrent_sessions)
            return

        logger.info("[REVIEW] Processing %d pending reviews (capacity=%d)",
                   len(self.state.pending_reviews), available_slots)

        # Launch reviews up to capacity
        for review in list(self.state.pending_reviews)[:available_slots]:
            logger.info("[REVIEW] Launching review for PR #%d (issue #%d)",
                       review.pr_number, review.issue_number)
            try:
                self.launch_review_session(review)
            except Exception as e:
                logger.exception("[REVIEW] Failed to launch review for PR #%d: %s",
                                review.pr_number, e)
                print(f"[REVIEW] Failed to launch review for PR #{review.pr_number}: {e}")

    def process_pending_triage_reviews(self) -> None:
        """Process any pending triage reviews (failure investigations or batch reviews).

        Called each loop iteration to launch triage sessions.
        Respects max_concurrent_sessions and paused state.
        triage reviews are treated with same priority as code reviews.
        """
        if not self.config.triage_review_agent:
            return  # Triage not configured

        if not self.state.pending_triage_reviews:
            return  # Normal case when queue is empty

        # Don't start reviews while paused
        if self.state.paused:
            logger.info("[TRIAGE] Skipping triage reviews - orchestrator paused")
            return

        # Check capacity
        available_slots = self.config.max_concurrent_sessions - len(self.state.active_sessions)
        if available_slots <= 0:
            logger.info("[TRIAGE] Skipping triage reviews - no capacity (active=%d, max=%d)",
                       len(self.state.active_sessions), self.config.max_concurrent_sessions)
            return

        logger.info("[TRIAGE] Processing %d pending triage reviews (capacity=%d)",
                   len(self.state.pending_triage_reviews), available_slots)

        # Launch triage reviews up to capacity
        for triage_review in list(self.state.pending_triage_reviews)[:available_slots]:
            logger.info("[TRIAGE] Launching triage review for issue #%d: %s",
                       triage_review.issue_number, triage_review.title)
            try:
                self._launch_triage_session(triage_review)
                # Remove from queue after successful launch
                self.state.pending_triage_reviews = [
                    r for r in self.state.pending_triage_reviews
                    if r.issue_number != triage_review.issue_number
                ]
            except Exception as e:
                logger.exception("[TRIAGE] Failed to launch triage review for #%d: %s",
                                triage_review.issue_number, e)
                print(f"[TRIAGE] Failed to launch triage review for #{triage_review.issue_number}: {e}")
                # Remove from queue to prevent infinite retry loop
                self.state.pending_triage_reviews = [
                    r for r in self.state.pending_triage_reviews
                    if r.issue_number != triage_review.issue_number
                ]

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

        Checks pending cleanups and performs cleanup when:
        - Triage workflow: PR has triage-reviewed label
        - Code review workflow: PR has code-reviewed label

        Called each loop iteration.
        """
        if not self.state.pending_cleanups:
            return  # Nothing to process

        # Determine which label indicates review is complete
        if self.config.triage_review_agent:
            cleanup_label = self.config.triage_reviewed_label
        elif self.config.code_review_agent:
            cleanup_label = self.config.code_reviewed_label
        else:
            # No review workflow - shouldn't have deferred cleanups
            logger.warning("[CLEANUP] Found deferred cleanups but no review workflow configured")
            return

        if not cleanup_label:
            logger.warning("[CLEANUP] No cleanup label configured")
            return

        # Get all PRs with the cleanup label
        try:
            reviewed_prs = list_prs_with_label(self.config.repo, cleanup_label)
            reviewed_pr_numbers = {pr.get("number") for pr in reviewed_prs}
        except Exception as e:
            logger.warning(f"[CLEANUP] Failed to fetch PRs with label {cleanup_label}: {e}")
            return

        # Process each pending cleanup
        cleanups_to_remove = []
        for pending in self.state.pending_cleanups:
            if pending.pr_number in reviewed_pr_numbers:
                logger.info(f"[CLEANUP] PR #{pending.pr_number} has '{cleanup_label}' label - cleaning up")

                # Close terminal session if configured
                close_tabs = (
                    self.config.cleanup.with_triage.close_ai_session_tabs
                    if self.config.triage_review_agent
                    else self.config.cleanup.without_triage.close_ai_session_tabs
                )
                if close_tabs:
                    try:
                        self._kill_session(pending.terminal_session_name)
                        logger.info(f"[CLEANUP] Closed terminal session for #{pending.issue_number}")
                    except Exception as e:
                        logger.warning(f"[CLEANUP] Failed to close session for #{pending.issue_number}: {e}")

                # Remove worktree if configured
                remove_wt = (
                    self.config.cleanup.with_triage.remove_worktrees
                    if self.config.triage_review_agent
                    else self.config.cleanup.without_triage.remove_worktrees
                )
                if remove_wt:
                    try:
                        remove_worktree(pending.worktree_path)
                        logger.info(f"[CLEANUP] Removed worktree for #{pending.issue_number}")
                    except Exception as e:
                        logger.warning(f"[CLEANUP] Failed to remove worktree for #{pending.issue_number}: {e}")

                cleanups_to_remove.append(pending)

        # Remove processed cleanups
        for cleanup in cleanups_to_remove:
            self.state.pending_cleanups.remove(cleanup)

        if cleanups_to_remove:
            logger.info(f"[CLEANUP] Processed {len(cleanups_to_remove)} deferred cleanups")

    def _recover_orphaned_cleanups(self) -> None:
        """Recover and process orphaned cleanups from before restart.

        Called during startup to clean up worktrees for PRs that were reviewed
        (have triage-reviewed or code-reviewed label) but weren't cleaned up before
        the orchestrator stopped.

        Uses centralized naming conventions to derive worktree paths.
        """
        import re

        # Determine which label indicates cleanup is due
        if self.config.triage_review_agent:
            cleanup_label = self.config.triage_reviewed_label
            close_tabs = self.config.cleanup.with_triage.close_ai_session_tabs
            remove_wt = self.config.cleanup.with_triage.remove_worktrees
        elif self.config.code_review_agent:
            cleanup_label = self.config.code_reviewed_label
            close_tabs = self.config.cleanup.without_triage.close_ai_session_tabs
            remove_wt = self.config.cleanup.without_triage.remove_worktrees
        else:
            # No review workflow - nothing to recover
            return

        if not cleanup_label:
            return

        self.state.startup_message = "Checking for orphaned cleanups..."
        print(f"\nChecking for orphaned cleanups (PRs with '{cleanup_label}' label)...")

        try:
            reviewed_prs = list_prs_with_label(self.config.repo, cleanup_label)
        except Exception as e:
            logger.warning(f"[CLEANUP] Failed to fetch reviewed PRs: {e}")
            return

        if not reviewed_prs:
            print("  No reviewed PRs found")
            return

        cleaned_count = 0
        for pr in reviewed_prs:
            # Extract issue number from branch name (e.g., "328-description" -> 328)
            branch = pr.get("headRefName", "")
            issue_number = extract_issue_number_from_branch(branch)
            if issue_number is None:
                continue
            session_name = self._get_session_name(issue_number, "issue")

            # Check if session is still running (skip if so)
            if self._session_exists(session_name):
                logger.debug(f"[CLEANUP] Session {session_name} still running - skipping")
                continue

            # Check each agent config for matching worktree
            for agent_label, agent_config in self.config.agents.items():
                worktree_path = self._get_worktree_path(issue_number, agent_config)

                if worktree_path.exists():
                    logger.info(f"[CLEANUP] Found orphaned worktree for #{issue_number} at {worktree_path}")
                    print(f"  #{issue_number}: Cleaning up orphaned worktree")

                    # Close terminal if configured (may already be closed)
                    if close_tabs:
                        try:
                            self._kill_session(session_name)
                        except Exception:
                            pass  # Session probably already gone

                    # Remove worktree if configured
                    if remove_wt:
                        try:
                            remove_worktree(worktree_path)
                            logger.info(f"[CLEANUP] Removed orphaned worktree for #{issue_number}")
                        except Exception as e:
                            logger.warning(f"[CLEANUP] Failed to remove worktree for #{issue_number}: {e}")

                    cleaned_count += 1
                    break  # Found the worktree, no need to check other agents

        if cleaned_count > 0:
            print(f"  Cleaned up {cleaned_count} orphaned worktree(s)")
        else:
            print("  No orphaned worktrees found")

    def check_triage_review_trigger(self) -> None:
        """Check if we should trigger a triage batch review based on PR count.

        Creates a review issue if:
        - triage_review_agent is configured
        - triage_review_threshold > 0
        - Number of code-reviewed PRs >= threshold
        - No existing open triage review issue exists
        - Orchestrator is not paused
        """
        # Don't trigger new reviews while paused
        if self.state.paused:
            logger.debug("[TRIAGE] Skipped - orchestrator paused")
            return

        # Check if triage review is configured
        if not self.config.triage_review_agent:
            logger.debug("[TRIAGE] Skipped - triage_review_agent not configured")
            return
        if self.config.triage_review_threshold <= 0:
            logger.debug("[TRIAGE] Skipped - threshold is 0 (manual only)")
            return

        # Label to watch: either explicit triage_review_label or code_reviewed_label
        watch_label = self.config.triage_review_label or self.config.code_reviewed_label
        if not watch_label:
            logger.debug("[TRIAGE] Skipped - no watch label configured")
            return

        # Count PRs ready for triage review
        prs = list_prs_with_label(self.config.repo, watch_label)
        pr_count = len(prs)
        threshold = self.config.triage_review_threshold

        # Log the check (audit trail)
        logger.info("[TRIAGE] Check: %d PRs with '%s' label (threshold: %d)",
                   pr_count, watch_label, threshold)

        if pr_count < threshold:
            logger.info("[TRIAGE] Not triggered - %d/%d PRs (need %d more)",
                       pr_count, threshold, threshold - pr_count)
            return

        # Check if a triage review issue already exists (avoid duplicates)
        logger.debug("[ADAPTER] Using GitHubAdapter for list_issues")
        existing = self.repository_host.list_issues(
            labels=[self.config.triage_review_agent],
            limit=10,
        )
        for issue in existing:
            if "Batch Review" in issue.title or "Triage Review" in issue.title:
                logger.info("[TRIAGE] Skipped - existing triage review issue #%d already open",
                           issue.number)
                return

        # Create the triage review issue
        logger.info("[TRIAGE] TRIGGERING batch review for %d PRs", pr_count)
        pr_list = "\n".join(f"- PR #{pr['number']}: {pr['title']}" for pr in prs)
        body = f"""## Triage Batch Review Triggered

{len(prs)} PRs have passed code review and are ready for triage review:

{pr_list}

Review these PRs for patterns, architectural concerns, and process improvements.
Flip labels from `{watch_label}` to `{self.config.triage_reviewed_label}` after review.
"""
        title = f"Triage Batch Review: {len(prs)} PRs pending"
        issue_number = create_issue(
            self.config.repo,
            title=title,
            body=body,
            labels=[self.config.triage_review_agent],
        )
        if issue_number:
            logger.info("[TRIAGE] Created triage review issue #%d for %d PRs", issue_number, pr_count)
            print(f"📋 Created triage review issue #{issue_number} for {len(prs)} PRs")
            # Queue for immediate processing
            self.state.pending_triage_reviews.append(
                PendingTriageReview(issue_number=issue_number, title=title)
            )
        else:
            logger.error("[TRIAGE] Failed to create triage review issue")

    def scan_needs_code_review_prs(self) -> None:
        """Scan for PRs with needs-code-review label and queue them.

        Called periodically to pick up PRs that need review but aren't queued.
        This handles cases where review sessions crashed or the orchestrator restarted.
        """
        if not self.config.code_review_agent or not self.config.code_review_label:
            return

        prs = list_prs_with_label(self.config.repo, self.config.code_review_label)

        for pr in prs:
            pr_number = pr.get("number")
            if pr_number is None:
                continue

            # Skip if already queued
            if any(r.pr_number == pr_number for r in self.state.pending_reviews):
                continue

            # Skip if already being reviewed (check for active review session)
            if self._session_exists(f"review-{pr_number}"):
                continue

            # Extract issue number from PR body
            import re
            pr_body = pr.get("body", "")
            issue_match = re.search(r'Closes #(\d+)', pr_body, re.IGNORECASE)
            issue_number = int(issue_match.group(1)) if issue_match else pr_number

            pr_url = pr.get("url", f"https://github.com/{self.config.repo}/pull/{pr_number}")

            review = PendingReview(
                issue_number=issue_number,
                pr_number=pr_number,
                pr_url=str(pr_url),
                branch_name=pr.get("headRefName", ""),
            )
            self.state.pending_reviews.append(review)
            logger.info("[REVIEW] Queued orphaned PR #%d for code review", pr_number)
            print(f"📝 Found orphaned PR #{pr_number} - queued for code review")

    def scan_needs_rework_prs(self) -> None:
        """Scan for PRs with needs-rework label and queue them for rework.

        Called periodically to pick up PRs where reviewers requested changes.
        """
        if not self.config.code_review_agent:
            return  # Review workflow not configured

        rework_label = self.config.get_label_needs_rework()
        prs = list_prs_with_label(self.config.repo, rework_label)
        logger.info("[REWORK] Scanned for '%s' label, found %d PRs", rework_label, len(prs))

        for pr in prs:
            pr_number = pr.get("number")
            if not pr_number:
                continue

            # Get the PR details to find associated issue
            import re
            pr_body = pr.get("body", "")
            issue_match = re.search(r"Closes #(\d+)", pr_body)
            issue_number = int(issue_match.group(1)) if issue_match else pr_number

            # Check if already queued (by issue_key)
            if any(int(r.issue_key.stable_id()) == issue_number for r in self.state.pending_reworks):
                continue

            # Check if already being worked on
            if any(s.issue.number == issue_number for s in self.state.active_sessions):
                continue

            branch_name = pr.get("headRefName", f"{issue_number}-rework")

            # Determine rework cycle from labels
            rework_cycle = self._get_rework_cycle_from_labels(pr.get("labels", []))

            # Check if we've exceeded max rework cycles
            if rework_cycle > self.config.max_rework_cycles:
                self._escalate_to_needs_human(pr_number, issue_number, rework_cycle)
                continue

            # Extract agent type from PR labels
            agent_type = None
            for label in pr.get("labels", []):
                label_name = label.get("name", "") if isinstance(label, dict) else str(label)
                if label_name.startswith("agent:"):
                    agent_type = label_name
                    break

            if not agent_type:
                logger.warning("[REWORK] PR #%d has no agent label, skipping", pr_number)
                continue

            # Create store-agnostic IssueKey
            from .domain.issue_key import GitHubIssueKey
            repo = self.config.repo or ""
            issue_key = GitHubIssueKey(repo=repo, external_id=str(issue_number))

            # Queue for rework
            rework = PendingRework(
                issue_key=issue_key,
                agent_type=agent_type,
                rework_cycle=rework_cycle,
            )
            self.state.pending_reworks.append(rework)
            print(f"🔄 Queued issue {issue_key} for rework (cycle {rework_cycle})")

    def _get_rework_cycle_from_labels(self, labels: list) -> int:
        """Extract rework cycle number from PR labels.

        Looks for labels like "rework-1", "rework-2", etc.
        Returns 1 if no rework label found (first rework).
        """
        import re
        for label in labels:
            label_name = label.get("name", "") if isinstance(label, dict) else str(label)
            match = re.match(r"rework-(\d+)", label_name)
            if match:
                return int(match.group(1)) + 1  # Next cycle
        return 1  # First rework

    def _escalate_to_needs_human(self, pr_number: int, issue_number: int, rework_cycle: int) -> None:
        """Escalate PR to needs-human after max rework cycles.

        Removes needs-rework label and adds needs-human label.
        """
        needs_human_label = self.config.get_label_needs_human()
        rework_label = self.config.get_label_needs_rework()

        # Add needs-human label and remove needs-rework
        try:
            subprocess.run([
                "gh", "pr", "edit", str(pr_number),
                "--add-label", needs_human_label,
                "--remove-label", rework_label,
            ], capture_output=True, text=True, check=True)
            print(f"⚠️  PR #{pr_number} escalated to {needs_human_label} after {rework_cycle} rework cycles")

            # Post comment explaining escalation
            comment = f"""## ⚠️ Escalated to Human Review

This PR has gone through {rework_cycle - 1} rework cycles without passing review.
Maximum rework cycles ({self.config.max_rework_cycles}) exceeded.

**A human needs to review and either:**
- Approve the PR manually
- Provide specific guidance for the agent
- Take over the implementation
"""
            subprocess.run(["gh", "pr", "comment", str(pr_number), "--body", comment], capture_output=True, text=True, check=True)

            # Emit trace event via EventSink (SSE, IPC, etc.)
            self.events.publish(TraceEvent("review.escalated", {
                "pr_number": pr_number,
                "issue_number": issue_number,
                "rework_count": rework_cycle - 1,
                "max_rework_cycles": self.config.max_rework_cycles,
            }))
        except Exception as e:
            print(f"Warning: Failed to escalate PR #{pr_number}: {e}")

    def reconcile_orphaned_pr_labels(self) -> int:
        """Reconcile labels on agent-created PRs that are missing review labels.

        Called on startup to catch PRs where label addition failed due to
        orchestrator crash/restart or other failures.

        Returns the number of PRs that were fixed.
        """
        if not self.config.code_review_label or not self.config.repo:
            return 0

        fixed_count = 0
        code_reviewed_label = self.config.code_reviewed_label

        # Get all open PRs
        try:
            result = subprocess.run(
                ["gh", "pr", "list", "--repo", self.config.repo, "--state", "open",
                 "--json", "number,body,labels", "--limit", "100"],
                capture_output=True, text=True
            )
            if result.returncode != 0:
                logger.warning("Failed to list PRs for label reconciliation: %s", result.stderr)
                return 0

            import json
            prs = json.loads(result.stdout)
        except Exception as e:
            logger.warning("Failed to list PRs for label reconciliation: %s", e)
            return 0

        for pr in prs:
            pr_number = pr.get("number")
            body = pr.get("body", "")
            labels = [l.get("name", "") for l in pr.get("labels", [])]

            # Only reconcile PRs created by the orchestrator
            if ORCHESTRATOR_PR_MARKER not in body:
                continue

            # Check if it already has a review label
            has_review_label = (
                self.config.code_review_label in labels or
                code_reviewed_label in labels
            )

            if not has_review_label:
                # Add the needs-code-review label
                try:
                    result = subprocess.run(
                        ["gh", "pr", "edit", str(pr_number), "--add-label",
                         self.config.code_review_label, "--repo", self.config.repo],
                        capture_output=True, text=True
                    )
                    if result.returncode == 0:
                        fixed_count += 1
                        print(f"🏷️  Added '{self.config.code_review_label}' to orphaned PR #{pr_number}")
                        logger.info("Reconciled label on PR #%d", pr_number)
                    else:
                        logger.warning("Failed to add label to PR #%d: %s", pr_number, result.stderr)
                except Exception as e:
                    logger.warning("Failed to reconcile label on PR #%d: %s", pr_number, e)

        if fixed_count > 0:
            print(f"✅ Reconciled labels on {fixed_count} orphaned PR(s)")

        return fixed_count

    def launch_rework_session(self, rework: PendingRework) -> Optional[Session]:
        """Launch a rework session to fix issues found in review.

        Similar to launch_session but for fixing an existing PR.
        Uses IssueKey for store-agnostic identity, resolves PR details via adapter.
        """
        # Use cached agent_type from PendingRework (no fetch needed)
        agent_config = self.config.agents.get(rework.agent_type)
        if not agent_config:
            print(f"Warning: No agent config for {rework.agent_type}")
            return None

        # Resolve issue number from IssueKey (adapter translates key → handle)
        # For GitHub, external_id is the issue number as string
        issue_number = int(rework.issue_key.stable_id())

        # Resolve PR details from issue number via adapter
        # The adapter knows how to find the PR associated with an issue
        prs = self.repository_host.get_prs_for_branch(f"{issue_number}-")  # Branch pattern
        if not prs:
            # Try fetching by issue reference in PR body
            logger.warning("[REWORK] Could not find PR for issue %s", rework.issue_key)
            # Fall back to constructing branch name
            branch_name = f"{issue_number}-rework"
            pr_number = issue_number  # Use issue number as fallback
        else:
            pr = prs[0]  # Take first matching PR
            branch_name = pr.branch
            pr_number = pr.number

        # Check if already being worked on (no lock files - direct reality check)
        session_name = f"rework-{issue_number}"
        if any(s.tmux_session_name == session_name for s in self.state.active_sessions):
            log_transition("rework", issue_number, "QUEUED", "SKIP", "already in active_sessions")
            self.state.pending_reworks = [r for r in self.state.pending_reworks if r.issue_key != rework.issue_key]
            return None

        if self._session_exists(session_name):
            log_transition("rework", issue_number, "QUEUED", "SKIP", "iTerm tab already running")
            self.state.pending_reworks = [r for r in self.state.pending_reworks if r.issue_key != rework.issue_key]
            return None

        log_transition("rework", issue_number, "QUEUED", "LAUNCHING", f"no conflicts, cycle={rework.rework_cycle}")

        # Use agent's repo_root if set, otherwise fall back to config.repo_root
        repo_root = agent_config.repo_root or self.config.repo_root

        # Create worktree for rework (checks out the existing PR branch)
        worktree_path, _ = create_worktree(
            repo_root=repo_root,
            issue_number=issue_number,
            issue_title=f"Rework #{pr_number}",
            branch_name=branch_name,  # Use existing PR branch
            enforce_hooks=self.config.enforce_hooks,
            pre_push_hook=self.config.pre_push_hook,
        )

        # Build command - include rework context
        command = agent_config.get_command(
            issue_number=issue_number,
            issue_title=f"Rework PR #{pr_number} (cycle {rework.rework_cycle})",
            worktree=worktree_path,
            pr_number=pr_number,
        )

        # Create session
        self._create_session(session_name, command, worktree_path, title=f"Rework #{issue_number}")

        # Create minimal Issue object for session tracking
        # (We have the key identity, agent type is cached in rework)
        rework_issue = Issue(
            number=issue_number,
            title=f"Rework #{pr_number}",
            labels=[rework.agent_type],
        )

        session = Session(
            issue=rework_issue,
            agent_config=agent_config,
            tmux_session_name=session_name,
            worktree_path=worktree_path,
            branch_name=branch_name,
        )

        self.state.active_sessions.append(session)
        log_transition("rework", issue_number, "LAUNCHING", "ACTIVE", f"session launched, cycle={rework.rework_cycle}")
        print(f"🔧 Launched rework session for issue #{issue_number} (cycle {rework.rework_cycle})")

        # Update rework cycle label on PR
        self._update_rework_cycle_label(pr_number, rework.rework_cycle)

        # Remove from pending queue and remove needs-rework label
        self.state.pending_reworks = [r for r in self.state.pending_reworks if r.issue_key != rework.issue_key]
        rework_label = self.config.get_label_needs_rework()
        try:
            subprocess.run(["gh", "pr", "edit", str(pr_number), "--remove-label", rework_label],
                          capture_output=True, text=True)
        except Exception:
            pass

        return session

    def _update_rework_cycle_label(self, pr_number: int, cycle: int) -> None:
        """Update the rework cycle label on a PR."""
        try:
            # Remove old rework labels and add new one
            for i in range(1, cycle):
                try:
                    subprocess.run(["gh", "pr", "edit", str(pr_number), "--remove-label", f"rework-{i}"],
                                  capture_output=True, text=True)
                except Exception:
                    pass
            subprocess.run(["gh", "pr", "edit", str(pr_number), "--add-label", f"rework-{cycle}"],
                          capture_output=True, text=True, check=True)
        except Exception as e:
            print(f"Warning: Failed to update rework label on PR #{pr_number}: {e}")

    def process_pending_reworks(self) -> None:
        """Process any pending rework requests.

        Called each loop iteration to launch rework sessions.
        Respects max_concurrent_sessions and paused state.
        """
        if not self.state.pending_reworks:
            return

        # Don't start reworks while paused
        if self.state.paused:
            return

        # Check capacity
        available_slots = self.config.max_concurrent_sessions - len(self.state.active_sessions)
        if available_slots <= 0:
            return

        # Launch reworks up to capacity
        for rework in list(self.state.pending_reworks)[:available_slots]:
            self.launch_rework_session(rework)

    def prioritize(self, issue_number: int) -> None:
        """Add an issue to the priority queue."""
        if issue_number not in self.state.priority_queue:
            self.state.priority_queue.insert(0, issue_number)
            print(f"Issue #{issue_number} added to priority queue")


async def run_orchestrator(config_path: Optional[Path] = None) -> None:
    """Entry point to run the orchestrator."""
    # Load config
    if config_path:
        config = Config.load(config_path)
    else:
        config = Config.find_and_load()

    orchestrator = Orchestrator(config=config)

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
