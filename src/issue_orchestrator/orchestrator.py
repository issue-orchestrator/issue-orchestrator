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
from ._worktree_impl import remove_worktree, extract_issue_number_from_branch
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
from .observation.observation import SessionObservation
# Port imports (protocols only - no concrete implementations in core)
from .ports import EventSink, SessionRunner, TraceEvent, NullEventSink, NullSessionRunner, RepositoryHost
# TODO: Inject WorkingCopy via bootstrap instead of importing concrete adapter
from .execution.git_working_copy import GitWorkingCopy


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

        # Use injected action applier or create default
        if self.action_applier is None:
            from .control.action_applier import ActionApplier as ActionApplierClass
            from .execution.worktree_adapter import GitWorktreeManager
            self.action_applier = ActionApplierClass(
                labels=self.repository_host,
                sessions=self.session_manager,
                events=self.events,
                repository_host=self.repository_host,
                worktree_manager=GitWorktreeManager(),
                issue_tracker=self.repository_host,
                reconcile=True,  # Compare-before-mutate for label operations
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
        self.observer = SessionObserver(self.config, events=self.events)

        # State machine infrastructure
        # Note: State machines are now pure - they return TransitionResult via last_transition
        # The caller should emit TraceEvents via EventSink after transitions
        self.issue_machines: dict[int, IssueStateMachine] = {}
        self.session_machines: dict[str, SessionStateMachine] = {}
        self.review_machines: dict[int, ReviewStateMachine] = {}  # keyed by PR number

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
        - WorkingCopy for git push operations
        - Config-based label mapping
        """
        return CompletionProcessor(
            label_adapter=self.repository_host,
            pr_adapter=self.repository_host,
            git_adapter=GitWorkingCopy(),
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
        return SessionRestorer(
            config=self.config,
            repository_host=self.repository_host,
        )

    async def _verify_hooks_on_startup(self) -> None:
        """Verify AI meta-agent hooks are installed and effective.

        Delegates to HookVerifier for the actual verification logic.
        """
        from .control.hook_verifier import HookVerifier

        verifier = HookVerifier(self.config)
        result = await verifier.verify()
        verifier.raise_on_failure(result)

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
            self.issue_machines[issue_number] = IssueStateMachine(issue_number)
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
                session_name, issue_number, timeout_minutes=timeout_minutes
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
                pr_number, issue_number, max_rework_cycles=self.config.max_rework_cycles
            )
            logger.debug(f"Created ReviewStateMachine for PR #{pr_number}")
        return self.review_machines[pr_number]

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

    async def startup(self) -> None:
        """Handle startup - check for stale in-progress issues."""
        from .analysis import analyze_issue, get_issue_branches

        startup_start = time.time()
        self.state.startup_status = "running"

        # Emit merged configuration for debugging (YAML + command line overrides)
        self.events.publish(TraceEvent("config.merged", self.config.to_event_dict()))

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
            prs = self.repository_host.get_prs_with_label(self.config.code_review_label)
            for pr in prs:
                pr_number = pr.number
                pr_url = pr.url
                pr_body = pr.body

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
                        pr_url=pr_url,
                        branch_name=pr.branch,
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
        self.events.publish(TraceEvent("orchestrator.ready", {
            "filter_label": self.config.filter_label,
            "filter_milestone": self.config.filter_milestone,
            "agents": list(self.config.agents.keys()),
            "max_concurrent": self.config.max_concurrent_sessions,
            "startup_seconds": round(elapsed, 1),
        }))

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
                        remove_worktree(session.worktree_path)
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
                        completion_path=session.completion_path,
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
                    snapshot = self.fact_gatherer.create_snapshot(self.state, filtered_issues)
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

                    # Clear discovered facts after plan is applied
                    # The Planner has already decided what to do with them
                    if self.state.discovered_reviews:
                        logger.debug("[PLAN] Clearing %d discovered reviews after plan applied",
                                   len(self.state.discovered_reviews))
                        self.state.discovered_reviews.clear()
                    if self.state.discovered_reworks:
                        logger.debug("[PLAN] Clearing %d discovered reworks after plan applied",
                                   len(self.state.discovered_reworks))
                        self.state.discovered_reworks.clear()
                    if self.state.discovered_escalations:
                        logger.debug("[PLAN] Clearing %d discovered escalations after plan applied",
                                   len(self.state.discovered_escalations))
                        self.state.discovered_escalations.clear()
                    if self.state.discovered_failures:
                        logger.debug("[PLAN] Clearing %d discovered failures after plan applied",
                                   len(self.state.discovered_failures))
                        self.state.discovered_failures.clear()

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

        Flow:
        1. ActionApplier handles IO (labels, sessions, worktrees, issues)
        2. Orchestrator handles state updates based on results

        Args:
            plan: Plan with actions to execute
        """
        from .control.planner import Plan
        from .control.actions import (
            ActionType, LaunchSessionAction, EscalateToHumanAction,
            QueueReviewAction, QueueReworkAction, QueueTriageAction,
            CreateTriageIssueAction, CleanupSessionAction,
        )

        for action in plan.actions:
            # Respect pause mid-batch: stop applying actions if paused
            if self.state.paused:
                logger.debug("[PLAN] Stopping plan application - orchestrator paused")
                break

            try:
                # Actions that need entity lookup before IO
                if action.action_type == ActionType.LAUNCH_SESSION:
                    assert isinstance(action, LaunchSessionAction)
                    self._execute_launch_action(action)

                # Actions that can delegate directly to ActionApplier
                elif action.action_type == ActionType.ESCALATE_TO_HUMAN:
                    assert isinstance(action, EscalateToHumanAction)
                    result = self.action_applier.apply(action)
                    if result.success:
                        self._handle_escalation_state_update(action)

                elif action.action_type == ActionType.QUEUE_REVIEW:
                    assert isinstance(action, QueueReviewAction)
                    result = self.action_applier.apply(action)
                    if result.success:
                        self._handle_queue_review_state_update(action)

                elif action.action_type == ActionType.QUEUE_REWORK:
                    assert isinstance(action, QueueReworkAction)
                    result = self.action_applier.apply(action)
                    if result.success:
                        self._handle_queue_rework_state_update(action)

                elif action.action_type == ActionType.QUEUE_TRIAGE:
                    assert isinstance(action, QueueTriageAction)
                    result = self.action_applier.apply(action)
                    if result.success:
                        self._handle_queue_triage_state_update(action)

                elif action.action_type == ActionType.CREATE_TRIAGE_ISSUE:
                    assert isinstance(action, CreateTriageIssueAction)
                    result = self.action_applier.apply(action)
                    if result.success:
                        # Add to pending triage reviews
                        issue_number = result.details.get("issue_number")
                        if issue_number:
                            self.state.pending_triage_reviews.append(
                                PendingTriageReview(issue_number=issue_number, title=action.title)
                            )
                            print(f"📋 Created triage review issue #{issue_number} for {action.pr_count} PRs")

                elif action.action_type == ActionType.CLEANUP_SESSION:
                    assert isinstance(action, CleanupSessionAction)
                    result = self.action_applier.apply(action)
                    if result.success:
                        # Remove from pending_cleanups
                        self.state.pending_cleanups = [
                            c for c in self.state.pending_cleanups
                            if c.pr_number != action.pr_number
                        ]
            except Exception as e:
                logger.exception("Failed to apply action %s: %s", action, e)

    def _handle_escalation_state_update(self, action: "EscalateToHumanAction") -> None:
        """Update state after escalation action succeeds."""
        # Escalation state is handled by the label being added
        # Any additional state tracking can go here
        logger.info("[PLAN] Escalated PR #%d to needs-human (cycle %d)",
                   action.pr_number, action.rework_cycles)

    def _handle_queue_review_state_update(self, action: "QueueReviewAction") -> None:
        """Update state after queue review action succeeds."""
        from .control.actions import QueueReviewAction

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

    def _handle_queue_rework_state_update(self, action: "QueueReworkAction") -> None:
        """Update state after queue rework action succeeds."""
        from .control.actions import QueueReworkAction

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

    def _handle_queue_triage_state_update(self, action: "QueueTriageAction") -> None:
        """Update state after queue triage action succeeds."""
        from .control.actions import QueueTriageAction

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

    def _execute_queue_review_action(self, action: "QueueReviewAction") -> None:
        """Execute a queue review action.

        Adds the review to pending_reviews and adds the review label.
        This is the execution of the Planner's decision to queue a review.
        """
        from .control.actions import QueueReviewAction

        # Check if already queued (defensive)
        if any(r.pr_number == action.pr_number for r in self.state.pending_reviews):
            logger.debug("[PLAN] PR #%d already queued, skipping", action.pr_number)
            return

        # Add needs-code-review label as backup via adapter
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

        # Emit event for visibility
        self.events.publish(TraceEvent("review.queued", {
            "pr_number": action.pr_number,
            "issue_number": action.issue_number,
            "pr_url": action.pr_url,
        }))

        # Create review state machine
        review_machine = self._get_review_machine(action.pr_number, action.issue_number)
        logger.debug("[STATE_MACHINE] ReviewStateMachine for PR #%d in %s", action.pr_number, review_machine.state)
        logger.info("[PLAN] Queued review for PR #%d", action.pr_number)

    def _execute_queue_rework_action(self, action: "QueueReworkAction") -> None:
        """Execute a queue rework action.

        Adds the rework to pending_reworks.
        This is the execution of the Planner's decision to queue a rework.
        """
        from .control.actions import QueueReworkAction

        # Check if already queued (defensive)
        queued_issue_ids = {int(r.issue_key.stable_id()) for r in self.state.pending_reworks}
        if action.issue_number in queued_issue_ids:
            logger.debug("[PLAN] Issue #%d already queued for rework, skipping", action.issue_number)
            return

        # Create IssueKey via repository
        issue_key = self.repository_host.create_issue_key(action.issue_number)

        # Need agent_type - try to find it from labels or use default
        # For now, we'll get it from the discovered rework that triggered this action
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

        # Emit event for visibility
        self.events.publish(TraceEvent("rework.queued", {
            "issue_number": action.issue_number,
            "rework_cycle": action.rework_cycle,
        }))
        logger.info("[PLAN] Queued rework for issue #%d (cycle %d)", action.issue_number, action.rework_cycle)
        print(f"🔄 Queued issue #{action.issue_number} for rework (cycle {action.rework_cycle})")

    def _execute_queue_triage_action(self, action: "QueueTriageAction") -> None:
        """Execute a queue triage action.

        Adds the triage review to pending_triage_reviews.
        This is the execution of the Planner's decision to queue a triage review.
        """
        from .control.actions import QueueTriageAction

        # Check if already queued (defensive)
        if any(t.issue_number == action.issue_number for t in self.state.pending_triage_reviews):
            logger.debug("[PLAN] Issue #%d already queued for triage, skipping", action.issue_number)
            return

        # Add to pending triage reviews
        self.state.pending_triage_reviews.append(
            PendingTriageReview(
                issue_number=action.issue_number,
                title=action.title,
            )
        )

        # Emit event for visibility
        self.events.publish(TraceEvent("triage.queued", {
            "issue_number": action.issue_number,
            "title": action.title,
        }))
        logger.info("[PLAN] Queued triage for issue #%d", action.issue_number)
        print(f"[TRIAGE] Queued failure investigation for #{action.issue_number}")

    def _execute_create_triage_issue_action(self, action: "CreateTriageIssueAction") -> None:
        """Execute a create triage issue action.

        Creates the GitHub issue and adds to pending_triage_reviews.
        This is the execution of the Planner's decision to trigger triage.
        """
        from .control.actions import CreateTriageIssueAction

        issue_number = self.repository_host.create_issue(
            title=action.title,
            body=action.body,
            labels=list(action.labels),
        )
        if issue_number:
            logger.info("[PLAN] Created triage review issue #%d for %d PRs", issue_number, action.pr_count)
            print(f"📋 Created triage review issue #{issue_number} for {action.pr_count} PRs")
            # Queue for immediate processing
            self.state.pending_triage_reviews.append(
                PendingTriageReview(issue_number=issue_number, title=action.title)
            )
        else:
            logger.error("[PLAN] Failed to create triage review issue")

    def _execute_cleanup_action(self, action: "CleanupSessionAction") -> None:
        """Execute a cleanup session action.

        Closes the terminal tab and removes the worktree for a reviewed session.
        This is the execution of the Planner's decision to clean up.
        """
        from .control.actions import CleanupSessionAction
        from ._worktree_impl import remove_worktree
        from pathlib import Path

        logger.info("[CLEANUP] Processing cleanup for issue #%d (PR #%d)",
                   action.issue_number, action.pr_number)

        # Close terminal session if configured
        if action.close_tabs:
            try:
                self._terminal.kill_session(action.terminal_session_name)
                logger.info("[CLEANUP] Closed terminal session for #%d", action.issue_number)
            except Exception as e:
                logger.warning("[CLEANUP] Failed to close session for #%d: %s",
                             action.issue_number, e)

        # Remove worktree if configured
        if action.remove_worktrees:
            try:
                remove_worktree(Path(action.worktree_path))
                logger.info("[CLEANUP] Removed worktree for #%d", action.issue_number)
            except Exception as e:
                logger.warning("[CLEANUP] Failed to remove worktree for #%d: %s",
                             action.issue_number, e)

        # Remove from pending_cleanups
        self.state.pending_cleanups = [
            c for c in self.state.pending_cleanups
            if c.pr_number != action.pr_number
        ]

        # Emit event
        self.events.publish(TraceEvent("cleanup.completed", {
            "issue_number": action.issue_number,
            "pr_number": action.pr_number,
        }))
        logger.info("[CLEANUP] Completed cleanup for issue #%d", action.issue_number)

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

    def _escalate_to_needs_human(self, pr_number: int, issue_number: int, rework_cycle: int) -> None:
        """Escalate PR to needs-human after max rework cycles.

        Removes needs-rework label and adds needs-human label.
        Uses repository_host adapter for all GitHub operations.
        """
        needs_human_label = self.config.get_label_needs_human()
        rework_label = self.config.get_label_needs_rework()

        # Add needs-human label and remove needs-rework via adapter
        # Note: GitHub treats PRs as issues, so issue label operations work on PRs
        try:
            self.repository_host.add_label(pr_number, needs_human_label)
            self.repository_host.remove_label(pr_number, rework_label)
            print(f"⚠️  PR #{pr_number} escalated to {needs_human_label} after {rework_cycle} rework cycles")

            # Post comment explaining escalation via adapter
            comment = f"""## ⚠️ Escalated to Human Review

This PR has gone through {rework_cycle - 1} rework cycles without passing review.
Maximum rework cycles ({self.config.max_rework_cycles}) exceeded.

**A human needs to review and either:**
- Approve the PR manually
- Provide specific guidance for the agent
- Take over the implementation
"""
            self.repository_host.add_comment(pr_number, comment)

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
        Uses repository_host adapter for all GitHub operations.

        Returns the number of PRs that were fixed.
        """
        if not self.config.code_review_label or not self.config.repo:
            return 0

        fixed_count = 0
        code_reviewed_label = self.config.code_reviewed_label

        # Get all open PRs via adapter
        prs = self.repository_host.list_prs(state="open", limit=100)

        for pr in prs:
            # Only reconcile PRs created by the orchestrator
            if ORCHESTRATOR_PR_MARKER not in pr.body:
                continue

            # Check if it already has a review label
            has_review_label = (
                self.config.code_review_label in pr.labels or
                code_reviewed_label in pr.labels
            )

            if not has_review_label:
                # Add the needs-code-review label via adapter
                # Note: GitHub treats PRs as issues, so issue label operations work on PRs
                try:
                    self.repository_host.add_label(pr.number, self.config.code_review_label)
                    fixed_count += 1
                    print(f"🏷️  Added '{self.config.code_review_label}' to orphaned PR #{pr.number}")
                    logger.info("Reconciled label on PR #%d", pr.number)
                except Exception as e:
                    logger.warning("Failed to reconcile label on PR #%d: %s", pr.number, e)

        if fixed_count > 0:
            print(f"✅ Reconciled labels on {fixed_count} orphaned PR(s)")

        return fixed_count

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
