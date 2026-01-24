"""Main orchestrator - ties everything together."""

import asyncio, logging, os, signal, time
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import TYPE_CHECKING, Optional, cast

if TYPE_CHECKING:
    from ..control.planner import Plan
    from ..control.session_manager import SessionRef, SessionType
    from ..ports.session_runner import DiscoveredSession

from ..events import EventName, EventContext, EventHub
from ..control.orchestrator_support import (
    OrchestratorSupport,
    run_planning_cycle as _run_planning_cycle_impl,
    run_tick as _run_tick_impl,
    pause_issue_for_reconciliation,
    check_health as _check_health,
    init_orchestrator_components,
    handle_signal as _handle_signal,
)
from ..control.github_workflow import GitHubWorkflow, launch_issue_by_number as _gw_launch_issue_by_number, get_issue_machine as _gw_get_issue_machine
from ..control.worktree_manager import get_worktree_path, get_session_name, extract_issue_branches

logger = logging.getLogger(__name__)


from .config import Config
from ..ports.issue import Issue
from ..domain.models import Session, SessionStatus, OrchestratorState, PendingReview, PendingRework, PendingTriageReview, AgentConfig, ORCHESTRATOR_PR_MARKER
from ..observation.observer import SessionObserver
from ..control.scheduler import Scheduler
from ..control.dependency_evaluator import DependencyEvaluator
from ..domain.state_machines.issue_machine import IssueStateMachine
from ..domain.state_machines.session_machine import SessionStateMachine
from ..domain.state_machines.review_machine import ReviewStateMachine
from ..control.session_launcher import (
    SessionLauncher,
    handle_session_completion as _handle_session_completion,
    process_active_sessions as _process_active_sessions,
    observe_active_sessions as _observe_active_sessions,
    orchestrator_launch_review_session as _launch_review_session,
    orchestrator_launch_rework_session as _launch_rework_session,
    launch_triage_session as _launch_triage_session,
    session_launcher_callback as _session_launcher_callback,
    restore_running_sessions as _restore_running_sessions,
    parse_session_ref as _parse_session_ref,
    create_session as _create_session,
    session_exists as _session_exists,
    kill_session as _kill_session,
    orchestrator_launch_session as _launch_session,
    get_session_machine as _sl_get_session_machine,
)
from ..control.publish_executor import create_publish_job
from ..control.cleanup_manager import CleanupManager
from ..control.completion_handler import (
    CompletionHandler,
    launch_review_by_number as _ch_launch_review_by_number,
    launch_rework_by_number as _ch_launch_rework_by_number,
    launch_triage_by_number as _ch_launch_triage_by_number,
    get_review_machine as _ch_get_review_machine,
)
from ..control.startup_manager import StartupManager
from ..ports import TraceEvent, RepositoryHost, SessionRunner
from ..control.health_gate import HealthDecision
from ..control.orchestrator_deps import OrchestratorDeps
from .e2e_runner import maybe_trigger_e2e, get_e2e_runner_manager


@dataclass
class Orchestrator:
    """Main orchestrator - mediates gather → plan → apply cycle.

    All dependencies are injected via OrchestratorDeps - no Optional fields, no Null defaults.
    Bootstrap is the single source of truth for choosing implementations.
    """

    config: Config
    deps: OrchestratorDeps  # All dependencies bundled - no nulls, no optionals
    state: OrchestratorState = field(default_factory=OrchestratorState)
    scheduler: Scheduler = field(init=False)
    observer: SessionObserver = field(init=False)
    _shutdown_requested: bool = field(default=False, init=False)
    _refresh_requested: bool = field(default=False, init=False)
    _inflight_stable_ids: dict[str, float] = field(default_factory=dict, init=False)  # stable_id -> expires_at (monotonic)
    _INFLIGHT_TTL_SECONDS: float = field(default=90.0, init=False, repr=False)
    _last_issue_fetch: float = field(default=0.0, init=False)
    _last_ui_update: float = field(default=0.0, init=False)
    _loop_iteration: int = field(default=0, init=False)
    _ui_update_interval: int = field(default=30, init=False)
    _event_context: EventContext = field(default_factory=EventContext, init=False)
    _loop_error_count: int = field(default=0, init=False)
    _loop_error_limit: int = field(default=3, init=False)
    _last_tick_time: float = field(default=0.0, init=False)

    def __post_init__(self):
        # All validation is done by OrchestratorDeps being a frozen dataclass with no Optional fields.
        # If deps is constructed, all dependencies are present.
        dep_eval = DependencyEvaluator(
            self.deps.repository_host,
            self.deps.events,
            foundation_milestone=self.config.foundation_milestone,
        )
        init_orchestrator_components(self, dep_eval)

    @property
    def event_hub(self) -> EventHub:
        return self.deps.event_hub

    @property
    def repository_host(self) -> RepositoryHost:
        """Access the repository host (GitHub adapter) for issue/PR operations."""
        return self.deps.repository_host

    @property
    def session_runner(self) -> SessionRunner:
        """Access the session runner for terminal operations."""
        return self.deps.runner

    @property
    def shutdown_requested(self) -> bool:
        """Check if shutdown has been requested."""
        return self._shutdown_requested

    @shutdown_requested.setter
    def shutdown_requested(self, value: bool) -> None:
        """Set shutdown requested flag (use request_shutdown() for proper event emission)."""
        self._shutdown_requested = value

    @property
    def event_context(self) -> EventContext:
        """Access the event context for tick and event metadata."""
        return self._event_context

    @property
    def last_tick_time(self) -> float:
        """Get the timestamp of the last tick."""
        return self._last_tick_time

    def kill_session(self, name: str) -> None:
        """Kill a session by terminal ID (public wrapper)."""
        self._kill_session(name)

    @cached_property
    def _cleanup_manager(self) -> CleanupManager:
        return CleanupManager(
            self.config, self.deps.repository_host, self.deps.worktree_manager,
            lambda name: _kill_session(name, self.deps.session_manager, self.deps.events),
            lambda name: _session_exists(name, self.deps.session_manager, self.deps.events),
            lambda issue_number, agent_config: get_worktree_path(self.config, issue_number, agent_config),
            lambda number, session_type="issue": get_session_name(number, session_type),
        )

    @cached_property
    def _completion_handler(self) -> CompletionHandler:
        return CompletionHandler(
            self.config, self.deps.events, self.deps.repository_host,
            lambda issue: self.deps.state_machine_manager.issue_machines.get(issue.number),
            lambda s: self.deps.state_machine_manager.session_machines.get(s),
            lambda n: self.deps.state_machine_manager.review_machines.get(n),
            self.deps.session_output,
        )

    @cached_property
    def _session_launcher(self) -> SessionLauncher:
        return SessionLauncher(
            self.config, self.deps.events, self.deps.repository_host, self.deps.action_applier, self.deps.session_manager,
            self.deps.worktree_manager, self.deps.working_copy, self.deps.command_runner, self.deps.session_output,
            self.deps.manifest_downloader,
            lambda name: _session_exists(name, self.deps.session_manager, self.deps.events),
            self._create_session, self._get_issue_machine, self._get_session_machine,
            self._get_review_machine, self._refresh_issue, getattr(self.scheduler, "dependency_evaluator", None),
            claim_manager=self.deps.claim_manager,
        )

    @cached_property
    def _plan_applier(self) -> OrchestratorSupport:
        return OrchestratorSupport(
            config=self.config, events=self.deps.events, repository_host=self.deps.repository_host,
            state=self.state, event_context=self._event_context, session_manager=self.deps.session_manager,
            action_applier=self.deps.action_applier, fact_gatherer=self.deps.fact_gatherer,
            planner=self.deps.planner, worktree_manager=self.deps.worktree_manager,
            state_machine_manager=self.deps.state_machine_manager, cleanup_manager=self._cleanup_manager,
            get_review_machine=self._get_review_machine,
            kill_session=lambda name: _kill_session(name, self.deps.session_manager, self.deps.events),
        )

    def _get_session_name(self, number: int, session_type: str = "issue") -> str: return get_session_name(number, session_type)
    def _get_worktree_path(self, issue_number: int, agent_config: AgentConfig) -> Path: return get_worktree_path(self.config, issue_number, agent_config)
    def session_launcher_callback(self, session_type: "SessionType", number: int) -> Optional[Session]: return _session_launcher_callback(session_type, number, self._launch_issue_by_number, self._launch_review_by_number, self._launch_rework_by_number, self._launch_triage_by_number)
    def _launch_issue_by_number(self, n: int) -> Optional[Session]: return _gw_launch_issue_by_number(n, self.state.cached_queue_issues, self.launch_session, lambda: setattr(self.state, 'issues_started_count', self.state.issues_started_count + 1))
    def _launch_review_by_number(self, n: int) -> Optional[Session]: return _ch_launch_review_by_number(n, self.state.pending_reviews, self.launch_review_session)
    def _launch_rework_by_number(self, n: int) -> Optional[Session]: return _ch_launch_rework_by_number(n, self.state.pending_reworks, self.launch_rework_session)
    def _launch_triage_by_number(self, n: int) -> Optional[Session]: return _ch_launch_triage_by_number(n, self.state.pending_triage_reviews, self.state.active_sessions, self._launch_triage_session)

    def _get_issue_machine(self, issue: Issue) -> Optional[IssueStateMachine]: return _gw_get_issue_machine(issue, self.deps.state_machine_manager)
    def _get_session_machine(self, name: str, n: int, timeout: int) -> Optional[SessionStateMachine]: return _sl_get_session_machine(name, n, timeout, self.deps.state_machine_manager)
    def _get_review_machine(self, pr: int, issue: int) -> Optional[ReviewStateMachine]: return _ch_get_review_machine(pr, issue, self.deps.state_machine_manager)

    def _restore_running_sessions(self, running: list["DiscoveredSession"]) -> None: _restore_running_sessions(running, self.state.active_sessions, self.deps.session_restorer)
    def _parse_session_ref(self, session_name: str, operation: str) -> "SessionRef": return _parse_session_ref(session_name, operation, self.deps.events)
    def _create_session(self, name: str, cmd: str, wd: Path, title: str | None = None) -> bool: return _create_session(name, cmd, wd, title, self.deps.session_manager, self.deps.events)
    def _session_exists(self, name: str) -> bool: return _session_exists(name, self.deps.session_manager, self.deps.events)
    def _kill_session(self, name: str) -> None: _kill_session(name, self.deps.session_manager, self.deps.events)
    def _refresh_issue(self, n: int) -> Optional[Issue]: return self._github_workflow.refresh_issue(n)
    def _build_labels(self, *labels: str) -> list[str]: return self._github_workflow.build_labels(*labels)

    def _get_milestone_filter(self) -> str | None: return self.config.filtering.milestone

    @cached_property
    def _startup_manager(self) -> StartupManager:
        return StartupManager(
            self.config, self.deps.events, self.deps.runner, self.deps.repository_host,
            self.deps.action_applier,
            self.deps.hook_verifier,
            lambda: extract_issue_branches(self.deps.working_copy, self.config.repo_root),
            lambda name: self._session_exists(name),
            lambda r: self._restore_running_sessions(r),
            self.launch_session, self.update_queue_cache,
        )

    async def startup(self) -> None: await self._startup_manager.run_startup(self.state)

    def launch_session(self, issue: Issue) -> Optional[Session]: return _launch_session(issue, self.state, self._session_launcher)
    def handle_session_completion(self, session: Session, status: SessionStatus) -> None: _handle_session_completion(session, status, self.state, self._completion_handler, self.deps.action_applier, self.observer, self.deps.worktree_manager, self._kill_session, self.config, self.deps.session_output)

    def tick(self) -> bool:
        self._last_tick_time = time.time()
        self._loop_iteration, cont = _run_tick_impl(self._loop_iteration, self._event_context, self._inflight_stable_ids, self.state, self.deps.events, self._shutdown_requested, self._process_active_sessions, self._check_health, self._run_planning_cycle, self._emit_heartbeat_if_needed)
        # Check if we should auto-trigger E2E tests
        self._maybe_trigger_e2e()
        return cont

    def _maybe_trigger_e2e(self) -> None:
        """Check and trigger E2E tests if conditions are met.

        Triggers when:
        1. E2E is enabled with auto_run_interval_minutes > 0
        2. This instance has executor role (not reader/disabled)
        3. Interval has passed since last run
        4. Main branch HEAD has changed since last tested commit
        """
        # Get instance_id from environment (set by CC for multi-instance)
        instance_id = os.environ.get("INSTANCE_ID")

        triggered = maybe_trigger_e2e(
            config=self.config,
            repo_root=self.config.repo_root,
            orchestrator_id=self.config.orchestrator_id,
            instance_id=instance_id,
        )
        if triggered:
            self.deps.events.publish(TraceEvent(
                EventName.E2E_AUTO_TRIGGERED,
                self._event_context.enrich({}),
            ))

    def _cleanup_e2e_runner(self) -> None:
        """Clean up E2E runner on orchestrator shutdown.

        Behavior depends on survive_restart config:
        - True (default): Let worker continue, mark run as 'interrupted' (resumable)
        - False: Stop worker and mark run as canceled
        """
        if not self.config.e2e.enabled:
            return

        orchestrator_id = self.config.orchestrator_id

        manager = get_e2e_runner_manager()
        status = manager.status(orchestrator_id)

        if not status["running"]:
            return

        if self.config.e2e.survive_restart:
            # Let worker continue - on next startup, orchestrator will detect
            # the running worker OR (if worker dies) mark as interrupted and resume
            logger.info(
                "E2E worker pid=%s continuing (survive_restart=True)",
                status["pid"],
            )
        else:
            # Stop the worker
            logger.info("Stopping E2E worker pid=%s on shutdown", status["pid"])
            manager.stop(orchestrator_id, self.config.repo_root)

    def _check_health(self) -> HealthDecision:
        return cast(HealthDecision, _check_health(self.deps.health_gate, len(self.state.active_sessions), self.state.paused))

    def _process_active_sessions(self) -> None:
        _process_active_sessions(
            self.state, self.observer, self.deps.session_controller, self._completion_handler,
            self.deps.action_applier, self.deps.worktree_manager, self._kill_session, self.config
        )
        # Check lease renewals for active sessions
        self._check_lease_renewals()

    def _observe_active_sessions_async(self) -> None:
        """Observe active sessions and collect completion facts (async flow).

        This is the new async-aware version that:
        1. Observes sessions using CompletionObserver (fast, no I/O)
        2. Collects ObservedCompletion facts for the planner
        3. The planner will plan label updates and create publish jobs
        4. Jobs are submitted to PublishJobExecutor for background execution
        """
        _observe_active_sessions(
            self.state,
            self.observer,
            self.deps.completion_observer,
            self._kill_session,
            claim_manager=self.deps.claim_manager,
            events=self.deps.events,
        )
        # Check lease renewals for active sessions
        self._check_lease_renewals()

    def _submit_publish_jobs(self) -> None:
        """Submit publish jobs for observed completions.

        Called after planning to submit jobs to the background executor.
        """
        # Process observed completions and create jobs
        for observed in list(self.state.observed_completions):
            if observed.needs_publish:
                job = create_publish_job(observed, run_validation=False)
                submitted = self.deps.publish_executor.submit(job)
                if submitted:
                    self.state.pending_publish_jobs[job.job_id] = job
                    logger.info(
                        "[ASYNC] Submitted publish job: job_id=%s issue=%d",
                        job.job_id,
                        observed.issue_number,
                    )

        # Clear observed completions after processing
        self.state.observed_completions = []

    def _poll_job_results(self) -> None:
        """Poll for completed publish jobs and handle results.

        Called at the start of each tick to check for background job completion.
        """
        results = self.deps.publish_executor.poll_results()

        for result in results:
            logger.info(
                "[ASYNC] Job completed: job_id=%s issue=%d success=%s pr_url=%s",
                result.job_id,
                result.issue_number,
                result.success,
                result.pr_url,
            )

            # Remove from pending
            self.state.pending_publish_jobs.pop(result.job_id, None)

            # Handle job result - queue review if successful
            if result.success and result.pr_url and result.pr_number:
                from ..domain.models import DiscoveredReview
                # Queue for code review
                # We need to look up the branch_name from the job or session
                # For now, we'll construct it from the issue number
                branch_name = f"issue-{result.issue_number}"  # Default pattern
                self.state.discovered_reviews.append(DiscoveredReview(
                    result.issue_number,
                    result.pr_number,
                    result.pr_url,
                    branch_name,
                    agent_label=None,  # TODO: track agent label in job
                ))
                self.state.completed_today.append(result.issue_number)
            elif not result.success:
                # Track failure
                from ..domain.models import DiscoveredFailure
                self.state.discovered_failures.append(DiscoveredFailure(
                    result.issue_number,
                    f"Issue #{result.issue_number}",
                    "publish_failed",
                ))
                self.state.failed_this_cycle.add(result.issue_number)

    def start_publish_executor(self) -> None:
        """Start the background publish executor. Call during orchestrator startup."""
        self.deps.publish_executor.start()
        logger.info("[ASYNC] Publish executor started")

    def shutdown_publish_executor(self, wait: bool = True, timeout: float | None = None) -> None:
        """Shutdown the background publish executor. Call during orchestrator shutdown."""
        self.deps.publish_executor.shutdown(wait=wait, timeout=timeout)
        logger.info("[ASYNC] Publish executor shutdown")

    def _check_lease_renewals(self) -> None:
        """Check and renew leases for active sessions.

        Handles sessions that have lost their claims by terminating them
        and adding the appropriate blocked label.
        """
        from ..infra import labels

        # Check renewals - returns sessions that lost their claim
        lost_sessions = self.deps.lease_renewer.check_renewals(
            list(self.state.active_sessions)
        )

        # Handle claim losses
        for session in lost_sessions:
            logger.warning(
                "[CLAIM] Session for issue #%d lost claim - terminating",
                session.issue.number,
            )

            # Kill the terminal session
            self._kill_session(session.terminal_id)

            # Remove from active sessions
            self.state.active_sessions = [
                s for s in self.state.active_sessions
                if s.terminal_id != session.terminal_id
            ]

            # Add blocked label (best effort - session lost claim so this may also fail)
            try:
                self.deps.action_applier.labels.add_label(
                    session.issue.number,
                    labels.BLOCKED_CLAIM_LOST,
                )
            except Exception as e:
                logger.warning(
                    "[CLAIM] Failed to add blocked label to issue #%d: %s",
                    session.issue.number,
                    e,
                )

            # Post comment explaining what happened (best effort)
            try:
                comment = (
                    "## Work Cancelled\n\n"
                    "Another orchestrator claimed this issue while work was in progress. "
                    "The session has been terminated to avoid conflicts.\n\n"
                    f"- **Worktree preserved**: `{session.worktree_path}`\n"
                    f"- **Branch**: `{session.branch_name}`\n\n"
                    "To resume work, remove the `blocked:claim-lost` label and re-assign an agent label."
                )
                self.deps.repository_host.add_comment(session.issue.number, comment)
            except Exception as e:
                logger.warning(
                    "[CLAIM] Failed to post claim loss comment to issue #%d: %s",
                    session.issue.number,
                    e,
                )

            # Note: We preserve the worktree so work isn't lost

    def _run_planning_cycle(self) -> None:
        # Capture and clear the refresh flag before the cycle.
        # If request_refresh() is called during the cycle, it will set
        # _refresh_requested = True again. We must NOT overwrite that
        # new value when the cycle returns.
        refresh_to_process = self._refresh_requested
        self._refresh_requested = False
        self._last_issue_fetch, _ = _run_planning_cycle_impl(self.config, self.deps.events, self._event_context, self.state, self.deps.fact_gatherer, self.deps.planner, self.deps.repository_host, self.scheduler, self._github_workflow, self._apply_plan, self._clear_discovered_facts, self._last_issue_fetch, refresh_to_process, self._inflight_stable_ids, self.observer, self.deps.claim_manager)

    def _clear_discovered_facts(self) -> None: self._plan_applier.clear_discovered_facts()
    def _emit_heartbeat_if_needed(self) -> None: self._plan_applier.emit_heartbeat_if_needed()

    async def run_loop(self) -> None:
        logger.info("Starting orchestration loop")

        # Initialize terminal backend (creates tmux session for this orchestrator)
        self.deps.runner.on_orchestrator_startup()

        # Emit orchestrator.started
        self.deps.events.publish(TraceEvent(
            EventName.ORCHESTRATOR_STARTED,
            self._event_context.enrich({
                "mode": "web" if hasattr(self, "_web_mode") else "headless",
            }),
        ))

        self.reconcile_orphaned_pr_labels()
        self._last_issue_fetch, self._last_ui_update, self._loop_iteration = 0.0, time.time(), 0

        while not self._shutdown_requested:
            try:
                # Run tick in thread pool to avoid blocking the event loop
                # during long-running operations like git push with hooks
                should_continue = await asyncio.to_thread(self.tick)
                self._loop_error_count = 0
                if not should_continue:
                    break
            except Exception as e:
                self._loop_error_count += 1
                logger.exception("[LOOP] Error in iteration %d: %s", self._loop_iteration, e)
                self.deps.events.publish(TraceEvent(
                    EventName.APPLY_FAILED,
                    self._event_context.enrich({
                        "step_type": "tick",
                        "iteration": self._loop_iteration,
                        "error": str(e),
                        "error_count": self._loop_error_count,
                    }),
                ))
                if self._loop_error_count >= self._loop_error_limit and not self.state.paused:
                    self.state.paused = True
                    logger.warning(
                        "[LOOP] Pausing orchestrator after %d consecutive errors",
                        self._loop_error_count,
                    )
                    self.deps.events.publish(TraceEvent(
                        EventName.ORCHESTRATOR_PAUSED,
                        self._event_context.enrich({
                            "reason": "loop_error_threshold",
                            "error_count": self._loop_error_count,
                        }),
                    ))
            await asyncio.sleep(10)

        # Shutdown sequence
        active = self.state.active_sessions
        self.deps.events.publish(TraceEvent(
            EventName.ORCHESTRATOR_SHUTDOWN_STARTED,
            self._event_context.enrich({
                "force": False,
                "active_sessions": len(active),
                "sessions": [s.issue.number for s in active],
            }),
        ))
        self.deps.events.publish(TraceEvent(
            EventName.ORCHESTRATOR_SHUTDOWN_COMPLETED,
            self._event_context.enrich({
                "force": False,
                "active_sessions_final": len(self.state.active_sessions),
                "iterations": self._loop_iteration,
            }),
        ))

        # Clean up E2E runner if active
        self._cleanup_e2e_runner()

        # Shutdown publish executor gracefully - wait for running jobs to complete
        # In-flight jobs will be saved to SQLite for recovery on next startup
        logger.info("[SHUTDOWN] Waiting for background publish jobs to complete...")
        self.shutdown_publish_executor(wait=True, timeout=60.0)

        # Clean up terminal backend (kills tmux session - atomic cleanup of all windows)
        self.deps.runner.on_orchestrator_shutdown()

    def request_shutdown(self, force: bool = False) -> None:
        """Request graceful or forced shutdown."""
        self._shutdown_requested = True
        active = self.state.active_sessions
        self.deps.events.publish(TraceEvent(
            EventName.ORCHESTRATOR_SHUTDOWN_REQUESTED,
            self._event_context.enrich({
                "force": force,
                "active_session_count": len(active),
                "sessions": [s.issue.number for s in active],
            }),
        ))
        if not active:
            logger.info("Shutdown requested - no active sessions, exiting")
            return
        if force:
            logger.info("Force shutdown - killing %d session(s)", len(active))
            for s in active:
                try: self._kill_session(s.terminal_id)
                except Exception as e: logger.warning("Failed to kill session %s: %s", s.terminal_id, e)
            self.state.active_sessions = []
        else:
            logger.info("Shutdown requested - waiting for %d session(s)", len(active))

    def request_refresh(self, inflight_stable_ids: set[str] | None = None) -> None:
        self._refresh_requested = True
        self._plan_applier.request_refresh(inflight_stable_ids, self._inflight_stable_ids, self._INFLIGHT_TTL_SECONDS)

    def pause(self) -> None:
        self.state.paused = True
        logger.info("Orchestrator paused")
        self.deps.events.publish(TraceEvent(
            EventName.ORCHESTRATOR_PAUSED,
            self._event_context.enrich({}),
        ))

    def resume(self) -> None:
        self.state.paused = False
        logger.info("Orchestrator resumed")
        self.deps.events.publish(TraceEvent(
            EventName.ORCHESTRATOR_RESUMED,
            self._event_context.enrich({}),
        ))

    def get_failure_diagnosis(self, issue_number: int) -> dict:
        """Get failure diagnosis for a session.

        This provides diagnostic info for debugging failed sessions,
        used by the web UI's failure diagnosis endpoint.

        Returns a dict ready for JSON serialization.
        """
        from .session_failure_diagnosis import create_session_failure_diagnosis
        diagnosis = create_session_failure_diagnosis(
            issue_number=issue_number,
            session_history=self.state.session_history,
            active_sessions=self.state.active_sessions,
            config=self.config,
            agents=self.config.agents,
        )
        return diagnosis.to_dict()

    def _pause_issue_for_reconciliation(self, issue_number: int, reason: str) -> None: pause_issue_for_reconciliation(self.deps.events, self.deps.action_applier, self._event_context, issue_number, reason)
    def _apply_plan(self, plan: "Plan") -> None: self._plan_applier.apply_plan(plan, self._pause_issue_for_reconciliation)
    def _fetch_all_issues(self, required_stable_ids: set[str] | None = None) -> list[Issue]: return self._github_workflow.fetch_all_issues(self._get_milestone_filter(), required_stable_ids)
    def update_queue_cache(self) -> None: self._plan_applier.update_queue_cache()
    def _update_dependency_problems(self, dep_blocked: list[tuple["Issue", str]]) -> None: self._github_workflow.update_dependency_problems(self.state, dep_blocked)
    @property
    def _github_workflow(self) -> GitHubWorkflow: return GitHubWorkflow(self.config, self.deps.events, self.deps.repository_host, self.deps.fact_gatherer, self.deps.pr_scanner, self.deps.label_sync, self._event_context)
    def launch_review_session(self, review: PendingReview) -> Optional[Session]: return _launch_review_session(review, self.state, self._session_launcher, self.deps.session_restorer)
    def _launch_triage_session(self, triage: PendingTriageReview) -> None: _launch_triage_session(triage, self.config, self.launch_session)
    def process_deferred_cleanups(self) -> None: self.state.pending_cleanups = self._github_workflow.process_deferred_cleanups(self.state.pending_cleanups, self._cleanup_manager)
    def _recover_orphaned_cleanups(self) -> None: self._plan_applier.recover_orphaned_cleanups()
    def scan_needs_code_review_prs(self) -> None: self._github_workflow.scan_needs_code_review_prs(self.state)
    def scan_needs_rework_prs(self) -> None: self._github_workflow.scan_needs_rework_prs(self.state)
    def reconcile_orphaned_pr_labels(self) -> int: return self._github_workflow.reconcile_orphaned_pr_labels(ORCHESTRATOR_PR_MARKER)
    def launch_rework_session(self, rework: PendingRework) -> Optional[Session]: return _launch_rework_session(rework, self.state, self._session_launcher, self.deps.session_restorer)

async def run_orchestrator(config_path: Optional[Path] = None) -> None:
    from ..entrypoints.bootstrap import build_orchestrator
    config = Config.load(config_path) if config_path else Config.find_and_load()
    orchestrator = build_orchestrator(config)
    signal.signal(signal.SIGINT, lambda s, f: _handle_signal(orchestrator, s, f))
    signal.signal(signal.SIGTERM, lambda s, f: _handle_signal(orchestrator, s, f))
    await orchestrator.startup()
    await orchestrator.run_loop()
