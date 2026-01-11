"""OrchestratorSupport - extracted methods from Orchestrator per IMPLEMENT_THIS.md Phase 3.

This class holds methods moved from orchestrator.py to reduce its size.
The Orchestrator delegates to this class for support operations.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Optional, Callable, cast

if TYPE_CHECKING:
    from types import FrameType
    from ..domain.models import OrchestratorState, Session, SessionStatus
    from ..infra.config import Config
    from ..infra.orchestrator import Orchestrator
    from .planner import Plan, Planner
    from .action_applier import ActionApplier, ActionResult
    from .actions import Action
    from .cleanup_manager import CleanupManager
    from .session_manager import SessionManager
    from .fact_gatherer import FactGatherer
    from .state_machine_manager import StateMachineManager
    from .dependency_evaluator import DependencyEvaluator
    from .health_gate import HealthGate, HealthDecision
    from ..ports.worktree_manager import WorktreeManager
    from ..ports.issue import Issue

from ..events import EventName, EventContext
from ..ports import EventSink, TraceEvent, RepositoryHost
from .reconciliation import ReconciliationRequired, get_pause_label
from ..domain.models import (
    PendingReview, PendingRework, PendingTriageReview,
)

logger = logging.getLogger(__name__)


def log_transition(
    entity_type: str,
    number: int,
    from_state: str,
    to_state: str,
    reason: str,
    extra: dict[str, object] | None = None,
) -> None:
    """Log state transition: [TRANSITION] {type} #{number}: {from} → {to} ({reason})"""
    logger.info(f"[TRANSITION] {entity_type} #{number}: {from_state} → {to_state} ({reason})")
    if extra:
        logger.debug(f"[TRANSITION] #{number} extra: {extra}")


def init_orchestrator_components(orch: "Orchestrator", dep_eval: "DependencyEvaluator") -> None:
    """Initialize orchestrator components - moved per method table.

    This is the core logic from Orchestrator.__post_init__.
    All dependencies are now provided via OrchestratorDeps (no fallbacks needed).
    Helpers are exposed via @cached_property on Orchestrator - no fields set here.
    """
    from ..observation.observer import SessionObserver

    # Wire up the scheduler from the planner
    orch.scheduler = orch.deps.planner.scheduler

    # Wire up action_applier's session_launcher callback
    orch.deps.action_applier.session_launcher = orch._session_launcher_callback

    # Create observer (still created here as it depends on runtime orchestrator state)
    orch.observer = SessionObserver(
        config=orch.config,
        events=orch.deps.events,
        session_runner=orch.deps.runner,
        repository_host=orch.deps.repository_host,
        fresh_issue_reader=orch.deps.fresh_issue_reader,
    )
    orch.observer.session_machines = orch.deps.state_machine_manager.session_machines


@dataclass
class OrchestratorSupport:
    """Support class holding methods extracted from Orchestrator."""

    config: "Config"
    events: EventSink
    repository_host: RepositoryHost
    state: "OrchestratorState"
    event_context: EventContext
    session_manager: "SessionManager"
    action_applier: "ActionApplier"
    fact_gatherer: "FactGatherer"
    planner: "Planner"
    worktree_manager: "WorktreeManager"
    state_machine_manager: "StateMachineManager"
    cleanup_manager: "CleanupManager"
    get_review_machine: Callable[[int, int], object]
    kill_session: Callable[[str], None]

    _last_ui_update: float = field(default=0.0, init=False)
    _ui_update_interval: int = field(default=30, init=False)

    # Accessor properties per method table
    @property
    def _sm(self) -> "SessionManager":
        return self.session_manager

    @property
    def _aa(self) -> "ActionApplier":
        return self.action_applier

    @property
    def _fg(self) -> "FactGatherer":
        return self.fact_gatherer

    @property
    def _planner(self) -> "Planner":
        return self.planner

    @property
    def _wm(self) -> "WorktreeManager":
        return self.worktree_manager

    @property
    def _state_machines(self) -> "StateMachineManager":
        return self.state_machine_manager

    @property
    def _cleanup_manager(self) -> "CleanupManager":
        return self.cleanup_manager

    def _get_milestone_filter(self) -> str | None:
        return self.config.filtering.milestone

    def _immediate_cleanup(self, session: "Session", status: "SessionStatus") -> None:
        from ..domain.models import SessionStatus
        if status == SessionStatus.COMPLETED and (
            self.config.cleanup.without_triage.close_ai_session_tabs or not self.config.code_review_agent
        ):
            try:
                self.worktree_manager.remove(session.worktree_path) if self.worktree_manager else None
            except Exception:
                pass
        try:
            self.kill_session(session.terminal_id)
        except Exception:
            pass

    def _check_health(self, health_gate: "HealthGate") -> object:
        """Check system health using HealthGate service."""
        return health_gate.check(
            active_sessions=len(self.state.active_sessions),
            paused=self.state.paused,
        )

    def _clear_discovered_facts(self) -> None:
        for attr in ("discovered_reviews", "discovered_reworks", "discovered_escalations", "discovered_failures"):
            getattr(self.state, attr).clear()

    def _emit_heartbeat_if_needed(self) -> None:
        if time.time() - self._last_ui_update >= self._ui_update_interval and self.state.active_sessions:
            self.events.publish(TraceEvent(
                EventName.ORCHESTRATOR_HEARTBEAT,
                self.event_context.enrich({
                    "active_count": len(self.state.active_sessions),
                    "sessions": [s.issue.number for s in self.state.active_sessions],
                }),
            ))
            self._last_ui_update = time.time()

    def request_refresh(self, inflight_stable_ids: set[str] | None, inflight_dict: dict[str, float], ttl: float) -> None:
        if inflight_stable_ids:
            now = time.monotonic()
            expires_at = now + ttl
            for stable_id in inflight_stable_ids:
                inflight_dict[stable_id] = expires_at
            logger.info("[REFRESH] Manual refresh requested with %d inflight IDs: %s",
                       len(inflight_stable_ids), sorted(inflight_stable_ids))
        else:
            logger.info("[REFRESH] Manual refresh requested")

    def _apply_plan(self, plan: "Plan", pause_issue_callback: Callable[[int, str], None]) -> None:
        if plan.action_count == 0:
            return

        self.events.publish(TraceEvent(
            EventName.APPLY_STARTED,
            self.event_context.enrich({"steps": plan.action_count}),
        ))

        applied_count = 0
        failed_count = 0

        from .actions import ActionType

        for action in plan.actions:
            if self.state.paused:
                break
            try:
                if action.action_type == ActionType.CREATE_TRIAGE_ISSUE and self._cleanup_manager:
                    if not self._cleanup_manager.should_retry_triage_issue():
                        logger.warning("[PLAN] Skipping triage issue creation due to cooldown")
                        failed_count += 1
                        self.events.publish(TraceEvent(
                            EventName.APPLY_FAILED,
                            self.event_context.enrich({
                                "step_type": action.action_type.value,
                                "issue_number": getattr(action, "number", getattr(action, "issue_number", None)),
                                "error": "triage_issue_creation_cooldown",
                            }),
                        ))
                        continue
                result = self._aa.apply(action)
                if result.success:
                    self._update_state_after_action(action, result)
                    applied_count += 1
                    self.events.publish(TraceEvent(
                        EventName.APPLY_STEP_APPLIED,
                        self.event_context.enrich({
                            "step_type": action.action_type.value,
                            "issue_number": getattr(action, "number", getattr(action, "issue_number", None)),
                            "result": "success",
                        }),
                    ))
                else:
                    failed_count += 1
                    logger.warning("[PLAN] Action %s failed: %s", action.action_type.value, result.error)
                    if action.action_type.value == "create_triage_issue":
                        try:
                            self._cleanup_manager.mark_triage_issue_failure()
                        except Exception:
                            pass
                    self.events.publish(TraceEvent(
                        EventName.APPLY_FAILED,
                        self.event_context.enrich({
                            "step_type": action.action_type.value,
                            "issue_number": getattr(action, "number", getattr(action, "issue_number", None)),
                            "error": result.error or "unknown",
                        }),
                    ))
            except ReconciliationRequired as rr:
                issue_number = rr.entity_id
                failed_count += 1
                logger.warning(
                    "[RECONCILIATION] Drift detected for %s #%d: %s",
                    rr.entity_type, issue_number, rr.reason
                )
                self.events.publish(TraceEvent(
                    EventName.RECONCILIATION_REQUIRED,
                    self.event_context.enrich({
                        "issue_number": issue_number,
                        "entity_type": rr.entity_type,
                        "reason": rr.reason,
                        "expected_labels": list(rr.expected.labels),
                        "actual_labels": list(rr.actual.labels),
                    }),
                ))
                pause_issue_callback(issue_number, rr.reason)
                break
            except Exception as e:
                failed_count += 1
                logger.exception("Failed to apply action %s: %s", action, e)
                self.events.publish(TraceEvent(
                    EventName.APPLY_FAILED,
                    self.event_context.enrich({
                        "step_type": action.action_type.value,
                        "error": str(e),
                    }),
                ))

        self.events.publish(TraceEvent(
            EventName.APPLY_COMPLETED,
            self.event_context.enrich({
                "applied_steps": applied_count,
                "failed_steps": failed_count,
            }),
        ))

    def _update_state_after_action(self, action: "Action", result: "ActionResult") -> None:
        from .actions import (
            ActionType, LaunchSessionAction, CreateTriageIssueAction,
            CleanupSessionAction, EscalateToHumanAction, QueueReviewAction,
            QueueReworkAction, QueueTriageAction,
        )

        t = action.action_type
        if t == ActionType.LAUNCH_SESSION:
            a = cast(LaunchSessionAction, action)
            logger.info("[PLAN] Launched %s session for #%d", a.session_type, a.number)
        elif t == ActionType.ESCALATE_TO_HUMAN:
            a = cast(EscalateToHumanAction, action)
            logger.info("[PLAN] Escalated PR #%d (cycle %d)", a.pr_number, a.rework_cycles)
        elif t == ActionType.CREATE_TRIAGE_ISSUE:
            a = cast(CreateTriageIssueAction, action)
            num = result.details.get("issue_number")
            if num:
                self.state.pending_triage_reviews.append(PendingTriageReview(num, a.title))
                logger.info("Created triage #%d", num)
        elif t == ActionType.CLEANUP_SESSION:
            a = cast(CleanupSessionAction, action)
            self.state.pending_cleanups = [
                c for c in self.state.pending_cleanups if c.pr_number != a.pr_number
            ]
        elif t == ActionType.QUEUE_REVIEW:
            a = cast(QueueReviewAction, action)
            if not any(r.pr_number == a.pr_number for r in self.state.pending_reviews):
                self.state.pending_reviews.append(
                    PendingReview(
                        issue_key=self.repository_host.create_issue_key(a.issue_number),
                        pr_number=a.pr_number,
                        pr_url=a.pr_url,
                        branch_name=a.branch_name,
                        _issue_number=a.issue_number,
                        agent_label=a.agent_label,
                    )
                )
                log_transition("review", a.pr_number, "CREATED", "QUEUED", f"from #{a.issue_number}")
                self.get_review_machine(a.pr_number, a.issue_number)
        elif t == ActionType.QUEUE_REWORK:
            a = cast(QueueReworkAction, action)
            if not any(int(r.issue_key.stable_id()) == a.issue_number for r in self.state.pending_reworks):
                agent = next(
                    (r.agent_type for r in self.state.discovered_reworks if r.issue_number == a.issue_number),
                    "agent:developer",
                )
                self.state.pending_reworks.append(
                    PendingRework(
                        self.repository_host.create_issue_key(a.issue_number),
                        agent,
                        a.rework_cycle,
                    )
                )
                log_transition("rework", a.issue_number, "CREATED", "QUEUED", f"cycle {a.rework_cycle}")
        elif t == ActionType.QUEUE_TRIAGE:
            a = cast(QueueTriageAction, action)
            if not any(t.issue_number == a.issue_number for t in self.state.pending_triage_reviews):
                self.state.pending_triage_reviews.append(PendingTriageReview(a.issue_number, a.title))

    def update_queue_cache(self) -> None:
        from .queue_projection import QueueProjection
        projection = QueueProjection(self.config, self.repository_host, self.events)
        projection.update_and_emit(self.state)

    def _recover_orphaned_cleanups(self) -> None:
        self._cleanup_manager.recover_orphaned_cleanups(
            lambda msg: setattr(self.state, 'startup_message', msg)
        )


def pause_issue_for_reconciliation(
    events: EventSink,
    repository_host: RepositoryHost,
    event_context: EventContext,
    issue_number: int,
    reason: str,
) -> None:
    """Pause an issue due to reconciliation failure (state drift)."""
    pause_label = get_pause_label()
    try:
        repository_host.add_label(issue_number, pause_label)
        logger.warning(
            "[RECONCILIATION] Paused issue #%d with label '%s': %s",
            issue_number, pause_label, reason
        )
        events.publish(TraceEvent(
            EventName.ISSUE_PAUSED_RECONCILE,
            event_context.enrich({
                "issue_number": issue_number,
                "pause_label": pause_label,
                "reason": reason,
            }),
        ))
    except Exception as e:
        logger.error(
            "[RECONCILIATION] Failed to add pause label to #%d: %s",
            issue_number, e
        )


def clear_discovered_facts(state: "OrchestratorState") -> None:
    """Clear discovered facts from state - moved per method table."""
    state.discovered_reviews.clear()
    state.discovered_reworks.clear()
    state.discovered_escalations.clear()
    state.discovered_failures.clear()
    state.immediate_cleanups.clear()


def emit_heartbeat_if_needed(
    events: EventSink,
    event_context: EventContext,
    state: "OrchestratorState",
    last_ui_update: float,
    ui_update_interval: int = 30,
) -> float:
    """Emit heartbeat if needed - moved per method table.

    Returns:
        Updated last_ui_update timestamp
    """
    now = time.time()
    if now - last_ui_update >= ui_update_interval:
        events.publish(TraceEvent(
            EventName.ORCHESTRATOR_IDLE,
            event_context.enrich({
                "active_sessions": len(state.active_sessions),
                "paused": state.paused,
            }),
        ))
        return now
    return last_ui_update


def check_health(
    health_gate: "HealthGate",
    active_sessions_count: int,
    paused: bool,
) -> "HealthDecision":
    """Check system health - moved per method table."""
    return health_gate.check(
        active_sessions=active_sessions_count,
        paused=paused,
    )


def handle_signal(
    orchestrator: "Orchestrator",
    signum: int,
    frame: Optional["FrameType"],
) -> None:
    """Handle OS signal (SIGINT, SIGTERM) - moved per method table.

    Args:
        orchestrator: The Orchestrator instance.
        signum: Signal number.
        frame: Stack frame (unused).
    """
    # Force shutdown if already requested, otherwise graceful
    orchestrator.request_shutdown(force=orchestrator._shutdown_requested)


# Keep PlanApplier for backward compatibility during transition
PlanApplier = OrchestratorSupport


def run_planning_cycle(
    config: "Config",
    events: EventSink,
    event_context: EventContext,
    state: "OrchestratorState",
    fact_gatherer: "FactGatherer",
    planner: "Planner",
    repository_host: RepositoryHost,
    scheduler: object,
    github_workflow: object,
    apply_plan_fn: Callable[["Plan"], None],
    clear_discovered_facts_fn: Callable[[], None],
    last_issue_fetch: float,
    refresh_requested: bool,
    inflight_stable_ids: dict[str, float],
    observer: object | None = None,
) -> tuple[float, bool]:
    """Run the planning cycle - extracted from Orchestrator per move map Step 2.

    Returns:
        Tuple of (updated_last_issue_fetch, updated_refresh_requested)
    """
    should_fetch = (time.time() - last_issue_fetch >= config.queue_refresh_seconds) or refresh_requested

    if should_fetch:
        manual_refresh = refresh_requested
        # Collect required inflight IDs before fetch
        required_stable_ids = set(inflight_stable_ids.keys()) if inflight_stable_ids else None
        if required_stable_ids:
            logger.info("[FETCH] %s refresh with %d required inflight IDs: %s",
                       "Manual" if manual_refresh else "Scheduled",
                       len(required_stable_ids), sorted(required_stable_ids))
        else:
            logger.info("[FETCH] %s refresh", "Manual" if manual_refresh else "Scheduled")
        refresh_requested = False
        from ..infra import gh_audit
        reason = gh_audit.AuditReason.QUEUE_REFRESH_MANUAL if manual_refresh else gh_audit.AuditReason.QUEUE_REFRESH_SCHEDULED
        scope = gh_audit.AuditScope.MANUAL if manual_refresh else gh_audit.AuditScope.PERIODIC
        with gh_audit.context(reason=reason, scope=scope):
            all_issues = github_workflow.fetch_all_issues(config.filtering.milestone, required_stable_ids)
        last_issue_fetch = time.time()

        # Remove discovered inflight IDs
        if required_stable_ids:
            discovered_ids = {i.key.stable_id() for i in all_issues}
            found = required_stable_ids & discovered_ids
            if found:
                for sid in found:
                    inflight_stable_ids.pop(sid, None)
                logger.info("[INFLIGHT] Discovered %d/%d required IDs: %s",
                           len(found), len(required_stable_ids), sorted(found))
            still_missing = required_stable_ids - discovered_ids
            if still_missing:
                logger.warning("[INFLIGHT] Still missing %d required IDs after refresh: %s",
                              len(still_missing), sorted(still_missing))
        for issue in all_issues:
            try:
                repository_host.update_label_cache(issue.number, list(issue.labels))
            except Exception:
                logger.debug("Failed to update label cache for issue #%s", issue.number, exc_info=True)
        github_workflow.scan_needs_code_review_prs(state)
        github_workflow.scan_needs_rework_prs(state)
        _, dep_blocked = scheduler.get_available_issues(all_issues)
        github_workflow.update_dependency_problems(state, dep_blocked)
        exclude = {e.issue_number for e in state.session_history} | {s.issue.number for s in state.active_sessions}
        filtered = [i for i in all_issues if i.number not in exclude]
        new_queue = [i for i in filtered if i.number == config.filtering.issue] if config.filtering.issue else filtered

        # Detect queue changes and emit event
        old_numbers = {i.number for i in state.cached_queue_issues}
        new_numbers = {i.number for i in new_queue}
        added_numbers = new_numbers - old_numbers
        removed_numbers = old_numbers - new_numbers
        if added_numbers or removed_numbers:
            added = [{"number": i.number, "title": i.title} for i in new_queue if i.number in added_numbers]
            removed = [{"number": n} for n in removed_numbers]
            events.publish(TraceEvent(
                EventName.QUEUE_CHANGED,
                {"added": added, "removed": removed, "total": len(new_queue)},
            ))
            logger.info("Queue changed: %d added, %d removed, %d total",
                       len(added), len(removed), len(new_queue))

        state.cached_queue_issues = new_queue

        # Clear failed_this_cycle on cache refresh - GitHub now has the blocked-failed labels
        if state.failed_this_cycle:
            logger.info(
                "[REFRESH] Clearing failed_this_cycle: %s (labels now synced from GitHub)",
                state.failed_this_cycle,
            )
            state.failed_this_cycle.clear()

    # Detect stale in-progress issues (label present but no active session)
    stale_issues = []
    if observer and hasattr(observer, 'detect_stale_in_progress'):
        stale_issues = observer.detect_stale_in_progress(
            state.cached_queue_issues,
            state.active_sessions,
        )
        # Emit events for each stale issue
        for issue in stale_issues:
            events.publish(TraceEvent(
                EventName.STALE_IN_PROGRESS_DETECTED,
                event_context.enrich({
                    "issue_number": issue.number,
                    "labels": list(issue.labels),
                }),
            ))

    snapshot = fact_gatherer.create_snapshot(
        state, state.cached_queue_issues, stale_in_progress_issues=stale_issues
    )

    # Emit facts.gathered
    events.publish(TraceEvent(
        EventName.FACTS_GATHERED,
        event_context.enrich({
            "issues_count": len(state.cached_queue_issues),
            "active_sessions": len(state.active_sessions),
            "pending_reviews": len(state.pending_reviews),
            "pending_reworks": len(state.pending_reworks),
            "stale_in_progress_count": len(stale_issues),
        }),
    ))

    plan = planner.plan(snapshot)

    # Emit plan.computed
    events.publish(TraceEvent(
        EventName.PLAN_COMPUTED,
        event_context.enrich({
            "steps": plan.action_count,
            "actions": [a.action_type.value for a in plan.actions],
        }),
    ))

    if plan.action_count > 0:
        logger.info("[PLAN] %d action(s): %s", plan.action_count, ", ".join(f"{a.action_type.value}:{getattr(a, 'number', '?')}" for a in plan.actions))

    apply_plan_fn(plan)

    # Track consecutive stale ticks for escalation (if threshold > 0)
    _track_stale_ticks(config, events, event_context, state, stale_issues)

    clear_discovered_facts_fn()

    return last_issue_fetch, refresh_requested


def _track_stale_ticks(
    config: "Config",
    events: EventSink,
    event_context: EventContext,
    state: "OrchestratorState",
    stale_issues: list["Issue"],
) -> None:
    """Track consecutive ticks with stale in-progress labels for escalation.

    Updates state.stale_issue_ticks and emits PERSISTENT_STALE_DETECTED
    if an issue exceeds the configured threshold.
    """
    current_stale = {i.number for i in stale_issues}

    # Increment for current stale issues
    for issue_num in current_stale:
        state.stale_issue_ticks[issue_num] = state.stale_issue_ticks.get(issue_num, 0) + 1

    # Clear for no-longer-stale issues and emit cleared event
    for issue_num in list(state.stale_issue_ticks.keys()):
        if issue_num not in current_stale:
            del state.stale_issue_ticks[issue_num]
            events.publish(TraceEvent(
                EventName.STALE_IN_PROGRESS_CLEARED,
                event_context.enrich({"issue_number": issue_num}),
            ))
            logger.info("[STALE] Issue #%d is no longer stale", issue_num)

    # Emit event if threshold exceeded (and threshold > 0)
    threshold = config.stale_escalation_ticks
    if threshold > 0:
        for issue_num, ticks in state.stale_issue_ticks.items():
            if ticks >= threshold:
                events.publish(TraceEvent(
                    EventName.PERSISTENT_STALE_DETECTED,
                    event_context.enrich({
                        "issue_number": issue_num,
                        "consecutive_ticks": ticks,
                        "threshold": threshold,
                    }),
                ))
                logger.warning(
                    "[STALE] Issue #%d has been stale for %d consecutive ticks (threshold: %d)",
                    issue_num, ticks, threshold
                )


# Backwards compatibility - keep PlanningCycleRunner as an alias
PlanningCycleRunner = run_planning_cycle


def run_tick(
    loop_iteration: int,
    event_context: EventContext,
    inflight_stable_ids: dict[str, float],
    state: "OrchestratorState",
    events: EventSink,
    shutdown_requested: bool,
    process_active_sessions_fn: Callable[[], None],
    check_health_fn: Callable[[], "HealthDecision"],
    run_planning_cycle_fn: Callable[[], None],
    emit_heartbeat_fn: Callable[[], None],
) -> tuple[int, bool]:
    """Execute one orchestration tick - extracted from Orchestrator per move map.

    Returns:
        Tuple of (updated_loop_iteration, should_continue)
    """
    tick_start = time.monotonic()
    loop_iteration += 1
    event_context.tick_id = loop_iteration

    # Prune expired inflight IDs
    now = time.monotonic()
    expired = [sid for sid, exp in inflight_stable_ids.items() if exp < now]
    for sid in expired:
        del inflight_stable_ids[sid]
    if expired:
        logger.debug("[INFLIGHT] Pruned %d expired IDs: %s", len(expired), expired)

    logger.info(
        "[LOOP] Iteration %d - active=%d paused=%s pending_reviews=%d pending_reworks=%d pending_triage=%d inflight=%d",
        loop_iteration,
        len(state.active_sessions),
        state.paused,
        len(state.pending_reviews),
        len(state.pending_reworks),
        len(state.pending_triage_reviews),
        len(inflight_stable_ids),
    )

    # Emit tick.started
    events.publish(TraceEvent(
        EventName.TICK_STARTED,
        event_context.enrich({}),
    ))

    if shutdown_requested:
        events.publish(TraceEvent(
            EventName.TICK_COMPLETED,
            event_context.enrich({"idle": True, "reason": "shutdown_requested"}),
        ))
        return loop_iteration, False

    active_start = time.monotonic()
    process_active_sessions_fn()
    active_elapsed = time.monotonic() - active_start
    if active_elapsed > 5:
        logger.warning(
            "[LOOP] Active session processing took %.1fs (active=%d)",
            active_elapsed,
            len(state.active_sessions),
        )

    # Use HealthGate to check if we can proceed with planning
    health_decision = check_health_fn()
    if health_decision.can_proceed:
        plan_start = time.monotonic()
        run_planning_cycle_fn()
        plan_elapsed = time.monotonic() - plan_start
        if plan_elapsed > 5:
            logger.warning("[LOOP] Planning cycle took %.1fs", plan_elapsed)
    else:
        # Emit plan.noop when we skip planning
        events.publish(TraceEvent(
            EventName.PLAN_NOOP,
            event_context.enrich({"reason": health_decision.reason}),
        ))

    emit_heartbeat_fn()

    # Emit tick.completed
    events.publish(TraceEvent(
        EventName.TICK_COMPLETED,
        event_context.enrich({
            "idle": len(state.active_sessions) == 0,
            "active_sessions": len(state.active_sessions),
        }),
    ))
    tick_elapsed = time.monotonic() - tick_start
    if tick_elapsed > 10:
        logger.warning("[LOOP] Tick took %.1fs", tick_elapsed)
    return loop_iteration, True
