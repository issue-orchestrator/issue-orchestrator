"""Main orchestrator - ties everything together."""

import asyncio, logging, signal, time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional, cast

if TYPE_CHECKING:
    from .control.planner import Planner, Plan, OrchestratorSnapshot
    from .control.session_manager import SessionManager
    from .control.label_sync import LabelSync
    from .control.action_applier import ActionApplier
    from .control.fact_gatherer import FactGatherer
    from .control.actions import LaunchSessionAction, EscalateToHumanAction
    from .models import TriageFacts

logger = logging.getLogger(__name__)

def log_transition(entity_type: str, number: int, from_state: str, to_state: str, reason: str, extra: dict | None = None) -> None:
    """Log state transition: [TRANSITION] {type} #{number}: {from} → {to} ({reason})"""
    logger.info(f"[TRANSITION] {entity_type} #{number}: {from_state} → {to_state} ({reason})")
    if extra: logger.debug(f"[TRANSITION] #{number} extra: {extra}")


from .config import Config
from .models import Issue, Session, SessionStatus, OrchestratorState, PendingReview, PendingRework, PendingTriageReview, PendingCleanup, AgentConfig, ORCHESTRATOR_PR_MARKER
from .observation.observer import SessionObserver
from .control.scheduler import Scheduler
from .control.dependency_evaluator import DependencyEvaluator
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
from .ports import EventSink, SessionRunner, TraceEvent, NullEventSink, NullSessionRunner, RepositoryHost
from .ports.worktree_manager import WorktreeManager
from .ports.working_copy import WorkingCopy


@dataclass
class Orchestrator:
    """Main orchestrator - mediates gather → plan → apply cycle. Dependencies injected via bootstrap."""
    config: Config
    events: EventSink = field(default_factory=NullEventSink)
    runner: SessionRunner = field(default_factory=NullSessionRunner)
    _repository_host: Optional[RepositoryHost] = field(default=None, repr=False)
    planner: Optional["Planner"] = field(default=None, repr=False)
    session_manager: Optional["SessionManager"] = field(default=None, repr=False)
    label_sync: Optional["LabelSync"] = field(default=None, repr=False)
    action_applier: Optional["ActionApplier"] = field(default=None, repr=False)
    fact_gatherer: Optional["FactGatherer"] = field(default=None, repr=False)
    pr_scanner: Optional["PRScanner"] = field(default=None, repr=False)
    session_restorer: Optional["SessionRestorer"] = field(default=None, repr=False)
    worktree_manager: Optional[WorktreeManager] = field(default=None, repr=False)
    working_copy: Optional[WorkingCopy] = field(default=None, repr=False)
    state_machine_manager: Optional[StateMachineManager] = field(default=None, repr=False)
    completion_processor: Optional["CompletionProcessor"] = field(default=None, repr=False)
    session_controller: Optional["SessionController"] = field(default=None, repr=False)
    state: OrchestratorState = field(default_factory=OrchestratorState)
    scheduler: Scheduler = field(init=False)
    observer: SessionObserver = field(init=False)
    _shutdown_requested: bool = field(default=False, init=False)
    _refresh_requested: bool = field(default=False, init=False)
    _last_issue_fetch: float = field(default=0.0, init=False)
    _last_ui_update: float = field(default=0.0, init=False)
    _loop_iteration: int = field(default=0, init=False)
    _ui_update_interval: int = field(default=30, init=False)

    def __post_init__(self):
        if self._repository_host is None:
            raise ValueError("RepositoryHost must be injected via bootstrap")
        if self.action_applier is None and self.worktree_manager is None:
            raise ValueError("Either action_applier or worktree_manager must be injected")

        dep_eval = DependencyEvaluator(self._repository_host, self.events)

        # Initialize components with injected or default values
        if self.planner:
            self.scheduler = self.planner.scheduler
        else:
            from .control.planner import Planner as P
            self.scheduler = Scheduler(self.config, dependency_evaluator=dep_eval)
            self.planner = P(self.config, self.scheduler, dep_eval)

        if not self.session_manager:
            from .control.session_manager import SessionManager as S
            self.session_manager = S(self.runner, self.events, self.config)

        if not self.action_applier:
            from .control.action_applier import ActionApplier as A
            self.action_applier = A(self.repository_host, self.session_manager, self.events, self.repository_host,
                                    self.worktree_manager, self.repository_host, True, self._session_launcher_callback)

        if not self.fact_gatherer:
            from .control.fact_gatherer import FactGatherer as F
            self.fact_gatherer = F(self.config, self.repository_host, self.events)

        self.observer = SessionObserver(self.config, self.events, self.runner, self._repository_host)

        if not self.state_machine_manager:
            self.state_machine_manager = StateMachineManager(self.config, self.events)

        self.issue_machines = self.state_machine_manager.issue_machines
        self.session_machines = self.state_machine_manager.session_machines
        self.review_machines = self.state_machine_manager.review_machines
        self.observer.session_machines = self.session_machines

    @property
    def repository_host(self) -> RepositoryHost:
        """Get the repository host (always initialized after __post_init__)."""
        assert self._repository_host is not None, "RepositoryHost not initialized"
        return self._repository_host

    @property
    def _completion_processor(self) -> CompletionProcessor:
        """Get the completion processor (must be injected)."""
        assert self.completion_processor is not None, "CompletionProcessor must be injected via bootstrap"
        return self.completion_processor

    @property
    def _session_controller(self) -> SessionController:
        """Get the session controller (must be injected)."""
        assert self.session_controller is not None, "SessionController must be injected via bootstrap"
        return self.session_controller

    @property
    def _pr_scanner(self) -> PRScanner:
        """Get the PR scanner (must be injected)."""
        assert self.pr_scanner is not None, "PRScanner must be injected via bootstrap"
        return self.pr_scanner

    @property
    def _session_launcher(self) -> SessionLauncher:
        return SessionLauncher(self.config, self.events, self.repository_host, self.session_manager, self.worktree_manager,
            self._session_exists, self._create_session, self._get_issue_machine, self._get_session_machine,
            self._get_review_machine, self._refresh_issue, getattr(self.scheduler, 'dependency_evaluator', None))

    @property
    def _cleanup_manager(self) -> CleanupManager:
        return CleanupManager(self.config, self.repository_host, self.worktree_manager, self._kill_session,
            self._session_exists, self._get_worktree_path, self._get_session_name)

    @property
    def _completion_handler(self) -> CompletionHandler:
        return CompletionHandler(self.config, self.events, self.repository_host,
            lambda n: self.issue_machines.get(n), lambda s: self.session_machines.get(s), lambda n: self.review_machines.get(n))

    @property
    def _session_restorer(self) -> SessionRestorer:
        return self.session_restorer or SessionRestorer(self.config, self.repository_host)

    @property
    def _state_machines(self) -> StateMachineManager:
        assert self.state_machine_manager is not None; return self.state_machine_manager

    def _get_session_name(self, number: int, session_type: str = "issue") -> str:
        if session_type not in ("issue", "review", "rework"): raise ValueError(f"Invalid session_type: {session_type}")
        return f"{session_type}-{number}"

    def _get_worktree_path(self, issue_number: int, agent_config: AgentConfig) -> Path:
        repo_root = agent_config.repo_root or self.config.repo_root
        return (Path(agent_config.worktree_base).resolve() if agent_config.worktree_base else repo_root.parent) / f"{repo_root.name}-{issue_number}"

    def _session_launcher_callback(self, session_type: str, number: int) -> Optional[Session]:
        handlers = {"issue": self._launch_issue_by_number, "review": self._launch_review_by_number, "rework": self._launch_rework_by_number, "triage": self._launch_triage_by_number}
        return handlers.get(session_type, lambda n: None)(number)

    def _launch_issue_by_number(self, n: int) -> Optional[Session]:
        issue = next((i for i in self.state.cached_queue_issues if i.number == n), None)
        if not issue: return None
        s = self.launch_session(issue); self.state.issues_started_count += 1 if s else 0; return s

    def _launch_review_by_number(self, n: int) -> Optional[Session]:
        r = next((r for r in self.state.pending_reviews if r.pr_number == n), None)
        return self.launch_review_session(r) if r else None

    def _launch_rework_by_number(self, n: int) -> Optional[Session]:
        r = next((r for r in self.state.pending_reworks if int(r.issue_key.stable_id()) == n), None)
        return self.launch_rework_session(r) if r else None

    def _launch_triage_by_number(self, n: int) -> Optional[Session]:
        t = next((t for t in self.state.pending_triage_reviews if t.issue_number == n), None)
        if t: self._launch_triage_session(t)
        return next((s for s in self.state.active_sessions if s.issue.number == n), None)

    def _get_issue_machine(self, n: int) -> IssueStateMachine: return self._state_machines.get_issue_machine(n)
    def _get_session_machine(self, name: str, n: int, timeout: int) -> SessionStateMachine: return self._state_machines.get_session_machine(name, n, timeout)
    def _get_review_machine(self, pr: int, issue: int) -> ReviewStateMachine: return self._state_machines.get_review_machine(pr, issue)

    async def _restore_running_sessions(self, running: list[dict]) -> None:
        self.state.active_sessions.extend(self._session_restorer.restore_sessions(running, self.state.active_sessions))

    def _parse_session_ref(self, session_name: str, operation: str) -> "SessionRef":
        from .control.session_manager import SessionRef
        try: return SessionRef.from_name(session_name)
        except ValueError as e: self.events.publish(TraceEvent("session.name_parse_error", {"session_name": session_name, "error": str(e)})); raise

    def _create_session(self, name: str, cmd: str, wd: Path, title: str | None = None) -> bool:
        from .control.session_manager import SessionContext
        return self.session_manager.start(SessionContext(ref=self._parse_session_ref(name, "create"), command=cmd, working_dir=wd, title=title))

    def _session_exists(self, name: str) -> bool: return self.session_manager.exists(self._parse_session_ref(name, "exists"))
    def _kill_session(self, name: str) -> None: self.session_manager.stop(self._parse_session_ref(name, "kill"))

    def _refresh_issue(self, n: int) -> Optional[Issue]:
        try: return self.repository_host.get_issue(n)
        except Exception as e: logger.warning("Failed to refresh issue #%d: %s", n, e); return None

    def _build_labels(self, *labels: str) -> list[str]:
        return list(labels) + ([self.config.filter_label] if self.config.filter_label else [])

    def _get_milestone_filter(self) -> str | None: return self.config.filter_milestone

    @property
    def _startup_manager(self) -> StartupManager:
        return StartupManager(self.config, self.events, self.runner, self.repository_host, self._session_exists,
            lambda r: self._restore_running_sessions(r), self.launch_session, self.update_queue_cache)

    async def startup(self) -> None: await self._startup_manager.run_startup(self.state)

    def launch_session(self, issue: Issue) -> Optional[Session]:
        result = self._session_launcher.launch_issue_session(issue, self.state.active_sessions)
        if result.success and result.session: self.state.active_sessions.append(result.session)
        return result.session if result.success else None

    def handle_session_completion(self, session: Session, status: SessionStatus) -> None:
        from .models import DiscoveredReview, DiscoveredFailure
        name = session.tmux_session_name
        entity = "review" if name.startswith("review-") else ("rework" if name.startswith("rework-") else "issue")
        log_transition(entity, session.issue.number, "ACTIVE", status.value.upper(), f"runtime={session.runtime_minutes}min")
        self.state.active_sessions = [s for s in self.state.active_sessions if s.issue.number != session.issue.number]
        self.observer.handle_completion(session, status)
        if status == SessionStatus.COMPLETED: self.state.completed_today.append(session.issue.number)
        result = self._completion_handler.process_completion(session, status)
        self.state.session_history.append(result.history_entry)
        if result.should_defer_cleanup and result.pending_cleanup: self.state.pending_cleanups.append(result.pending_cleanup)
        else: self._immediate_cleanup(session, status)
        if result.should_queue_review and result.pr_url and result.pr_number:
            self.state.discovered_reviews.append(DiscoveredReview(session.issue.number, result.pr_number, result.pr_url, session.branch_name))
        if status in (SessionStatus.FAILED, SessionStatus.TIMED_OUT):
            self.state.discovered_failures.append(DiscoveredFailure(session.issue.number, session.issue.title, status.value))

    def _immediate_cleanup(self, session: Session, status: SessionStatus) -> None:
        if status == SessionStatus.COMPLETED and (self.config.cleanup.without_triage.close_ai_session_tabs or not self.config.code_review_agent):
            try: self.worktree_manager.remove(session.worktree_path) if self.worktree_manager else None
            except: pass
        try: self._kill_session(session.tmux_session_name)
        except: pass

    def tick(self) -> bool:
        self._loop_iteration += 1
        logger.info("[LOOP] Iteration %d - active=%d, paused=%s", self._loop_iteration, len(self.state.active_sessions), self.state.paused)
        if self._shutdown_requested: return False
        self._process_active_sessions()
        self.scan_needs_code_review_prs(); self.scan_needs_rework_prs()
        if not self.state.paused and len(self.state.active_sessions) < self.config.max_concurrent_sessions:
            self._run_planning_cycle()
        self._emit_ui_update_if_needed()
        return True

    def _process_active_sessions(self) -> None:
        for session in list(self.state.active_sessions):
            obs = self.observer.observe_session(session)
            if obs.observation == SessionObservation.RUNNING: continue
            decision = self._session_controller.decide_outcome(obs, session.worktree_path, session.issue.number,
                session.issue.title, session.tmux_session_name, session.completion_path)
            self.handle_session_completion(session, decision.status)

    def _run_planning_cycle(self) -> None:
        """Fetch issues, create snapshot, plan, and apply."""
        should_fetch = (time.time() - self._last_issue_fetch >= self.config.queue_refresh_seconds) or self._refresh_requested

        if should_fetch:
            logger.info("[FETCH] %s refresh", "Manual" if self._refresh_requested else "Scheduled")
            self._refresh_requested = False
            all_issues = self._fetch_all_issues()
            self._last_issue_fetch = time.time()
            _, dep_blocked = self.scheduler.get_available_issues(all_issues)
            self._update_dependency_problems(dep_blocked)
            exclude = {e.issue_number for e in self.state.session_history} | {s.issue.number for s in self.state.active_sessions}
            filtered = [i for i in all_issues if i.number not in exclude]
            self.state.cached_queue_issues = [i for i in filtered if i.number == self.config.filter_issue] if self.config.filter_issue else filtered

        snapshot = self.fact_gatherer.create_snapshot(self.state, self.state.cached_queue_issues)
        plan = self.planner.plan(snapshot)
        if plan.action_count > 0:
            logger.info("[PLAN] %d action(s): %s", plan.action_count, ", ".join(f"{a.action_type.value}:{getattr(a, 'number', '?')}" for a in plan.actions))
        self._apply_plan(plan)
        self._clear_discovered_facts()

    def _clear_discovered_facts(self) -> None:
        for attr in ("discovered_reviews", "discovered_reworks", "discovered_escalations", "discovered_failures"):
            getattr(self.state, attr).clear()

    def _emit_ui_update_if_needed(self) -> None:
        if time.time() - self._last_ui_update >= self._ui_update_interval and self.state.active_sessions:
            self.events.publish(TraceEvent("orchestrator.state_changed", {
                "active_count": len(self.state.active_sessions), "sessions": [s.issue.number for s in self.state.active_sessions]}))
            self._last_ui_update = time.time()

    async def run_loop(self) -> None:
        print("Starting orchestration loop...")
        self.reconcile_orphaned_pr_labels()
        self._last_issue_fetch, self._last_ui_update, self._loop_iteration = 0.0, time.time(), 0
        while not self._shutdown_requested:
            try:
                if not self.tick(): break
            except Exception as e:
                logger.exception("[LOOP] Error in iteration %d: %s", self._loop_iteration, e)
            await asyncio.sleep(10)

    def request_shutdown(self, force: bool = False) -> None:
        """Request graceful or forced shutdown."""
        self._shutdown_requested = True
        active = self.state.active_sessions
        if not active:
            print("Shutdown requested - no active sessions, exiting...")
            return
        if force:
            print(f"Force shutdown - killing {len(active)} session(s)")
            for s in active:
                try: self._kill_session(s.tmux_session_name)
                except Exception as e: print(f"  Warning: {e}")
            self.state.active_sessions = []
        else:
            print(f"Shutdown requested - waiting for {len(active)} session(s). Ctrl+C again to force.")

    def request_refresh(self) -> None:
        self._refresh_requested = True
        logger.info("[REFRESH] Manual refresh requested")

    def pause(self) -> None:
        self.state.paused = True
        print("Orchestrator paused")
        self.events.publish(TraceEvent("orchestrator.paused"))

    def resume(self) -> None:
        self.state.paused = False
        print("Orchestrator resumed")
        self.events.publish(TraceEvent("orchestrator.resumed"))

    def _apply_plan(self, plan: "Plan") -> None:
        for action in plan.actions:
            if self.state.paused: break
            try:
                result = self.action_applier.apply(action)
                if result.success: self._update_state_after_action(action, result)
                else: logger.warning("[PLAN] Action %s failed: %s", action.action_type.value, result.error)
            except Exception as e:
                logger.exception("Failed to apply action %s: %s", action, e)

    def _update_state_after_action(self, action: "Action", result: "ActionResult") -> None:
        from .control.actions import ActionType, LaunchSessionAction, CreateTriageIssueAction, CleanupSessionAction, EscalateToHumanAction, QueueReviewAction, QueueReworkAction, QueueTriageAction
        t = action.action_type
        if t == ActionType.LAUNCH_SESSION:
            logger.info("[PLAN] Launched %s session for #%d", cast(LaunchSessionAction, action).session_type, cast(LaunchSessionAction, action).number)
        elif t == ActionType.ESCALATE_TO_HUMAN:
            a = cast(EscalateToHumanAction, action); logger.info("[PLAN] Escalated PR #%d (cycle %d)", a.pr_number, a.rework_cycles)
        elif t == ActionType.CREATE_TRIAGE_ISSUE:
            a, num = cast(CreateTriageIssueAction, action), result.details.get("issue_number")
            if num: self.state.pending_triage_reviews.append(PendingTriageReview(num, a.title)); print(f"Created triage #{num}")
        elif t == ActionType.CLEANUP_SESSION:
            self.state.pending_cleanups = [c for c in self.state.pending_cleanups if c.pr_number != cast(CleanupSessionAction, action).pr_number]
        elif t == ActionType.QUEUE_REVIEW:
            a = cast(QueueReviewAction, action)
            if not any(r.pr_number == a.pr_number for r in self.state.pending_reviews):
                self.state.pending_reviews.append(PendingReview(a.issue_number, a.pr_number, a.pr_url, a.branch_name))
                log_transition("review", a.pr_number, "CREATED", "QUEUED", f"from #{a.issue_number}")
                self._get_review_machine(a.pr_number, a.issue_number)
        elif t == ActionType.QUEUE_REWORK:
            a = cast(QueueReworkAction, action)
            if not any(int(r.issue_key.stable_id()) == a.issue_number for r in self.state.pending_reworks):
                agent = next((r.agent_type for r in self.state.discovered_reworks if r.issue_number == a.issue_number), "agent:developer")
                self.state.pending_reworks.append(PendingRework(self.repository_host.create_issue_key(a.issue_number), agent, a.rework_cycle))
                log_transition("rework", a.issue_number, "CREATED", "QUEUED", f"cycle {a.rework_cycle}")
        elif t == ActionType.QUEUE_TRIAGE:
            a = cast(QueueTriageAction, action)
            if not any(t.issue_number == a.issue_number for t in self.state.pending_triage_reviews):
                self.state.pending_triage_reviews.append(PendingTriageReview(a.issue_number, a.title))

    def _fetch_all_issues(self) -> list[Issue]:
        """Fetch all issues from GitHub - delegates to FactGatherer."""
        base_labels = [self.config.filter_label] if self.config.filter_label else []
        return self.fact_gatherer.fetch_issues(base_labels, self._get_milestone_filter())

    def update_queue_cache(self) -> None:
        """Update the cached queue issues and emit queue.changed event if changed."""
        from .audit import get_queue_issues
        try:
            queue_issues = get_queue_issues(self.config, self.state, issue_tracker=self.repository_host)
            old, new = {i.number for i in self.state.cached_queue_issues}, {i.number for i in queue_issues}
            added, removed = new - old, old - new
            self.state.cached_queue_issues = queue_issues
            if added or removed:
                self.events.publish(TraceEvent("queue.changed", {
                    "added": [{"number": i.number, "title": i.title} for i in queue_issues if i.number in added],
                    "removed": [{"number": num} for num in removed], "total": len(queue_issues),
                }))
                logger.info("Queue changed: %d added, %d removed, %d total", len(added), len(removed), len(queue_issues))
        except Exception as e:
            logger.warning("Failed to update queue cache: %s", e)

    def _update_dependency_problems(self, dep_blocked: list[tuple["Issue", str]]) -> None:
        from .models import DependencyProblem
        new = {i.number: DependencyProblem(i.number, i.title, [], r) for i, r in dep_blocked}
        blocked, unblocked = set(new) - set(self.state.dependency_problems), set(self.state.dependency_problems) - set(new)
        for n in blocked: self.events.publish(TraceEvent("dependency.blocked", {"issue_number": n, "summary": new[n].summary}))
        for n in unblocked: self.events.publish(TraceEvent("dependency.unblocked", {"issue_number": n}))
        self.state.dependency_problems = new

    def launch_review_session(self, review: PendingReview) -> Optional[Session]:
        result = self._session_launcher.launch_review_session(review, self.state.active_sessions)
        self.state.pending_reviews = [r for r in self.state.pending_reviews if r.pr_number != review.pr_number]
        if result.success and result.session: self.state.active_sessions.append(result.session)
        return result.session if result.success else None

    def _launch_triage_session(self, triage: PendingTriageReview) -> None:
        agent = self.config.triage_review_agent
        if not agent or agent not in self.config.agents: raise ValueError(f"Invalid triage agent: {agent}")
        self.launch_session(Issue(triage.issue_number, triage.title, [agent]))

    def process_deferred_cleanups(self) -> None:
        self.state.pending_cleanups = self._cleanup_manager.process_deferred_cleanups(self.state.pending_cleanups)

    def _recover_orphaned_cleanups(self) -> None:
        self._cleanup_manager.recover_orphaned_cleanups(lambda msg: setattr(self.state, 'startup_message', msg))

    def scan_needs_code_review_prs(self) -> None:
        from .models import DiscoveredReview
        for r in self._pr_scanner.scan_for_reviews(self.state.pending_reviews, [s.tmux_session_name for s in self.state.active_sessions]):
            self.state.discovered_reviews.append(DiscoveredReview(r.issue_number, r.pr_number, r.pr_url, r.branch_name))

    def scan_needs_rework_prs(self) -> None:
        from .models import DiscoveredRework, DiscoveredEscalation
        reworks, escalations = self._pr_scanner.scan_for_reworks(self.state.pending_reworks, [s.issue.number for s in self.state.active_sessions])
        for pr, issue, cycle in escalations: self.state.discovered_escalations.append(DiscoveredEscalation(issue, pr, cycle))
        for r in reworks: self.state.discovered_reworks.append(DiscoveredRework(int(r.issue_key.stable_id()), 0, "", r.agent_type, r.rework_cycle))

    def reconcile_orphaned_pr_labels(self) -> int:
        if not self.config.code_review_label or not self.config.repo or not self.label_sync: return 0
        return self.label_sync.reconcile_orphaned_pr_labels(self.config.code_review_label, self.config.code_reviewed_label, ORCHESTRATOR_PR_MARKER)

    def launch_rework_session(self, rework: PendingRework) -> Optional[Session]:
        result = self._session_launcher.launch_rework_session(rework, self.state.active_sessions)
        self.state.pending_reworks = [r for r in self.state.pending_reworks if r.issue_key != rework.issue_key]
        if result.success and result.session: self.state.active_sessions.append(result.session)
        return result.session if result.success else None

    def prioritize(self, n: int) -> None:
        if n not in self.state.priority_queue: self.state.priority_queue.insert(0, n)


async def run_orchestrator(config_path: Optional[Path] = None) -> None:
    from .bootstrap import build_orchestrator
    config = Config.load(config_path) if config_path else Config.find_and_load()
    orchestrator = build_orchestrator(config)
    def handle_signal(signum, frame):
        orchestrator.request_shutdown(force=orchestrator._shutdown_requested)
    signal.signal(signal.SIGINT, handle_signal); signal.signal(signal.SIGTERM, handle_signal)
    await orchestrator.startup()
    await orchestrator.run_loop()
