"""Main orchestrator - ties everything together."""

import asyncio, logging, signal, time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional, cast

if TYPE_CHECKING:
    from .control.planner import Planner, Plan, OrchestratorSnapshot
    from .control.session_manager import SessionManager, SessionRef
    from .control.label_sync import LabelSync
    from .control.action_applier import ActionApplier, ActionResult
    from .control.fact_gatherer import FactGatherer
    from .control.actions import Action, LaunchSessionAction, EscalateToHumanAction
    from .models import TriageFacts
    from .ports.session_runner import DiscoveredSession

from .events import EventName, EventContext, EventHub
from .control.orchestrator_support import (
    log_transition,
    OrchestratorSupport,
    PlanApplier,
    pause_issue_for_reconciliation,
    clear_discovered_facts as _clear_discovered_facts,
    emit_heartbeat_if_needed as _emit_heartbeat_if_needed,
    check_health as _check_health,
    init_orchestrator_components,
    handle_signal as _handle_signal,
)
from .control.github_workflow import GitHubWorkflow, launch_issue_by_number as _gw_launch_issue_by_number, get_issue_machine as _gw_get_issue_machine
from .control.worktree_manager import get_worktree_path, get_session_name, extract_issue_branches

logger = logging.getLogger(__name__)


from .config import Config
from .ports.issue import Issue
from .models import Issue as ConcreteIssue  # For instantiation
from .models import Session, SessionStatus, OrchestratorState, PendingReview, PendingRework, PendingTriageReview, PendingCleanup, AgentConfig, ORCHESTRATOR_PR_MARKER
from .observation.observer import SessionObserver
from .control.scheduler import Scheduler
from .control.dependency_evaluator import DependencyEvaluator
from .domain.state_machines.issue_machine import IssueStateMachine, IssueState
from .domain.state_machines.session_machine import SessionStateMachine, SessionState
from .domain.state_machines.review_machine import ReviewStateMachine, ReviewState
from .control.completion_processor import CompletionProcessor
from .control.session_controller import SessionController
from .control.pr_scanner import PRScanner
from .control.session_launcher import (
    SessionLauncher,
    handle_session_completion as _handle_session_completion,
    process_active_sessions as _process_active_sessions,
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
from .control.cleanup_manager import CleanupManager
from .control.completion_handler import (
    CompletionHandler,
    launch_review_by_number as _ch_launch_review_by_number,
    launch_rework_by_number as _ch_launch_rework_by_number,
    launch_triage_by_number as _ch_launch_triage_by_number,
    get_review_machine as _ch_get_review_machine,
)
from .control.session_restorer import SessionRestorer
from .control.startup_manager import StartupManager
from .control.state_machine_manager import StateMachineManager
from .control.reconciliation import ReconciliationRequired, get_pause_label
from .observation.observation import SessionObservation
from .ports import (
    EventSink,
    SessionRunner,
    TraceEvent,
    NullEventSink,
    NullSessionRunner,
    RepositoryHost,
    CommandRunner,
    NullCommandRunner,
    HookVerifier,
)
from .ports.worktree_manager import WorktreeManager
from .ports.working_copy import WorkingCopy
from .control.health_gate import HealthGate, HealthDecision


@dataclass
class Orchestrator:
    """Main orchestrator - mediates gather → plan → apply cycle. Dependencies injected via bootstrap."""
    config: Config
    events: EventSink = field(default_factory=NullEventSink)
    runner: SessionRunner = field(default_factory=NullSessionRunner)
    _repository_host: Optional[RepositoryHost] = field(default=None, repr=False)
    event_hub: Optional[EventHub] = field(default=None, repr=False)
    planner: Optional["Planner"] = field(default=None, repr=False)
    session_manager: Optional["SessionManager"] = field(default=None, repr=False)
    label_sync: Optional["LabelSync"] = field(default=None, repr=False)
    action_applier: Optional["ActionApplier"] = field(default=None, repr=False)
    fact_gatherer: Optional["FactGatherer"] = field(default=None, repr=False)
    pr_scanner: Optional["PRScanner"] = field(default=None, repr=False)
    session_restorer: Optional["SessionRestorer"] = field(default=None, repr=False)
    worktree_manager: Optional[WorktreeManager] = field(default=None, repr=False)
    working_copy: Optional[WorkingCopy] = field(default=None, repr=False)
    hook_verifier: Optional[HookVerifier] = field(default=None, repr=False)
    command_runner: CommandRunner = field(default_factory=NullCommandRunner, repr=False)
    state_machine_manager: Optional[StateMachineManager] = field(default=None, repr=False)
    completion_processor: Optional["CompletionProcessor"] = field(default=None, repr=False)
    session_controller: Optional["SessionController"] = field(default=None, repr=False)
    health_gate: Optional[HealthGate] = field(default=None, repr=False)
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

    def __post_init__(self):
        if self._repository_host is None:
            raise ValueError("RepositoryHost must be injected via bootstrap")
        if self.action_applier is None and self.worktree_manager is None:
            raise ValueError("Either action_applier or worktree_manager must be injected")
        if self.hook_verifier is None:
            raise ValueError("HookVerifier must be injected via bootstrap")
        if self.working_copy is None:
            raise ValueError("WorkingCopy must be injected via bootstrap")
        if self.health_gate is None:
            raise ValueError("HealthGate must be injected via bootstrap")

        dep_eval = DependencyEvaluator(self._repository_host, self.events)
        init_orchestrator_components(self, dep_eval)

    @property
    def repository_host(self) -> RepositoryHost:
        assert self._repository_host is not None; return self._repository_host
    @property
    def _completion_processor(self) -> CompletionProcessor:
        assert self.completion_processor is not None; return self.completion_processor
    @property
    def _session_controller(self) -> SessionController:
        assert self.session_controller is not None; return self.session_controller
    @property
    def _pr_scanner(self) -> PRScanner:
        assert self.pr_scanner is not None; return self.pr_scanner

    @property
    def _session_launcher(self) -> SessionLauncher:
        if self._session_launcher_instance is None:
            if self.working_copy is None:
                raise ValueError("WorkingCopy must be injected via bootstrap")
            self._session_launcher_instance = SessionLauncher(
                self.config,
                self.events,
                self.repository_host,
                self._sm,
                self._wm,
                self.working_copy,
                self.command_runner,
                self._session_exists,
                self._create_session,
                self._get_issue_machine,
                self._get_session_machine,
                self._get_review_machine,
                self._refresh_issue,
                getattr(self.scheduler, "dependency_evaluator", None),
            )
        return self._session_launcher_instance

    @property
    def _cleanup_manager(self) -> CleanupManager:
        if self._cleanup_manager_instance is None:
            self._cleanup_manager_instance = CleanupManager(self.config, self.repository_host, self._wm, self._kill_session,
                self._session_exists, self._get_worktree_path, self._get_session_name)
        return self._cleanup_manager_instance

    @property
    def _completion_handler(self) -> CompletionHandler:
        if self._completion_handler_instance is None:
            self._completion_handler_instance = CompletionHandler(self.config, self.events, self.repository_host,
                lambda issue: self._state_machines.issue_machines.get(issue.number), lambda s: self._state_machines.session_machines.get(s), lambda n: self._state_machines.review_machines.get(n))
        return self._completion_handler_instance

    @property
    def _plan_applier(self) -> OrchestratorSupport:
        if self._plan_applier_instance is None:
            self._plan_applier_instance = OrchestratorSupport(
                config=self.config,
                events=self.events,
                repository_host=self.repository_host,
                state=self.state,
                event_context=self._event_context,
                session_manager=self._sm,
                action_applier=self._aa,
                fact_gatherer=self._fg,
                planner=self._planner,
                worktree_manager=self._wm,
                state_machine_manager=self._state_machines,
                cleanup_manager=self._cleanup_manager,
                get_review_machine=self._get_review_machine,
                kill_session=self._kill_session,
            )
        return self._plan_applier_instance

    @property
    def _session_restorer(self) -> SessionRestorer:
        if self.session_restorer:
            return self.session_restorer
        if self.working_copy is None:
            raise ValueError("WorkingCopy must be injected via bootstrap")
        return SessionRestorer(self.config, self.repository_host, self.working_copy)

    @property
    def _state_machines(self) -> StateMachineManager:
        assert self.state_machine_manager is not None; return self.state_machine_manager

    @property
    def _sm(self) -> "SessionManager":
        """Session manager accessor (always initialized after __post_init__)."""
        assert self.session_manager is not None; return self.session_manager

    @property
    def _aa(self) -> "ActionApplier":
        """Action applier accessor (always initialized after __post_init__)."""
        assert self.action_applier is not None; return self.action_applier

    @property
    def _fg(self) -> "FactGatherer":
        """Fact gatherer accessor (always initialized after __post_init__)."""
        assert self.fact_gatherer is not None; return self.fact_gatherer

    @property
    def _planner(self) -> "Planner":
        """Planner accessor (always initialized after __post_init__)."""
        assert self.planner is not None; return self.planner

    @property
    def _wm(self) -> WorktreeManager:
        """Worktree manager accessor (must be injected)."""
        assert self.worktree_manager is not None, "WorktreeManager must be injected"; return self.worktree_manager

    def _get_session_name(self, number: int, session_type: str = "issue") -> str: return get_session_name(number, session_type)
    def _get_worktree_path(self, issue_number: int, agent_config: AgentConfig) -> Path: return get_worktree_path(self.config, issue_number, agent_config)
    def _session_launcher_callback(self, session_type: str, number: int) -> Optional[Session]: return _session_launcher_callback(session_type, number, self._launch_issue_by_number, self._launch_review_by_number, self._launch_rework_by_number, self._launch_triage_by_number)
    def _launch_issue_by_number(self, n: int) -> Optional[Session]: return _gw_launch_issue_by_number(n, self.state.cached_queue_issues, self.launch_session, lambda: setattr(self.state, 'issues_started_count', self.state.issues_started_count + 1))
    def _launch_review_by_number(self, n: int) -> Optional[Session]: return _ch_launch_review_by_number(n, self.state.pending_reviews, self.launch_review_session)
    def _launch_rework_by_number(self, n: int) -> Optional[Session]: return _ch_launch_rework_by_number(n, self.state.pending_reworks, self.launch_rework_session)
    def _launch_triage_by_number(self, n: int) -> Optional[Session]: return _ch_launch_triage_by_number(n, self.state.pending_triage_reviews, self.state.active_sessions, self._launch_triage_session)

    def _get_issue_machine(self, issue: Issue) -> Optional[IssueStateMachine]: return _gw_get_issue_machine(issue, self._state_machines)
    def _get_session_machine(self, name: str, n: int, timeout: int) -> Optional[SessionStateMachine]: return _sl_get_session_machine(name, n, timeout, self._state_machines)
    def _get_review_machine(self, pr: int, issue: int) -> Optional[ReviewStateMachine]: return _ch_get_review_machine(pr, issue, self._state_machines)

    def _restore_running_sessions(self, running: list["DiscoveredSession"]) -> None: _restore_running_sessions(running, self.state.active_sessions, self._session_restorer)
    def _parse_session_ref(self, session_name: str, operation: str) -> "SessionRef": return _parse_session_ref(session_name, operation, self.events)
    def _create_session(self, name: str, cmd: str, wd: Path, title: str | None = None) -> bool: return _create_session(name, cmd, wd, title, self._sm, self.events)
    def _session_exists(self, name: str) -> bool: return _session_exists(name, self._sm, self.events)
    def _kill_session(self, name: str) -> None: _kill_session(name, self._sm, self.events)
    def _refresh_issue(self, n: int) -> Optional[Issue]: return self._github_workflow.refresh_issue(n)
    def _build_labels(self, *labels: str) -> list[str]: return self._github_workflow.build_labels(*labels)

    def _get_milestone_filter(self) -> str | None: return self.config.filter_milestone

    @property
    def _startup_manager(self) -> StartupManager:
        if self._startup_manager_instance is None:
            working_copy = self.working_copy
            hook_verifier = self.hook_verifier
            if working_copy is None:
                raise ValueError("WorkingCopy must be injected via bootstrap")
            if hook_verifier is None:
                raise ValueError("HookVerifier must be injected via bootstrap")
            issue_branches_fn = lambda: extract_issue_branches(working_copy, self.config.repo_root)

            self._startup_manager_instance = StartupManager(self.config, self.events, self.runner, self.repository_host,
                hook_verifier, issue_branches_fn, self._session_exists,
                lambda r: self._restore_running_sessions(r), self.launch_session, self.update_queue_cache)
        return self._startup_manager_instance

    async def startup(self) -> None: await self._startup_manager.run_startup(self.state)

    def launch_session(self, issue: Issue) -> Optional[Session]: return _launch_session(issue, self.state, self._session_launcher)
    def handle_session_completion(self, session: Session, status: SessionStatus) -> None: _handle_session_completion(session, status, self.state, self._completion_handler, self._aa, self.observer, self.worktree_manager, self._kill_session, self.config)

    def tick(self) -> bool:
        tick_start = time.monotonic()
        self._loop_iteration += 1
        self._event_context.tick_id = self._loop_iteration

        # Prune expired inflight IDs
        now = time.monotonic()
        expired = [sid for sid, exp in self._inflight_stable_ids.items() if exp < now]
        for sid in expired:
            del self._inflight_stable_ids[sid]
        if expired:
            logger.debug("[INFLIGHT] Pruned %d expired IDs: %s", len(expired), expired)

        logger.info(
            "[LOOP] Iteration %d - active=%d paused=%s pending_reviews=%d pending_reworks=%d pending_triage=%d inflight=%d",
            self._loop_iteration,
            len(self.state.active_sessions),
            self.state.paused,
            len(self.state.pending_reviews),
            len(self.state.pending_reworks),
            len(self.state.pending_triage_reviews),
            len(self._inflight_stable_ids),
        )

        # Emit tick.started
        self.events.publish(TraceEvent(
            EventName.TICK_STARTED,
            self._event_context.enrich({}),
        ))

        if self._shutdown_requested:
            self.events.publish(TraceEvent(
                EventName.TICK_COMPLETED,
                self._event_context.enrich({"idle": True, "reason": "shutdown_requested"}),
            ))
            return False

        active_start = time.monotonic()
        self._process_active_sessions()
        active_elapsed = time.monotonic() - active_start
        if active_elapsed > 5:
            logger.warning(
                "[LOOP] Active session processing took %.1fs (active=%d)",
                active_elapsed,
                len(self.state.active_sessions),
            )

        # Use HealthGate to check if we can proceed with planning
        health_decision = self._check_health()
        if health_decision.can_proceed:
            plan_start = time.monotonic()
            self._run_planning_cycle()
            plan_elapsed = time.monotonic() - plan_start
            if plan_elapsed > 5:
                logger.warning("[LOOP] Planning cycle took %.1fs", plan_elapsed)
        else:
            # Emit plan.noop when we skip planning
            self.events.publish(TraceEvent(
                EventName.PLAN_NOOP,
                self._event_context.enrich({"reason": health_decision.reason}),
            ))

        self._emit_heartbeat_if_needed()

        # Emit tick.completed
        self.events.publish(TraceEvent(
            EventName.TICK_COMPLETED,
            self._event_context.enrich({
                "idle": len(self.state.active_sessions) == 0,
                "active_sessions": len(self.state.active_sessions),
            }),
        ))
        tick_elapsed = time.monotonic() - tick_start
        if tick_elapsed > 10:
            logger.warning("[LOOP] Tick took %.1fs", tick_elapsed)
        return True

    def _check_health(self) -> HealthDecision:
        if self.health_gate is None:
            raise ValueError("HealthGate must be injected via bootstrap")
        return cast(HealthDecision, _check_health(self.health_gate, len(self.state.active_sessions), self.state.paused))

    def _process_active_sessions(self) -> None:
        _process_active_sessions(
            self.state, self.observer, self._session_controller, self._completion_handler,
            self._aa, self.worktree_manager, self._kill_session, self.config
        )

    def _run_planning_cycle(self) -> None:
        """Fetch issues, create snapshot, plan, and apply."""
        should_fetch = (time.time() - self._last_issue_fetch >= self.config.queue_refresh_seconds) or self._refresh_requested

        if should_fetch:
            manual_refresh = self._refresh_requested
            # Collect required inflight IDs before fetch
            required_stable_ids = set(self._inflight_stable_ids.keys()) if self._inflight_stable_ids else None
            if required_stable_ids:
                logger.info("[FETCH] %s refresh with %d required inflight IDs: %s",
                           "Manual" if manual_refresh else "Scheduled",
                           len(required_stable_ids), sorted(required_stable_ids))
            else:
                logger.info("[FETCH] %s refresh", "Manual" if manual_refresh else "Scheduled")
            self._refresh_requested = False
            from . import gh_audit
            reason = gh_audit.AuditReason.QUEUE_REFRESH_MANUAL if manual_refresh else gh_audit.AuditReason.QUEUE_REFRESH_SCHEDULED
            scope = gh_audit.AuditScope.MANUAL if manual_refresh else gh_audit.AuditScope.PERIODIC
            with gh_audit.context(reason=reason, scope=scope):
                all_issues = self._fetch_all_issues(required_stable_ids=required_stable_ids)
            self._last_issue_fetch = time.time()

            # Remove discovered inflight IDs
            if required_stable_ids:
                discovered_ids = {i.key.stable_id() for i in all_issues}
                found = required_stable_ids & discovered_ids
                if found:
                    for sid in found:
                        self._inflight_stable_ids.pop(sid, None)
                    logger.info("[INFLIGHT] Discovered %d/%d required IDs: %s",
                               len(found), len(required_stable_ids), sorted(found))
                still_missing = required_stable_ids - discovered_ids
                if still_missing:
                    logger.warning("[INFLIGHT] Still missing %d required IDs after refresh: %s",
                                  len(still_missing), sorted(still_missing))
            for issue in all_issues:
                try:
                    self.repository_host.update_label_cache(issue.number, list(issue.labels))
                except Exception:
                    logger.debug("Failed to update label cache for issue #%s", issue.number, exc_info=True)
            self.scan_needs_code_review_prs()
            self.scan_needs_rework_prs()
            _, dep_blocked = self.scheduler.get_available_issues(all_issues)
            self._update_dependency_problems(dep_blocked)
            exclude = {e.issue_number for e in self.state.session_history} | {s.issue.number for s in self.state.active_sessions}
            filtered = [i for i in all_issues if i.number not in exclude]
            new_queue = [i for i in filtered if i.number == self.config.filter_issue] if self.config.filter_issue else filtered

            # Detect queue changes and emit event
            old_numbers = {i.number for i in self.state.cached_queue_issues}
            new_numbers = {i.number for i in new_queue}
            added_numbers = new_numbers - old_numbers
            removed_numbers = old_numbers - new_numbers
            if added_numbers or removed_numbers:
                added = [{"number": i.number, "title": i.title} for i in new_queue if i.number in added_numbers]
                removed = [{"number": n} for n in removed_numbers]
                self.events.publish(TraceEvent(
                    EventName.QUEUE_CHANGED,
                    {"added": added, "removed": removed, "total": len(new_queue)},
                ))
                logger.info("Queue changed: %d added, %d removed, %d total",
                           len(added), len(removed), len(new_queue))

            self.state.cached_queue_issues = new_queue

        snapshot = self._fg.create_snapshot(self.state, self.state.cached_queue_issues)

        # Emit facts.gathered
        self.events.publish(TraceEvent(
            EventName.FACTS_GATHERED,
            self._event_context.enrich({
                "issues_count": len(self.state.cached_queue_issues),
                "active_sessions": len(self.state.active_sessions),
                "pending_reviews": len(self.state.pending_reviews),
                "pending_reworks": len(self.state.pending_reworks),
            }),
        ))

        plan = self._planner.plan(snapshot)

        # Emit plan.computed
        self.events.publish(TraceEvent(
            EventName.PLAN_COMPUTED,
            self._event_context.enrich({
                "steps": plan.action_count,
                "actions": [a.action_type.value for a in plan.actions],
            }),
        ))

        if plan.action_count > 0:
            logger.info("[PLAN] %d action(s): %s", plan.action_count, ", ".join(f"{a.action_type.value}:{getattr(a, 'number', '?')}" for a in plan.actions))

        self._apply_plan(plan)
        self._clear_discovered_facts()

    def _clear_discovered_facts(self) -> None: self._plan_applier._clear_discovered_facts()
    def _emit_heartbeat_if_needed(self) -> None: self._plan_applier._emit_heartbeat_if_needed()

    async def run_loop(self) -> None:
        logger.info("Starting orchestration loop")

        # Emit orchestrator.started
        self.events.publish(TraceEvent(
            EventName.ORCHESTRATOR_STARTED,
            self._event_context.enrich({
                "mode": "web" if hasattr(self, "_web_mode") else "headless",
            }),
        ))

        self.reconcile_orphaned_pr_labels()
        self._last_issue_fetch, self._last_ui_update, self._loop_iteration = 0.0, time.time(), 0

        while not self._shutdown_requested:
            try:
                if not self.tick():
                    break
            except Exception as e:
                logger.exception("[LOOP] Error in iteration %d: %s", self._loop_iteration, e)
            await asyncio.sleep(10)

        # Shutdown sequence
        active = self.state.active_sessions
        self.events.publish(TraceEvent(
            EventName.ORCHESTRATOR_SHUTDOWN_STARTED,
            self._event_context.enrich({
                "force": False,
                "active_sessions": len(active),
                "sessions": [s.issue.number for s in active],
            }),
        ))
        self.events.publish(TraceEvent(
            EventName.ORCHESTRATOR_SHUTDOWN_COMPLETED,
            self._event_context.enrich({
                "force": False,
                "active_sessions_final": len(self.state.active_sessions),
                "iterations": self._loop_iteration,
            }),
        ))

    def request_shutdown(self, force: bool = False) -> None:
        """Request graceful or forced shutdown."""
        self._shutdown_requested = True
        active = self.state.active_sessions
        self.events.publish(TraceEvent(
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
        self.events.publish(TraceEvent(
            EventName.ORCHESTRATOR_PAUSED,
            self._event_context.enrich({}),
        ))

    def resume(self) -> None:
        self.state.paused = False
        logger.info("Orchestrator resumed")
        self.events.publish(TraceEvent(
            EventName.ORCHESTRATOR_RESUMED,
            self._event_context.enrich({}),
        ))

    def _pause_issue_for_reconciliation(self, issue_number: int, reason: str) -> None: pause_issue_for_reconciliation(self.events, self.repository_host, self._event_context, issue_number, reason)
    def _apply_plan(self, plan: "Plan") -> None: self._plan_applier._apply_plan(plan, self._pause_issue_for_reconciliation)
    def _fetch_all_issues(self, required_stable_ids: set[str] | None = None) -> list[Issue]: return self._github_workflow.fetch_all_issues(self._get_milestone_filter(), required_stable_ids)
    def update_queue_cache(self) -> None: self._plan_applier.update_queue_cache()
    def _update_dependency_problems(self, dep_blocked: list[tuple["Issue", str]]) -> None: self._github_workflow.update_dependency_problems(self.state, dep_blocked)
    @property
    def _github_workflow(self) -> GitHubWorkflow: return GitHubWorkflow(self.config, self.events, self.repository_host, self._fg, self._pr_scanner, self.label_sync, self._event_context)
    def launch_review_session(self, review: PendingReview) -> Optional[Session]: return _launch_review_session(review, self.state, self._session_launcher, self._session_restorer)
    def _launch_triage_session(self, triage: PendingTriageReview) -> None: _launch_triage_session(triage, self.config, self.launch_session)
    def process_deferred_cleanups(self) -> None: self.state.pending_cleanups = self._github_workflow.process_deferred_cleanups(self.state.pending_cleanups, self._cleanup_manager)
    def _recover_orphaned_cleanups(self) -> None: self._plan_applier._recover_orphaned_cleanups()
    def scan_needs_code_review_prs(self) -> None: self._github_workflow.scan_needs_code_review_prs(self.state)
    def scan_needs_rework_prs(self) -> None: self._github_workflow.scan_needs_rework_prs(self.state)
    def reconcile_orphaned_pr_labels(self) -> int: return self._github_workflow.reconcile_orphaned_pr_labels(ORCHESTRATOR_PR_MARKER)
    def launch_rework_session(self, rework: PendingRework) -> Optional[Session]: return _launch_rework_session(rework, self.state, self._session_launcher, self._session_restorer)

async def run_orchestrator(config_path: Optional[Path] = None) -> None:
    from .bootstrap import build_orchestrator
    config = Config.load(config_path) if config_path else Config.find_and_load()
    orchestrator = build_orchestrator(config)
    signal.signal(signal.SIGINT, lambda s, f: _handle_signal(orchestrator, s, f))
    signal.signal(signal.SIGTERM, lambda s, f: _handle_signal(orchestrator, s, f))
    await orchestrator.startup()
    await orchestrator.run_loop()
