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
    from ..models import OrchestratorState, Session, SessionStatus
    from ..config import Config
    from ..orchestrator import Orchestrator
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

from ..events import EventName, EventContext
from ..ports import EventSink, TraceEvent, RepositoryHost
from .reconciliation import ReconciliationRequired, get_pause_label
from ..models import (
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
    """
    from ..observation.observer import SessionObserver
    from .state_machine_manager import StateMachineManager
    from ..control.planner import Scheduler

    # Initialize components with injected or default values
    if orch.planner:
        orch.scheduler = orch.planner.scheduler
    else:
        from .planner import Planner as P
        orch.scheduler = Scheduler(orch.config, dependency_evaluator=dep_eval)
        orch.planner = P(orch.config, orch.scheduler, dep_eval)

    if not orch.session_manager:
        from .session_manager import SessionManager as S
        orch.session_manager = S(orch.runner, orch.events, orch.config)

    if not orch.action_applier:
        from .action_applier import ActionApplier as A
        orch.action_applier = A(
            orch.repository_host, orch.session_manager, orch.events,
            orch.repository_host, orch.worktree_manager, orch.repository_host,
            True, orch._session_launcher_callback
        )
    else:
        orch.action_applier.session_launcher = orch._session_launcher_callback

    if not orch.fact_gatherer:
        from .fact_gatherer import FactGatherer as F
        orch.fact_gatherer = F(orch.config, orch.repository_host, orch.events)

    orch.observer = SessionObserver(
        config=orch.config,
        events=orch.events,
        session_runner=orch.runner,
        repository_host=orch._repository_host,
    )

    if not orch.state_machine_manager:
        orch.state_machine_manager = StateMachineManager(orch.config, orch.events)

    orch.issue_machines = orch.state_machine_manager.issue_machines
    orch.session_machines = orch.state_machine_manager.session_machines
    orch.review_machines = orch.state_machine_manager.review_machines
    orch.observer.session_machines = orch.session_machines

    # Initialize helper managers (deferred)
    orch._session_launcher_instance = None
    orch._cleanup_manager_instance = None
    orch._completion_handler_instance = None
    orch._startup_manager_instance = None
    orch._plan_applier_instance = None


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
        return self.config.filter_milestone

    def _immediate_cleanup(self, session: "Session", status: "SessionStatus") -> None:
        from ..models import SessionStatus
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
                        self.repository_host.create_issue_key(a.issue_number),
                        a.pr_number,
                        a.pr_url,
                        a.branch_name,
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
