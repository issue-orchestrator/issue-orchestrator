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
from .actions import AddLabelAction
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
    orch.deps.action_applier.session_launcher = orch.session_launcher_callback

    # Create observer (still created here as it depends on runtime orchestrator state)
    orch.observer = SessionObserver(
        config=orch.config,
        events=orch.deps.events,
        session_runner=orch.deps.runner,
        repository_host=orch.deps.repository_host,
        fresh_issue_reader=orch.deps.fresh_issue_reader,
        session_output=orch.deps.session_output,
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

        self.events.publish(TraceEvent(EventName.APPLY_STARTED, self.event_context.enrich({"steps": plan.action_count})))

        applied_count = 0
        failed_count = 0

        for action in plan.actions:
            if self.state.paused:
                break

            result_info = self._apply_single_action(action, pause_issue_callback)
            if result_info.success:
                applied_count += 1
            else:
                failed_count += 1

            if result_info.halt:
                break

        self.events.publish(TraceEvent(EventName.APPLY_COMPLETED, self.event_context.enrich({"applied_steps": applied_count, "failed_steps": failed_count})))

    @dataclass
    class _ActionApplyResult:
        """Result of applying a single action."""
        success: bool = False
        halt: bool = False  # Stop processing remaining actions

    def _apply_single_action(self, action: "Action", pause_issue_callback: Callable[[int, str], None]) -> "_ActionApplyResult":
        """Apply a single action and return the result."""
        from .actions import ActionType

        # Check triage cooldown
        if action.action_type == ActionType.CREATE_TRIAGE_ISSUE and self._cleanup_manager:
            if not self._cleanup_manager.should_retry_triage_issue():
                logger.warning("[PLAN] Skipping triage issue creation due to cooldown")
                self._emit_apply_failed(action, "triage_issue_creation_cooldown")
                return self._ActionApplyResult(success=False)

        try:
            result = self._aa.apply(action)
            if result.success:
                return self._handle_action_success(action, result)
            return self._handle_action_failure(action, result)
        except ReconciliationRequired as rr:
            return self._handle_reconciliation_error(rr, pause_issue_callback)
        except Exception as e:
            logger.exception("Failed to apply action %s: %s", action, e)
            self.events.publish(TraceEvent(EventName.APPLY_FAILED, self.event_context.enrich({"step_type": action.action_type.value, "error": str(e)})))
            return self._ActionApplyResult(success=False)

    def _handle_action_success(self, action: "Action", result: "ActionResult") -> "_ActionApplyResult":
        """Handle successful action application."""
        self._update_state_after_action(action, result)
        self.events.publish(TraceEvent(
            EventName.APPLY_STEP_APPLIED,
            self.event_context.enrich({"step_type": action.action_type.value, "issue_number": self._get_action_issue_number(action), "result": "success"}),
        ))
        return self._ActionApplyResult(success=True)

    def _handle_action_failure(self, action: "Action", result: "ActionResult") -> "_ActionApplyResult":
        """Handle failed action application."""
        from .actions import ActionType

        logger.warning("[PLAN] Action %s failed: %s", action.action_type.value, result.error)

        # Mark issue as failed if applicable
        failed_actions_mark_issue = {ActionType.ADD_LABEL, ActionType.REMOVE_LABEL, ActionType.SYNC_LABELS, ActionType.ADD_COMMENT}
        if action.action_type in failed_actions_mark_issue:
            issue_number = self._resolve_action_issue_number(action)
            if issue_number is not None:
                self.state.failed_this_cycle.add(issue_number)
                logger.info("[PLAN] Marked issue #%d failed_this_cycle due to %s failure", issue_number, action.action_type.value)

        # Handle triage issue failure cooldown
        if action.action_type.value == "create_triage_issue" and self._cleanup_manager:
            try:
                self._cleanup_manager.mark_triage_issue_failure()
            except Exception:
                pass

        self._emit_apply_failed(action, result.error or "unknown")
        return self._ActionApplyResult(success=False)

    def _handle_reconciliation_error(self, rr: ReconciliationRequired, pause_issue_callback: Callable[[int, str], None]) -> "_ActionApplyResult":
        """Handle reconciliation required error."""
        issue_number = rr.entity_id
        logger.warning("[RECONCILIATION] Drift detected for %s #%d: %s", rr.entity_type, issue_number, rr.reason)
        self.events.publish(TraceEvent(
            EventName.RECONCILIATION_REQUIRED,
            self.event_context.enrich({
                "issue_number": issue_number, "entity_type": rr.entity_type, "reason": rr.reason,
                "expected_labels": list(rr.expected.labels), "actual_labels": list(rr.actual.labels),
            }),
        ))
        pause_issue_callback(issue_number, rr.reason)
        return self._ActionApplyResult(success=False, halt=True)

    def _emit_apply_failed(self, action: "Action", error: str) -> None:
        """Emit APPLY_FAILED event."""
        self.events.publish(TraceEvent(
            EventName.APPLY_FAILED,
            self.event_context.enrich({"step_type": action.action_type.value, "issue_number": self._get_action_issue_number(action), "error": error}),
        ))

    def _get_action_issue_number(self, action: "Action") -> int | None:
        """Get issue number from action for events."""
        return getattr(action, "number", getattr(action, "issue_number", None))

    def _resolve_action_issue_number(self, action: "Action") -> int | None:
        """Resolve issue number from action, excluding PRs."""
        issue_number = getattr(action, "issue_number", None)
        if issue_number is not None:
            return issue_number
        number = getattr(action, "number", None)
        if number is None or getattr(action, "is_pr", False):
            return None
        return number

    def _update_state_after_action(self, action: "Action", result: "ActionResult") -> None:
        from .actions import ActionType

        handlers = {
            ActionType.LAUNCH_SESSION: self._handle_launch_session,
            ActionType.ESCALATE_TO_HUMAN: self._handle_escalate_to_human,
            ActionType.CREATE_TRIAGE_ISSUE: self._handle_create_triage_issue,
            ActionType.CLEANUP_SESSION: self._handle_cleanup_session,
            ActionType.QUEUE_REVIEW: self._handle_queue_review,
            ActionType.QUEUE_REWORK: self._handle_queue_rework,
            ActionType.QUEUE_TRIAGE: self._handle_queue_triage,
        }

        handler = handlers.get(action.action_type)
        if handler:
            handler(action, result)

    def _handle_launch_session(self, action: "Action", result: "ActionResult") -> None:
        from .actions import LaunchSessionAction
        a = cast(LaunchSessionAction, action)
        logger.info("[PLAN] Launched %s session for #%d", a.session_type, a.number)

    def _handle_escalate_to_human(self, action: "Action", result: "ActionResult") -> None:
        from .actions import EscalateToHumanAction
        a = cast(EscalateToHumanAction, action)
        logger.info("[PLAN] Escalated PR #%d (cycle %d)", a.pr_number, a.rework_cycles)

    def _handle_create_triage_issue(self, action: "Action", result: "ActionResult") -> None:
        from .actions import CreateTriageIssueAction
        a = cast(CreateTriageIssueAction, action)
        num = result.details.get("issue_number")
        if num:
            self.state.pending_triage_reviews.append(PendingTriageReview(num, a.title))
            logger.info("Created triage #%d", num)

    def _handle_cleanup_session(self, action: "Action", result: "ActionResult") -> None:
        from .actions import CleanupSessionAction
        a = cast(CleanupSessionAction, action)
        self.state.pending_cleanups = [c for c in self.state.pending_cleanups if c.pr_number != a.pr_number]

    def _handle_queue_review(self, action: "Action", result: "ActionResult") -> None:
        from .actions import QueueReviewAction
        a = cast(QueueReviewAction, action)
        if any(r.pr_number == a.pr_number for r in self.state.pending_reviews):
            return
        self.state.pending_reviews.append(
            PendingReview(
                issue_key=self.repository_host.create_issue_key(a.issue_number),
                pr_number=a.pr_number, pr_url=a.pr_url, branch_name=a.branch_name,
                _issue_number=a.issue_number, agent_label=a.agent_label,
            )
        )
        log_transition("review", a.pr_number, "CREATED", "QUEUED", f"from #{a.issue_number}")
        self.get_review_machine(a.pr_number, a.issue_number)

    def _handle_queue_rework(self, action: "Action", result: "ActionResult") -> None:
        from .actions import QueueReworkAction
        a = cast(QueueReworkAction, action)
        if any(r.resolve_issue_number() == a.issue_number for r in self.state.pending_reworks):
            return
        agent = next((r.agent_type for r in self.state.discovered_reworks if r.issue_number == a.issue_number), "agent:developer")
        self.state.pending_reworks.append(
            PendingRework(self.repository_host.create_issue_key(a.issue_number), agent, a.rework_cycle, issue_number=a.issue_number)
        )
        log_transition("rework", a.issue_number, "CREATED", "QUEUED", f"cycle {a.rework_cycle}")

    def _handle_queue_triage(self, action: "Action", result: "ActionResult") -> None:
        from .actions import QueueTriageAction
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
    action_applier: "ActionApplier",
    event_context: EventContext,
    issue_number: int,
    reason: str,
) -> None:
    """Pause an issue due to reconciliation failure (state drift)."""
    pause_label = get_pause_label()
    try:
        action_applier.apply(AddLabelAction(
            issue_number=issue_number,
            label=pause_label,
            reason="reconciliation drift detected",
        ))
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
    orchestrator.request_shutdown(force=orchestrator.shutdown_requested)


# Keep PlanApplier for backward compatibility during transition
PlanApplier = OrchestratorSupport


def _detect_stale_claims(
    issues: list["Issue"],
    active_sessions: list["Session"],
    claim_manager: object | None,
    events: EventSink,
    event_context: EventContext,
) -> list["Issue"]:
    """Detect issues with stale claims (io:claimed label but no valid claim).

    A claim is considered stale if:
    1. The issue has the io:claimed label
    2. There's no active session for this issue
    3. The claim has expired or doesn't exist

    Args:
        issues: List of issues to check
        active_sessions: Currently active sessions
        claim_manager: ClaimManager for checking claim validity
        events: Event sink for emitting events
        event_context: Event context for enriching events

    Returns:
        List of issues with stale claims
    """
    from ..infra import labels

    if not claim_manager:
        return []

    # Build set of issues with active sessions
    active_issue_numbers = {s.issue.number for s in active_sessions}

    stale_claim_issues: list["Issue"] = []

    for issue in issues:
        # Only check issues with io:claimed label
        if labels.IO_CLAIMED not in issue.labels:
            continue

        # Skip issues with active sessions (claim is valid, session is running)
        if issue.number in active_issue_numbers:
            continue

        # Check if claim is valid via ClaimManager
        if hasattr(claim_manager, 'get_current_claim'):
            claim = claim_manager.get_current_claim(issue.number)
            if claim is None or (hasattr(claim, 'is_expired') and claim.is_expired()):
                # Claim is stale
                stale_claim_issues.append(issue)
                logger.info(
                    "[STALE-CLAIM] Issue #%d has io:claimed label but no valid claim",
                    issue.number,
                )
                events.publish(TraceEvent(
                    EventName.CLAIM_STALE_DETECTED,
                    event_context.enrich({
                        "issue_number": issue.number,
                        "labels": list(issue.labels),
                    }),
                ))

    return stale_claim_issues


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
    claim_manager: object | None = None,
) -> tuple[float, bool]:
    """Run the planning cycle - extracted from Orchestrator per move map Step 2."""
    should_fetch = (time.time() - last_issue_fetch >= config.queue_refresh_seconds) or refresh_requested

    if should_fetch:
        last_issue_fetch, refresh_requested = _fetch_and_update_queue(
            config, events, state, repository_host, scheduler, github_workflow,
            refresh_requested, inflight_stable_ids,
        )

    # Detect stale issues and claims
    stale_issues = _detect_stale_in_progress(observer, state, events, event_context)
    stale_claim_issues = _detect_stale_claims(state.cached_queue_issues, state.active_sessions, claim_manager, events, event_context)

    # Create snapshot and plan
    snapshot = fact_gatherer.create_snapshot(state, state.cached_queue_issues, stale_in_progress_issues=stale_issues, stale_claim_issues=stale_claim_issues)
    _emit_facts_gathered(events, event_context, state, stale_issues)

    plan = planner.plan(snapshot)
    _emit_plan_computed(events, event_context, plan)

    if plan.action_count > 0:
        logger.info("[PLAN] %d action(s): %s", plan.action_count, ", ".join(f"{a.action_type.value}:{getattr(a, 'number', '?')}" for a in plan.actions))

    apply_plan_fn(plan)
    _track_stale_ticks(config, events, event_context, state, stale_issues)
    clear_discovered_facts_fn()

    return last_issue_fetch, refresh_requested


def _fetch_and_update_queue(
    config: "Config",
    events: EventSink,
    state: "OrchestratorState",
    repository_host: RepositoryHost,
    scheduler: object,
    github_workflow: object,
    refresh_requested: bool,
    inflight_stable_ids: dict[str, float],
) -> tuple[float, bool]:
    """Fetch issues and update queue cache."""
    from ..infra import gh_audit

    manual_refresh = refresh_requested
    required_stable_ids = set(inflight_stable_ids.keys()) if inflight_stable_ids else None

    if required_stable_ids:
        logger.info("[FETCH] %s refresh with %d required inflight IDs: %s", "Manual" if manual_refresh else "Scheduled", len(required_stable_ids), sorted(required_stable_ids))
    else:
        logger.info("[FETCH] %s refresh", "Manual" if manual_refresh else "Scheduled")

    reason = gh_audit.AuditReason.QUEUE_REFRESH_MANUAL if manual_refresh else gh_audit.AuditReason.QUEUE_REFRESH_SCHEDULED
    scope = gh_audit.AuditScope.MANUAL if manual_refresh else gh_audit.AuditScope.PERIODIC
    with gh_audit.context(reason=reason, scope=scope):
        all_issues = github_workflow.fetch_all_issues(config.filtering.milestone, required_stable_ids)

    _process_inflight_ids(required_stable_ids, all_issues, inflight_stable_ids)
    _update_label_cache(repository_host, all_issues)

    github_workflow.scan_needs_code_review_prs(state)
    github_workflow.scan_needs_rework_prs(state)
    _, dep_blocked = scheduler.get_available_issues(all_issues)
    github_workflow.update_dependency_problems(state, dep_blocked)

    new_queue = _filter_queue(config, state, all_issues)
    _emit_queue_changes(events, state, new_queue)
    state.cached_queue_issues = new_queue

    if state.failed_this_cycle:
        logger.info("[REFRESH] Clearing failed_this_cycle: %s (labels now synced from GitHub)", state.failed_this_cycle)
        state.failed_this_cycle.clear()

    return time.time(), False


def _process_inflight_ids(required_stable_ids: set[str] | None, all_issues: list["Issue"], inflight_stable_ids: dict[str, float]) -> None:
    """Process and remove discovered inflight IDs."""
    if not required_stable_ids:
        return
    discovered_ids = {i.key.stable_id() for i in all_issues}
    found = required_stable_ids & discovered_ids
    if found:
        for sid in found:
            inflight_stable_ids.pop(sid, None)
        logger.info("[INFLIGHT] Discovered %d/%d required IDs: %s", len(found), len(required_stable_ids), sorted(found))
    still_missing = required_stable_ids - discovered_ids
    if still_missing:
        logger.warning("[INFLIGHT] Still missing %d required IDs after refresh: %s", len(still_missing), sorted(still_missing))


def _update_label_cache(repository_host: RepositoryHost, all_issues: list["Issue"]) -> None:
    """Update label cache for all issues."""
    for issue in all_issues:
        try:
            repository_host.update_label_cache(issue.number, list(issue.labels))
        except Exception:
            logger.debug("Failed to update label cache for issue #%s", issue.number, exc_info=True)


def _filter_queue(config: "Config", state: "OrchestratorState", all_issues: list["Issue"]) -> list["Issue"]:
    """Filter issues for the queue."""
    exclude = {e.issue_number for e in state.session_history} | {s.issue.number for s in state.active_sessions}
    filtered = [i for i in all_issues if i.number not in exclude]
    return [i for i in filtered if i.number == config.filtering.issue] if config.filtering.issue else filtered


def _emit_queue_changes(events: EventSink, state: "OrchestratorState", new_queue: list["Issue"]) -> None:
    """Detect and emit queue changes."""
    old_numbers = {i.number for i in state.cached_queue_issues}
    new_numbers = {i.number for i in new_queue}
    added_numbers = new_numbers - old_numbers
    removed_numbers = old_numbers - new_numbers
    if added_numbers or removed_numbers:
        added = [{"number": i.number, "title": i.title} for i in new_queue if i.number in added_numbers]
        removed = [{"number": n} for n in removed_numbers]
        events.publish(TraceEvent(EventName.QUEUE_CHANGED, {"added": added, "removed": removed, "total": len(new_queue)}))
        logger.info("Queue changed: %d added, %d removed, %d total", len(added), len(removed), len(new_queue))


def _detect_stale_in_progress(observer: object | None, state: "OrchestratorState", events: EventSink, event_context: EventContext) -> list["Issue"]:
    """Detect stale in-progress issues."""
    if not (observer and hasattr(observer, 'detect_stale_in_progress')):
        return []
    stale_issues = observer.detect_stale_in_progress(state.cached_queue_issues, state.active_sessions)
    for issue in stale_issues:
        events.publish(TraceEvent(EventName.STALE_IN_PROGRESS_DETECTED, event_context.enrich({"issue_number": issue.number, "labels": list(issue.labels)})))
    return stale_issues


def _emit_facts_gathered(events: EventSink, event_context: EventContext, state: "OrchestratorState", stale_issues: list["Issue"]) -> None:
    """Emit facts gathered event."""
    events.publish(TraceEvent(
        EventName.FACTS_GATHERED,
        event_context.enrich({
            "issues_count": len(state.cached_queue_issues), "active_sessions": len(state.active_sessions),
            "pending_reviews": len(state.pending_reviews), "pending_reworks": len(state.pending_reworks),
            "stale_in_progress_count": len(stale_issues),
        }),
    ))


def _emit_plan_computed(events: EventSink, event_context: EventContext, plan: "Plan") -> None:
    """Emit plan computed event."""
    events.publish(TraceEvent(EventName.PLAN_COMPUTED, event_context.enrich({"steps": plan.action_count, "actions": [a.action_type.value for a in plan.actions]})))


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
