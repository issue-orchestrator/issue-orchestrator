"""Main orchestrator - ties everything together."""

import asyncio
import logging
import signal
import subprocess
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

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


def _emit_event(event_type: str, data: dict | None = None) -> None:
    """Emit an SSE event to connected dashboard clients.

    This is a fire-and-forget operation - if no event loop is running
    or the web module isn't imported, it silently does nothing.
    """
    try:
        from .web import broadcast_event, _event_subscribers
        # Only try to emit if there are subscribers
        if not _event_subscribers:
            logger.debug("[SSE] No subscribers, skipping event: %s", event_type)
            return
        # Schedule the async broadcast in the current event loop
        loop = asyncio.get_event_loop()
        if loop.is_running():
            logger.info("[SSE] Emitting event '%s' to %d subscribers, data=%s",
                       event_type, len(_event_subscribers), data)
            asyncio.create_task(broadcast_event(event_type, data))
        else:
            logger.debug("[SSE] Event loop not running, skipping event: %s", event_type)
    except Exception as e:
        logger.warning("[SSE] Failed to emit event %s: %s", event_type, e)

from .config import Config
from .github import (
    # Core functions (adapter-preferred but kept for backward compatibility and tests)
    list_issues, add_label, remove_label, get_issue_labels, get_open_prs_for_branch,
    # Functions still used directly (no adapter equivalent yet)
    get_latest_blocked_info, get_latest_needs_human_info,
    list_prs_with_label, create_issue,
)
# Lock files removed - using direct iTerm/active_sessions checks instead
from .models import Issue, Session, SessionStatus, OrchestratorState, PendingReview, PendingRework, PendingCTOReview, PendingCleanup, AgentConfig, ORCHESTRATOR_PR_MARKER
from .monitor import SessionMonitor
from .scheduler import Scheduler
# Terminal backend handled via adapters (see _terminal_adapter property)
from .worktree import create_worktree, remove_worktree, has_uncommitted_changes
# State machine infrastructure
from .domain.events import EventBus, IssueEvent, SessionEvent, ReviewEvent
from .domain.state_machines.issue_machine import IssueStateMachine, IssueState
from .domain.state_machines.session_machine import SessionStateMachine, SessionState
from .domain.state_machines.review_machine import ReviewStateMachine, ReviewState
from .adapters.github_adapter import GitHubAdapter


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
    """Main orchestrator that coordinates everything."""

    config: Config
    state: OrchestratorState = field(default_factory=OrchestratorState)
    # Dependency injection: adapter can be provided for testing
    _github_adapter: Optional[GitHubAdapter] = field(default=None, repr=False)
    scheduler: Scheduler = field(init=False)
    monitor: SessionMonitor = field(init=False)
    _shutdown_requested: bool = field(default=False, init=False)

    def __post_init__(self):
        self.scheduler = Scheduler(self.config)
        # Note: Monitor is initialized without session_machines initially
        # We'll update the reference after session_machines is created
        self.monitor = SessionMonitor(self.config)
        self._plugin_manager_instance = None  # Lazy init

        # State machine infrastructure
        self.event_bus = EventBus()
        self.issue_machines: dict[int, IssueStateMachine] = {}
        self.session_machines: dict[str, SessionStateMachine] = {}
        self.review_machines: dict[int, ReviewStateMachine] = {}  # keyed by PR number

        # GitHub adapter - use injected adapter or create real one
        if self._github_adapter is None:
            self._github_adapter = GitHubAdapter(self.config.repo)

        # State persistence
        from .adapters.json_store import JsonSessionStore
        # Use the state_file directory for state machine persistence
        store_path = self.config.state_file.parent / "state_machines.json"
        self.state_store = JsonSessionStore(store_path)

        # IPC server for external UI processes (lazy init, started in startup())
        self._ipc_server = None

        # Set up event handlers
        self._setup_event_handlers()

        # Recover state machines from persisted state
        self._recover_state_machines()

        # Update monitor's reference to session machines
        self.monitor.session_machines = self.session_machines

    @property
    def github_adapter(self) -> GitHubAdapter:
        """Get the GitHub adapter (always initialized after __post_init__)."""
        assert self._github_adapter is not None, "GitHub adapter not initialized"
        return self._github_adapter

    @property
    def _using_iterm2(self) -> bool:
        """Check if we're using iTerm2 mode (or web mode, which also uses iTerm2 tabs)."""
        # Check explicit terminal_adapter first, then fall back to ui_mode
        if self.config.terminal_adapter:
            return "iterm" in self.config.terminal_adapter.lower()
        return self.config.ui_mode in ("iterm2", "web")

    @property
    def _plugins(self):
        """Get the plugin manager (lazy init)."""
        if self._plugin_manager_instance is None:
            from .adapters import PluginManager

            self._plugin_manager_instance = PluginManager(
                terminal_plugin=self.config.terminal_adapter,
                ui_mode=self.config.ui_mode,
            )
        return self._plugin_manager_instance

    # ==================== IPC Server ====================
    # Unix socket server for external UI processes to receive events.

    async def _start_ipc_server(self) -> None:
        """Start the IPC server for external UI processes.

        Creates a Unix socket server and registers the LifecycleIPCPlugin
        to forward lifecycle events to all connected UI clients.
        """
        from .ipc import EventServer
        from .adapters import LifecycleIPCPlugin

        try:
            self._ipc_server = EventServer()
            await self._ipc_server.start()

            # Register lifecycle plugin to forward events to IPC
            lifecycle_plugin = LifecycleIPCPlugin(self._ipc_server)
            self._plugins.register_plugin(lifecycle_plugin, name="lifecycle_ipc")

            logger.info("IPC server started at %s", self._ipc_server.socket_path)
        except Exception as e:
            logger.warning("Failed to start IPC server: %s", e)
            self._ipc_server = None

    async def _stop_ipc_server(self) -> None:
        """Stop the IPC server gracefully."""
        if self._ipc_server:
            try:
                await self._ipc_server.stop()
                logger.info("IPC server stopped")
            except Exception as e:
                logger.warning("Error stopping IPC server: %s", e)
            self._ipc_server = None

    async def _verify_hooks_on_startup(self) -> None:
        """Verify AI meta-agent hooks are installed and effective.

        This check ensures that agents cannot bypass safety guardrails
        like --no-verify. If verification fails and skip_verification
        is not enabled, startup will be blocked.
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

        # State persistence on significant state changes
        # Subscribe to all state transitions to ensure state is persisted
        self.event_bus.subscribe(SessionEvent.STARTED, self._on_state_change_persist)
        self.event_bus.subscribe(SessionEvent.COMPLETED, self._on_state_change_persist)
        self.event_bus.subscribe(SessionEvent.FAILED, self._on_state_change_persist)
        self.event_bus.subscribe(SessionEvent.TIMED_OUT, self._on_state_change_persist)
        self.event_bus.subscribe(SessionEvent.BLOCKED, self._on_state_change_persist)
        self.event_bus.subscribe(SessionEvent.NEEDS_HUMAN, self._on_state_change_persist)
        self.event_bus.subscribe(IssueEvent.CLAIMED, self._on_state_change_persist)
        self.event_bus.subscribe(IssueEvent.SESSION_STARTED, self._on_state_change_persist)
        self.event_bus.subscribe(IssueEvent.BLOCKED, self._on_state_change_persist)
        self.event_bus.subscribe(IssueEvent.NEEDS_HUMAN, self._on_state_change_persist)
        self.event_bus.subscribe(IssueEvent.UNBLOCKED, self._on_state_change_persist)
        self.event_bus.subscribe(IssueEvent.PR_CREATED, self._on_state_change_persist)
        self.event_bus.subscribe(IssueEvent.COMPLETED, self._on_state_change_persist)
        self.event_bus.subscribe(IssueEvent.RELEASED, self._on_state_change_persist)
        self.event_bus.subscribe(ReviewEvent.REVIEW_STARTED, self._on_state_change_persist)
        self.event_bus.subscribe(ReviewEvent.APPROVED, self._on_state_change_persist)
        self.event_bus.subscribe(ReviewEvent.CHANGES_REQUESTED, self._on_state_change_persist)
        self.event_bus.subscribe(ReviewEvent.REWORK_STARTED, self._on_state_change_persist)
        self.event_bus.subscribe(ReviewEvent.REWORK_COMPLETED, self._on_state_change_persist)
        self.event_bus.subscribe(ReviewEvent.MERGED, self._on_state_change_persist)
        self.event_bus.subscribe(ReviewEvent.CLOSED, self._on_state_change_persist)

        logger.info("Event handlers configured for state machine integration")

    def _on_issue_claimed(self, event) -> None:
        """Handle issue claimed event from state machine."""
        logger.debug(f"[STATE_MACHINE] Issue #{event.entity_id} claimed")
        _emit_event("issue_claimed", {"issue_number": event.entity_id})

    def _on_issue_session_started(self, event) -> None:
        """Handle issue session started event from state machine."""
        logger.debug(f"[STATE_MACHINE] Issue #{event.entity_id} session started")
        _emit_event("issue_session_started", {"issue_number": event.entity_id})

    def _on_issue_pr_created(self, event) -> None:
        """Handle issue PR created event from state machine."""
        logger.debug(f"[STATE_MACHINE] Issue #{event.entity_id} PR created")
        _emit_event("issue_pr_created", {"issue_number": event.entity_id})

    def _on_issue_completed(self, event) -> None:
        """Handle issue completed event from state machine."""
        logger.debug(f"[STATE_MACHINE] Issue #{event.entity_id} completed")
        _emit_event("issue_completed", {"issue_number": event.entity_id})

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
        _emit_event("review_approved", {"pr_number": pr_number})

    def _on_review_changes_requested(self, event) -> None:
        """Handle review changes requested event from state machine."""
        pr_number = event.entity_id
        rework_count = event.data.get("rework_count", 0)
        logger.info(f"[STATE_MACHINE] PR #{pr_number} changes requested (rework cycle {rework_count})")
        _emit_event("review_changes_requested", {"pr_number": pr_number, "rework_count": rework_count})

    # ==================== Label Sync Handlers ====================

    def _sync_label_in_progress(self, event) -> None:
        """Add in-progress label when issue is claimed."""
        try:
            logger.debug("[ADAPTER] Using GitHubAdapter for add_label")
            self.github_adapter.add_label(event.entity_id, "in-progress")
            logger.debug(f"[LABEL_SYNC] Added 'in-progress' to #{event.entity_id}")
        except Exception as e:
            logger.warning(f"[LABEL_SYNC] Failed to add label: {e}")

    def _sync_label_blocked(self, event) -> None:
        """Add blocked label when issue is blocked."""
        reason = event.data.get('reason', '')
        label = f"blocked-{reason}" if reason else "blocked"
        try:
            logger.debug("[ADAPTER] Using GitHubAdapter for add_label")
            self.github_adapter.add_label(event.entity_id, label)
            logger.debug(f"[LABEL_SYNC] Added '{label}' to #{event.entity_id}")
        except Exception as e:
            logger.warning(f"[LABEL_SYNC] Failed to add label: {e}")

    def _sync_label_needs_human(self, event) -> None:
        """Add needs-human label."""
        try:
            logger.debug("[ADAPTER] Using GitHubAdapter for add_label")
            self.github_adapter.add_label(event.entity_id, "blocked-needs-human")
            logger.debug(f"[LABEL_SYNC] Added 'blocked-needs-human' to #{event.entity_id}")
        except Exception as e:
            logger.warning(f"[LABEL_SYNC] Failed to add label: {e}")

    def _sync_label_unblocked(self, event) -> None:
        """Remove blocking labels when unblocked."""
        try:
            logger.debug("[ADAPTER] Using GitHubAdapter for get_issue_labels and remove_label")
            labels = self.github_adapter.get_issue_labels(event.entity_id)
            for label in labels:
                if label.startswith("blocked"):
                    self.github_adapter.remove_label(event.entity_id, label)
                    logger.debug(f"[LABEL_SYNC] Removed '{label}' from #{event.entity_id}")
        except Exception as e:
            logger.warning(f"[LABEL_SYNC] Failed to remove labels: {e}")

    def _sync_label_completed(self, event) -> None:
        """Remove in-progress label when completed."""
        try:
            logger.debug("[ADAPTER] Using GitHubAdapter for remove_label")
            self.github_adapter.remove_label(event.entity_id, "in-progress")
            logger.debug(f"[LABEL_SYNC] Removed 'in-progress' from #{event.entity_id}")
        except Exception as e:
            logger.warning(f"[LABEL_SYNC] Failed to remove label: {e}")

    def _sync_label_released(self, event) -> None:
        """Remove in-progress label when released."""
        try:
            logger.debug("[ADAPTER] Using GitHubAdapter for remove_label")
            self.github_adapter.remove_label(event.entity_id, "in-progress")
            logger.debug(f"[LABEL_SYNC] Removed 'in-progress' from #{event.entity_id}")
        except Exception as e:
            logger.warning(f"[LABEL_SYNC] Failed to remove label: {e}")

    # ==================== End State Machine Helpers ====================

    # ==================== State Persistence ====================

    def _persist_state_machines(self) -> None:
        """Persist current state machine states to disk.

        This method saves the state of all active state machines to enable
        recovery after orchestrator restarts.
        """
        try:
            # Persist session machines
            for session_id, machine in self.session_machines.items():
                self.state_store.save_session_state(
                    session_id=session_id,
                    issue_number=machine.issue_number,
                    state=machine.state.value,  # Convert enum to string
                    started_at=machine.started_at,
                    metadata={'timeout_minutes': machine.timeout_minutes}
                )

            # Persist issue machines
            for issue_number, machine in self.issue_machines.items():
                self.state_store.save_issue_state(
                    issue_number=issue_number,
                    state=machine.state.value  # Convert enum to string
                )

            # Persist review machines
            for pr_number, machine in self.review_machines.items():
                self.state_store.save_review_state(
                    pr_number=pr_number,
                    state=machine.state.value,  # Convert enum to string
                    rework_count=machine.rework_count,
                    metadata={'issue_number': machine.issue_number}
                )

            logger.debug("[PERSISTENCE] State machines persisted successfully")
        except Exception as e:
            logger.error(f"[PERSISTENCE] Failed to persist state machines: {e}")

    def _recover_state_machines(self) -> None:
        """Recover state machines from persisted state after restart.

        This method restores state machines from the persisted state store,
        allowing the orchestrator to resume where it left off after a crash
        or restart.
        """
        try:
            # Recover session machines
            for session_id, data in self.state_store.get_all_sessions().items():
                if session_id not in self.session_machines:
                    try:
                        initial_state = SessionState(data["state"])
                        timeout_minutes = data.get("metadata", {}).get("timeout_minutes") or self.config.session_timeout_minutes

                        machine = SessionStateMachine(
                            session_id=session_id,
                            issue_number=data["issue_number"],
                            event_bus=self.event_bus,
                            initial_state=initial_state,
                            timeout_minutes=timeout_minutes
                        )

                        # Restore started_at if available
                        if data.get("started_at"):
                            machine.started_at = datetime.fromisoformat(data["started_at"])

                        self.session_machines[session_id] = machine
                        logger.info(f"[RECOVERY] Restored SessionStateMachine for {session_id} in state {data['state']}")
                    except Exception as e:
                        logger.warning(f"[RECOVERY] Failed to restore session {session_id}: {e}")

            # Recover issue machines
            for issue_number_str, data in self.state_store._cache.get("issue_machines", {}).items():
                issue_number = int(issue_number_str)
                if issue_number not in self.issue_machines:
                    try:
                        initial_state = IssueState(data["state"])
                        machine = IssueStateMachine(
                            issue_number=issue_number,
                            event_bus=self.event_bus,
                            initial_state=initial_state
                        )
                        self.issue_machines[issue_number] = machine
                        logger.info(f"[RECOVERY] Restored IssueStateMachine for issue #{issue_number} in state {data['state']}")
                    except Exception as e:
                        logger.warning(f"[RECOVERY] Failed to restore issue #{issue_number}: {e}")

            # Recover review machines
            for pr_number_str, data in self.state_store._cache.get("review_machines", {}).items():
                pr_number = int(pr_number_str)
                if pr_number not in self.review_machines:
                    try:
                        initial_state = ReviewState(data["state"])
                        # We need the issue number - try to get it from metadata or skip
                        issue_number = data.get("metadata", {}).get("issue_number")
                        if not issue_number:
                            logger.warning(f"[RECOVERY] Skipping PR #{pr_number} - no issue_number in metadata")
                            continue

                        machine = ReviewStateMachine(
                            pr_number=pr_number,
                            issue_number=issue_number,
                            event_bus=self.event_bus,
                            initial_state=initial_state,
                            max_rework_cycles=self.config.max_rework_cycles
                        )
                        machine.rework_count = data.get("rework_count", 0)
                        self.review_machines[pr_number] = machine
                        logger.info(f"[RECOVERY] Restored ReviewStateMachine for PR #{pr_number} in state {data['state']}")
                    except Exception as e:
                        logger.warning(f"[RECOVERY] Failed to restore review PR #{pr_number}: {e}")

            logger.info("[RECOVERY] State machine recovery completed")
        except Exception as e:
            logger.error(f"[RECOVERY] Failed to recover state machines: {e}")

    def _on_state_change_persist(self, event) -> None:
        """Persist state after significant state changes.

        This handler is triggered on terminal state transitions to ensure
        state is saved to disk.

        Args:
            event: The state change event
        """
        self._persist_state_machines()

    # ==================== End State Persistence ====================

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
                    logger.warning("Could not find worktree for session %s - skipping", session_name)
                    continue

                # Fetch issue details from GitHub to get agent type
                logger.debug("[ADAPTER] Using GitHubAdapter for list_issues")
                issues = self.github_adapter.list_issues(limit=200)
                issue_obj = None
                agent_config = None

                for issue in issues:
                    if issue.number == issue_number:
                        issue_obj = issue
                        if issue.agent_type:
                            agent_config = self.config.agents.get(issue.agent_type)
                        break

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
        """Create a session using the terminal plugin.

        Returns:
            True if session was created successfully, False otherwise.
        """
        session_id = self._extract_session_number(session_name)
        return self._plugins.create_session(
            session_id=session_id,
            command=command,
            working_dir=str(working_dir),
            title=title,
        )

    def _session_exists(self, session_name: str) -> bool:
        """Check if a session exists using the terminal plugin."""
        session_id = self._extract_session_number(session_name)
        return self._plugins.session_exists(session_id)

    def _kill_session(self, session_name: str) -> None:
        """Kill a session using the terminal plugin."""
        session_id = self._extract_session_number(session_name)
        self._plugins.kill_session(session_id)

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

        # Start IPC server for external UI processes
        self.state.startup_message = "Starting IPC server..."
        await self._start_ipc_server()

        # Verify AI meta-agent hooks are installed and working
        self.state.startup_message = "Verifying hook enforcement..."
        await self._verify_hooks_on_startup()

        self.state.startup_message = "Cleaning up stale claims..."
        logger.info("Starting up - checking for stale in-progress issues...")
        print("Checking for stale in-progress issues...")

        # Clean up idle terminal sessions (tabs at shell prompt where Claude has exited)
        self.state.startup_message = "Cleaning up idle terminal sessions..."
        closed_tabs = self._plugins.cleanup_idle_sessions()
        if closed_tabs:
            logger.info("Closed %d idle terminal sessions", closed_tabs)
            print(f"  Closed {closed_tabs} idle terminal sessions")

        # Discover and restore tracking for running sessions
        self.state.startup_message = "Discovering running sessions..."
        running = self._plugins.discover_running_sessions()
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
            issues = self.github_adapter.list_issues(
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
                    self.github_adapter.remove_label(issue.number, self.config.get_label_in_progress())

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

                # Verify PR was created via agent-done (has verification token)
                from .agent_done import verify_pr_token, extract_pr_verification_status
                has_marker, token = extract_pr_verification_status(pr_body)
                if has_marker:
                    is_verified = verify_pr_token(pr_body, issue_number)
                    if not is_verified:
                        logger.warning(f"PR #{pr_number}: Invalid verification token (issue #{issue_number})")
                        print(f"  PR #{pr_number}: ⚠️  Invalid verification token - manual review needed")
                else:
                    # PR created without agent-done - flag it
                    logger.warning(f"PR #{pr_number}: Missing verification token (not created via agent-done)")
                    print(f"  PR #{pr_number}: ⚠️  No verification token - created outside agent-done")

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

        # Check for pending CTO review issues (recovery after crash/restart)
        if self.config.cto_review_agent:
            self.state.startup_message = "Checking for pending CTO review issues..."
            print("\nChecking for pending CTO review issues...")
            logger.debug("[ADAPTER] Using GitHubAdapter for list_issues")
            cto_issues = self.github_adapter.list_issues(
                labels=[self.config.cto_review_agent],
                limit=20,
            )
            for cto_issue in cto_issues:
                # Skip if already in active session
                session_name = f"issue-{cto_issue.number}"
                if self._session_exists(session_name):
                    print(f"  CTO issue #{cto_issue.number}: Already running")
                    continue

                # Skip if already queued
                if any(r.issue_number == cto_issue.number for r in self.state.pending_cto_reviews):
                    print(f"  CTO issue #{cto_issue.number}: Already queued")
                    continue

                # Queue for processing
                self.state.pending_cto_reviews.append(
                    PendingCTOReview(
                        issue_number=cto_issue.number,
                        title=cto_issue.title,
                    )
                )
                print(f"  CTO issue #{cto_issue.number}: Queued ({cto_issue.title})")

            if self.state.pending_cto_reviews:
                print(f"  Found {len(self.state.pending_cto_reviews)} CTO review(s) to process")

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
        _emit_event("startup_complete")

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

        log_transition("issue", issue.number, "AVAILABLE", "LAUNCHING", "no conflicts")

        # Use agent's repo_root if set, otherwise fall back to config.repo_root
        repo_root = agent_config.repo_root or self.config.repo_root

        # Create worktree (sibling to repo, named {repo}-{issue_number})
        step_start = time.time()
        logger.debug("Creating worktree for issue #%d", issue.number)
        print(f"[launch] Creating worktree for issue #{issue.number}...")
        worktree_path, branch_name = create_worktree(
            repo_root=repo_root,
            issue_number=issue.number,
            issue_title=issue.title,
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
        self.github_adapter.add_label(issue.number, self.config.get_label_in_progress())
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
            self.github_adapter.remove_label(issue.number, self.config.get_label_in_progress())
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
        _emit_event("session_started", {"issue_number": issue.number, "title": issue.title})

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
        _emit_event("session_completed", {"issue_number": session.issue.number, "status": status.value})

        # Remove from active sessions (no lock to release)
        self.state.active_sessions = [
            s for s in self.state.active_sessions
            if s.issue.number != session.issue.number
        ]

        # Let monitor handle label updates
        self.monitor.handle_completion(session, status)

        # Track completion
        if status == SessionStatus.COMPLETED:
            self.state.completed_today.append(session.issue.number)

        # Record in session history
        pr_url = None
        prs = None
        if status == SessionStatus.COMPLETED:
            logger.debug("[ADAPTER] Using GitHubAdapter for get_prs_for_branch")
            pr_infos = self.github_adapter.get_prs_for_branch(session.branch_name)
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
            if self.config.cto_review_agent:
                # CTO workflow: defer until CTO review passes
                should_defer_cleanup = self.config.cleanup.with_cto.close_ai_session_tabs
            elif self.config.code_review_agent:
                # Code review only: defer if configured to wait
                should_defer_cleanup = (
                    self.config.cleanup.without_cto.wait_for_code_review
                    and self.config.cleanup.without_cto.close_ai_session_tabs
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
                if self.config.cleanup.without_cto.close_ai_session_tabs or not self.config.code_review_agent:
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

        # Trigger CTO review on failure if configured
        if status in (SessionStatus.FAILED, SessionStatus.TIMED_OUT):
            self._queue_cto_failure_review(session, status)

    def _queue_cto_failure_review(self, session: Session, status: SessionStatus) -> None:
        """Queue a CTO review to investigate a session failure.

        Only queues if:
        - cto_review_on_failure is enabled (default: True)
        - cto_review_agent is configured
        - No CTO review is already pending for this issue
        """
        if not self.config.cto_review_on_failure:
            logger.debug("[CTO] Skipping failure review - cto_review_on_failure disabled")
            return

        if not self.config.cto_review_agent:
            logger.debug("[CTO] Skipping failure review - cto_review_agent not configured")
            return

        # Check if already queued
        already_queued = any(
            r.issue_number == session.issue.number
            for r in self.state.pending_cto_reviews
        )
        if already_queued:
            logger.debug("[CTO] Skipping failure review - already queued for #%d", session.issue.number)
            return

        logger.info("[CTO] Queuing failure review for issue #%d (%s)",
                   session.issue.number, status.value)
        print(f"[CTO] Queuing failure investigation for #{session.issue.number}")

        self.state.pending_cto_reviews.append(
            PendingCTOReview(
                issue_number=session.issue.number,
                title=f"Investigate: {session.issue.title} ({status.value})",
            )
        )

    async def run_loop(self) -> None:
        """Main orchestration loop."""
        print("Starting orchestration loop...")

        # Reconcile any orphaned PR labels on startup
        self.reconcile_orphaned_pr_labels()

        last_cache_update = time.time()
        last_ui_update = time.time()
        ui_update_interval = 30  # Emit state_changed every 30 seconds for UI refresh

        loop_iteration = 0
        while not self._shutdown_requested:
            loop_iteration += 1
            logger.info("[LOOP] Iteration %d - active=%d, pending_reviews=%d, paused=%s",
                       loop_iteration, len(self.state.active_sessions),
                       len(self.state.pending_reviews), self.state.paused)

            try:
                # Check status of all active sessions
                for session in list(self.state.active_sessions):
                    status = self.monitor.check_session(session)

                    if status != SessionStatus.RUNNING:
                        self.handle_session_completion(session, status)

                # Scan for PRs needing code review and process them
                self.scan_needs_code_review_prs()
                self.process_pending_reviews()

                # Scan for PRs needing rework and process them
                self.scan_needs_rework_prs()
                self.process_pending_reworks()

                # Process pending CTO reviews (from failures or batch trigger)
                self.process_pending_cto_reviews()

                # Check if CTO review should be triggered (batch threshold)
                self.check_cto_review_trigger()

                # Process deferred cleanups (sessions waiting for review to complete)
                self.process_deferred_cleanups()

                # Check if we've hit the max issues limit for this session
                max_issues = self.config.max_issues_to_start
                hit_max_issues = max_issues > 0 and self.state.issues_started_count >= max_issues

                # PRIORITY: Reviews before new issues
                # Only launch new issues if there are no pending reviews
                # This ensures completed work (PRs) gets reviewed before starting new work
                has_pending_reviews = bool(self.state.pending_reviews)
                has_pending_reworks = bool(self.state.pending_reworks)
                has_pending_cto = bool(self.state.pending_cto_reviews)

                if has_pending_reviews or has_pending_reworks or has_pending_cto:
                    logger.info("[PRIORITY] Skipping new issues - %d reviews, %d reworks, %d CTO pending",
                               len(self.state.pending_reviews), len(self.state.pending_reworks),
                               len(self.state.pending_cto_reviews))

                # If not paused, not at max issues limit, no pending reviews, and have capacity, launch more sessions
                if not self.state.paused and not hit_max_issues and not has_pending_reviews and not has_pending_reworks and not has_pending_cto:
                    available_slots = self.config.max_concurrent_sessions - len(self.state.active_sessions)

                    if available_slots > 0:
                        # Get available issues
                        all_issues = []
                        for agent_label in self.config.agents.keys():
                            logger.debug("[ADAPTER] Using GitHubAdapter for list_issues")
                            issues = self.github_adapter.list_issues(
                                labels=self._build_labels(agent_label),
                                milestone=self._get_milestone_filter(),
                                limit=self.config.issue_fetch_limit,
                            )
                            all_issues.extend(issues)

                        available = self.scheduler.get_available_issues(all_issues)

                        # Filter out issues already in session history (already processed today)
                        history_numbers = {e.issue_number for e in self.state.session_history}
                        active_numbers = {s.issue.number for s in self.state.active_sessions}
                        exclude_numbers = history_numbers | active_numbers
                        available = [i for i in available if i.number not in exclude_numbers]

                        # Filter to single issue if specified
                        if self.config.filter_issue:
                            available = [i for i in available if i.number == self.config.filter_issue]

                        sorted_issues = self.scheduler.sort_by_priority(available)

                        # Pick next batch
                        to_launch = self.scheduler.pick_next_batch(
                            sorted_issues,
                            len(self.state.active_sessions),
                            self.state.priority_queue,
                        )

                        for issue in to_launch:
                            # Check pause and limit before each launch (might change mid-batch)
                            if self.state.paused:
                                print("Paused - stopping batch launch")
                                break
                            if max_issues > 0 and self.state.issues_started_count >= max_issues:
                                break
                            try:
                                session = self.launch_session(issue)
                                if session is None:
                                    # Issue was already claimed by another instance
                                    continue
                                # Successfully launched - increment counter
                                self.state.issues_started_count += 1
                            except Exception as e:
                                print(f"Failed to launch session for #{issue.number}: {e}")

                # Periodically refresh the queue cache for the dashboard
                cache_age = time.time() - last_cache_update
                if cache_age >= self.config.queue_refresh_seconds:
                    self.update_queue_cache()
                    last_cache_update = time.time()

                # Periodically emit state_changed for UI to update runtimes
                # This ensures "Starting" transitions to "Active" as time passes
                ui_age = time.time() - last_ui_update
                if ui_age >= ui_update_interval and self.state.active_sessions:
                    _emit_event("state_changed", {
                        "active_count": len(self.state.active_sessions),
                        "sessions": [s.issue.number for s in self.state.active_sessions],
                    })
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

        # Stop IPC server
        if self._ipc_server:
            asyncio.create_task(self._stop_ipc_server())

        self._shutdown_requested = True

    def pause(self) -> None:
        """Pause - don't start new sessions."""
        self.state.paused = True
        print("Orchestrator paused - will finish current sessions but not start new ones")
        _emit_event("paused")

    def resume(self) -> None:
        """Resume after pause."""
        self.state.paused = False
        print("Orchestrator resumed")
        _emit_event("resumed")

    def update_queue_cache(self) -> None:
        """Update the cached queue issues for instant dashboard pagination.

        This should be called after startup and periodically in run_loop.
        """
        from .audit import get_queue_issues
        try:
            queue_issues = get_queue_issues(self.config, self.state)
            self.state.cached_queue_issues = queue_issues
            logger.debug("Updated queue cache with %d issues", len(queue_issues))
        except Exception as e:
            logger.warning("Failed to update queue cache: %s", e)

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

    def process_pending_cto_reviews(self) -> None:
        """Process any pending CTO reviews (failure investigations or batch reviews).

        Called each loop iteration to launch CTO sessions.
        Respects max_concurrent_sessions and paused state.
        CTO reviews are treated with same priority as code reviews.
        """
        if not self.config.cto_review_agent:
            return  # CTO not configured

        if not self.state.pending_cto_reviews:
            return  # Normal case when queue is empty

        # Don't start reviews while paused
        if self.state.paused:
            logger.info("[CTO] Skipping CTO reviews - orchestrator paused")
            return

        # Check capacity
        available_slots = self.config.max_concurrent_sessions - len(self.state.active_sessions)
        if available_slots <= 0:
            logger.info("[CTO] Skipping CTO reviews - no capacity (active=%d, max=%d)",
                       len(self.state.active_sessions), self.config.max_concurrent_sessions)
            return

        logger.info("[CTO] Processing %d pending CTO reviews (capacity=%d)",
                   len(self.state.pending_cto_reviews), available_slots)

        # Launch CTO reviews up to capacity
        for cto_review in list(self.state.pending_cto_reviews)[:available_slots]:
            logger.info("[CTO] Launching CTO review for issue #%d: %s",
                       cto_review.issue_number, cto_review.title)
            try:
                self._launch_cto_session(cto_review)
                # Remove from queue after successful launch
                self.state.pending_cto_reviews = [
                    r for r in self.state.pending_cto_reviews
                    if r.issue_number != cto_review.issue_number
                ]
            except Exception as e:
                logger.exception("[CTO] Failed to launch CTO review for #%d: %s",
                                cto_review.issue_number, e)
                print(f"[CTO] Failed to launch CTO review for #{cto_review.issue_number}: {e}")
                # Remove from queue to prevent infinite retry loop
                self.state.pending_cto_reviews = [
                    r for r in self.state.pending_cto_reviews
                    if r.issue_number != cto_review.issue_number
                ]

    def _launch_cto_session(self, cto_review: PendingCTOReview) -> None:
        """Launch a CTO session to investigate a failure or review PRs.

        Creates a worktree, launches the CTO agent, and tracks the session.
        """
        cto_agent_name = self.config.cto_review_agent
        if not cto_agent_name:
            raise ValueError("No CTO review agent configured")

        agent_config = self.config.agents.get(cto_agent_name)
        if not agent_config:
            raise ValueError(f"No agent config for {cto_agent_name}")

        # Create a synthetic Issue for the CTO session
        # This allows reusing the normal session launch flow
        cto_issue = Issue(
            number=cto_review.issue_number,
            title=cto_review.title,
            labels=[cto_agent_name],
        )

        # Launch using normal flow
        session = self.launch_session(cto_issue)
        if session:
            print(f"[CTO] Launched investigation session for #{cto_review.issue_number}")

    def process_deferred_cleanups(self) -> None:
        """Process deferred cleanups for sessions awaiting review completion.

        Checks pending cleanups and performs cleanup when:
        - CTO workflow: PR has cto-reviewed label
        - Code review workflow: PR has code-reviewed label

        Called each loop iteration.
        """
        if not self.state.pending_cleanups:
            return  # Nothing to process

        # Determine which label indicates review is complete
        if self.config.cto_review_agent:
            cleanup_label = self.config.cto_reviewed_label
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
                    self.config.cleanup.with_cto.close_ai_session_tabs
                    if self.config.cto_review_agent
                    else self.config.cleanup.without_cto.close_ai_session_tabs
                )
                if close_tabs:
                    try:
                        self._kill_session(pending.terminal_session_name)
                        logger.info(f"[CLEANUP] Closed terminal session for #{pending.issue_number}")
                    except Exception as e:
                        logger.warning(f"[CLEANUP] Failed to close session for #{pending.issue_number}: {e}")

                # Remove worktree if configured
                remove_wt = (
                    self.config.cleanup.with_cto.remove_worktrees
                    if self.config.cto_review_agent
                    else self.config.cleanup.without_cto.remove_worktrees
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
        (have cto-reviewed or code-reviewed label) but weren't cleaned up before
        the orchestrator stopped.

        Uses centralized naming conventions to derive worktree paths.
        """
        import re

        # Determine which label indicates cleanup is due
        if self.config.cto_review_agent:
            cleanup_label = self.config.cto_reviewed_label
            close_tabs = self.config.cleanup.with_cto.close_ai_session_tabs
            remove_wt = self.config.cleanup.with_cto.remove_worktrees
        elif self.config.code_review_agent:
            cleanup_label = self.config.code_reviewed_label
            close_tabs = self.config.cleanup.without_cto.close_ai_session_tabs
            remove_wt = self.config.cleanup.without_cto.remove_worktrees
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
            # Extract issue number from branch name (e.g., "issue-123-description" -> 123)
            branch = pr.get("headRefName", "")
            match = re.match(r'issue-(\d+)', branch)
            if not match:
                continue

            issue_number = int(match.group(1))
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

    def check_cto_review_trigger(self) -> None:
        """Check if we should trigger a CTO batch review based on PR count.

        Creates a review issue if:
        - cto_review_agent is configured
        - cto_review_threshold > 0
        - Number of code-reviewed PRs >= threshold
        - No existing open CTO review issue exists
        - Orchestrator is not paused
        """
        # Don't trigger new reviews while paused
        if self.state.paused:
            logger.debug("[CTO] Skipped - orchestrator paused")
            return

        # Check if CTO review is configured
        if not self.config.cto_review_agent:
            logger.debug("[CTO] Skipped - cto_review_agent not configured")
            return
        if self.config.cto_review_threshold <= 0:
            logger.debug("[CTO] Skipped - threshold is 0 (manual only)")
            return

        # Label to watch: either explicit cto_review_label or code_reviewed_label
        watch_label = self.config.cto_review_label or self.config.code_reviewed_label
        if not watch_label:
            logger.debug("[CTO] Skipped - no watch label configured")
            return

        # Count PRs ready for CTO review
        prs = list_prs_with_label(self.config.repo, watch_label)
        pr_count = len(prs)
        threshold = self.config.cto_review_threshold

        # Log the check (audit trail)
        logger.info("[CTO] Check: %d PRs with '%s' label (threshold: %d)",
                   pr_count, watch_label, threshold)

        if pr_count < threshold:
            logger.info("[CTO] Not triggered - %d/%d PRs (need %d more)",
                       pr_count, threshold, threshold - pr_count)
            return

        # Check if a CTO review issue already exists (avoid duplicates)
        logger.debug("[ADAPTER] Using GitHubAdapter for list_issues")
        existing = self.github_adapter.list_issues(
            labels=[self.config.cto_review_agent],
            limit=10,
        )
        for issue in existing:
            if "Batch Review" in issue.title or "CTO Review" in issue.title:
                logger.info("[CTO] Skipped - existing CTO review issue #%d already open",
                           issue.number)
                return

        # Create the CTO review issue
        logger.info("[CTO] TRIGGERING batch review for %d PRs", pr_count)
        pr_list = "\n".join(f"- PR #{pr['number']}: {pr['title']}" for pr in prs)
        body = f"""## CTO Batch Review Triggered

{len(prs)} PRs have passed code review and are ready for CTO review:

{pr_list}

Review these PRs for patterns, architectural concerns, and process improvements.
Flip labels from `{watch_label}` to `{self.config.cto_reviewed_label}` after review.
"""
        title = f"CTO Batch Review: {len(prs)} PRs pending"
        issue_number = create_issue(
            self.config.repo,
            title=title,
            body=body,
            labels=[self.config.cto_review_agent],
        )
        if issue_number:
            logger.info("[CTO] Created CTO review issue #%d for %d PRs", issue_number, pr_count)
            print(f"📋 Created CTO review issue #{issue_number} for {len(prs)} PRs")
            # Queue for immediate processing
            self.state.pending_cto_reviews.append(
                PendingCTOReview(issue_number=issue_number, title=title)
            )
        else:
            logger.error("[CTO] Failed to create CTO review issue")

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

            # Check if already queued or being processed
            if any(r.pr_number == pr_number for r in self.state.pending_reworks):
                continue

            # Check if already being worked on
            if any(s.issue.number == pr_number for s in self.state.active_sessions):
                continue

            # Get the PR details to find associated issue and branch
            import re
            pr_body = pr.get("body", "")
            issue_match = re.search(r"Closes #(\d+)", pr_body)
            issue_number = int(issue_match.group(1)) if issue_match else pr_number

            branch_name = pr.get("headRefName", f"{issue_number}-rework")

            # Determine rework cycle from labels
            rework_cycle = self._get_rework_cycle_from_labels(pr.get("labels", []))

            # Check if we've exceeded max rework cycles
            if rework_cycle > self.config.max_rework_cycles:
                self._escalate_to_needs_human(pr_number, issue_number, rework_cycle)
                continue

            # Queue for rework
            rework = PendingRework(
                issue_number=issue_number,
                pr_number=pr_number,
                pr_url=pr.get("url", f"https://github.com/{self.config.repo}/pull/{pr_number}"),
                branch_name=branch_name,
                rework_cycle=rework_cycle,
            )
            self.state.pending_reworks.append(rework)
            print(f"🔄 Queued PR #{pr_number} for rework (cycle {rework_cycle})")

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
        """
        # Find the original issue to get the agent type
        logger.debug("[ADAPTER] Using GitHubAdapter for list_issues")
        issues = self.github_adapter.list_issues(limit=200)
        original_issue = None
        for issue in issues:
            if issue.number == rework.issue_number:
                original_issue = issue
                break

        if not original_issue:
            print(f"Warning: Could not find issue #{rework.issue_number} for rework")
            return None

        agent_label = original_issue.agent_type
        if not agent_label:
            print(f"Warning: Issue #{rework.issue_number} has no agent label")
            return None

        agent_config = self.config.agents.get(agent_label)
        if not agent_config:
            print(f"Warning: No agent config for {agent_label}")
            return None

        # Check if already being worked on (no lock files - direct reality check)
        session_name = f"rework-{rework.issue_number}"
        if any(s.tmux_session_name == session_name for s in self.state.active_sessions):
            log_transition("rework", rework.issue_number, "QUEUED", "SKIP", "already in active_sessions")
            self.state.pending_reworks = [r for r in self.state.pending_reworks if r.issue_number != rework.issue_number]
            return None

        if self._session_exists(session_name):
            log_transition("rework", rework.issue_number, "QUEUED", "SKIP", "iTerm tab already running")
            self.state.pending_reworks = [r for r in self.state.pending_reworks if r.issue_number != rework.issue_number]
            return None

        log_transition("rework", rework.issue_number, "QUEUED", "LAUNCHING", f"no conflicts, cycle={rework.rework_cycle}")

        # Use agent's repo_root if set, otherwise fall back to config.repo_root
        repo_root = agent_config.repo_root or self.config.repo_root

        # Create worktree for rework (checks out the existing PR branch)
        worktree_path, _ = create_worktree(
            repo_root=repo_root,
            issue_number=rework.issue_number,
            issue_title=f"Rework #{rework.pr_number}",
            branch_name=rework.branch_name,  # Use existing PR branch
            enforce_hooks=self.config.enforce_hooks,
            pre_push_hook=self.config.pre_push_hook,
        )

        # Build command - include rework context
        command = agent_config.get_command(
            issue_number=rework.issue_number,
            issue_title=f"Rework PR #{rework.pr_number} (cycle {rework.rework_cycle})",
            worktree=worktree_path,
            pr_number=rework.pr_number,
        )

        # Create session
        session_name = f"rework-{rework.issue_number}"
        self._create_session(session_name, command, worktree_path, title=f"Rework #{rework.issue_number}")

        session = Session(
            issue=original_issue,
            agent_config=agent_config,
            tmux_session_name=session_name,
            worktree_path=worktree_path,
            branch_name=rework.branch_name,
        )

        self.state.active_sessions.append(session)
        log_transition("rework", rework.issue_number, "LAUNCHING", "ACTIVE", f"session launched, cycle={rework.rework_cycle}")
        print(f"🔧 Launched rework session for issue #{rework.issue_number} (cycle {rework.rework_cycle})")

        # Update rework cycle label on PR
        self._update_rework_cycle_label(rework.pr_number, rework.rework_cycle)

        # Remove from pending queue and remove needs-rework label
        self.state.pending_reworks = [r for r in self.state.pending_reworks if r.pr_number != rework.pr_number]
        rework_label = self.config.get_label_needs_rework()
        try:
            subprocess.run(["gh", "pr", "edit", str(rework.pr_number), "--remove-label", rework_label],
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
