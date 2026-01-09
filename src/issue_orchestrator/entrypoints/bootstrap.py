"""Composition root for the issue orchestrator.

This module is the ONLY place where dependencies are wired together.
It creates concrete adapters and injects them into the orchestrator.

This is the "app layer" that knows about all concrete implementations
but keeps that knowledge out of the core (orchestrator).

Principle: The orchestrator core imports only Protocols (ports).
           This module imports concrete implementations (adapters).

Principle: "No Nulls in Orchestrator"
           - Bootstrap is the single source of truth for choosing implementations
           - Orchestrator has no Optional deps, no Null defaults
           - Tests explicitly pass fakes/nulls
"""

import logging
from typing import TYPE_CHECKING

from ..infra.config import Config
from ..ports import EventSink, SessionRunner, NullEventSink, NullSessionRunner, IssueTracker
from ..control.orchestrator_deps import OrchestratorDeps
from ..execution import (
    create_plugin_manager,
    PluggyEventSink,
    PluggySessionRunner,
    LifecycleSSEPlugin,
    GitHubAdapter,
    CompositeEventSink,
)
from ..execution.gh_guard import install_gh_guard
from ..events import EventHub, SequencedEventSink
from ..control import (
    Planner,
    Scheduler,
    SessionManager,
    LabelSync,
)
from ..control.action_applier import ActionApplier
from ..control.fact_gatherer import FactGatherer
from ..control.health_gate import HealthGate
from ..adapters.github import GitHubIssueResolver, GitHubCache
from ..execution.verification_service import DefaultVerificationService
from ..ports.verification import VerificationBudget
from ..execution.worktree_adapter import GitWorktreeManager
from ..execution.git_working_copy import GitWorkingCopy
from ..execution.command_runner import LocalCommandRunner
from ..control.dependency_evaluator import DependencyEvaluator
from ..control.workflows import ReviewWorkflow, ReworkWorkflow, TriageWorkflow
from ..infra import gh_audit

if TYPE_CHECKING:
    from ..infra.orchestrator import Orchestrator

logger = logging.getLogger(__name__)


class Dependencies:
    """Container for all injected dependencies.

    This keeps the orchestrator constructor signature clean by bundling
    all dependencies into a single object.
    """

    def __init__(
        self,
        events: EventSink,
        runner: SessionRunner,
        github: GitHubAdapter | None = None,
    ):
        self.events = events
        self.runner = runner
        self.github = github


def build_orchestrator(
    config: Config,
    enable_ipc: bool = True,
    enable_sse: bool = True,
) -> "Orchestrator":
    """Build a fully-wired orchestrator with all dependencies.

    This is the composition root - the only place that knows about
    concrete implementations.

    Args:
        config: Application configuration
        enable_ipc: Whether to enable IPC event broadcasting
        enable_sse: Whether to enable SSE event broadcasting

    Returns:
        Fully configured Orchestrator instance
    """
    # Import here to avoid circular imports
    from ..infra.orchestrator import Orchestrator

    install_gh_guard()

    # Create the pluggy plugin manager (knows about terminal backend)
    pm = create_plugin_manager(
        terminal_plugin=config.terminal_adapter,
        ui_mode=config.ui_mode,
    )

    # Register lifecycle plugins for event broadcasting
    if enable_sse:
        try:
            sse_plugin = LifecycleSSEPlugin()
            pm.register(sse_plugin, name="lifecycle_sse")
            logger.info("SSE lifecycle plugin registered")
        except Exception as e:
            logger.warning("Failed to register SSE plugin: %s", e)

    # Create port adapters
    base_events = PluggyEventSink(pm)
    runner = PluggySessionRunner(pm)

    # Create GitHub adapter if repo is configured
    github = None
    github_cache = None
    verification_service = None
    if config.repo:
        # Create cache with TTL from config
        cache_ttl = float(max(0, getattr(config, "queue_refresh_seconds", 0)))
        github_cache = GitHubCache(default_ttl=cache_ttl)

        # Create verification service with config-based budget
        # The service maintains circuit breaker state across calls
        default_budget = VerificationBudget(
            timeout_seconds=config.gh_write_verify_timeout_seconds,
            max_attempts=20,
            initial_delay_ms=config.gh_write_verify_initial_delay_ms,
            max_delay_ms=config.gh_write_verify_max_delay_ms,
            backoff_factor=config.gh_write_verify_backoff,
            jitter_ms=config.gh_write_verify_jitter_ms,
        )
        verification_service = DefaultVerificationService(default_budget=default_budget)

        github = GitHubAdapter(
            config.repo,
            config=config,
            cache=github_cache,
            verification_service=verification_service,
        )

    event_hub = EventHub() if github else None
    if event_hub:
        events = CompositeEventSink(base_events, event_hub)
    else:
        events = base_events
    events = SequencedEventSink(events)
    gh_audit.set_event_sink(events)
    if github:
        gh_audit.set_rate_limit_fetcher(github.get_rate_limit_snapshot)
    gh_audit.configure(
        enabled=config.gh_audit_enabled,
        include_events=config.gh_audit_events,
        audit_path=config.gh_audit_file,
    )
    gh_audit.configure_rate_limit(
        every_calls=config.gh_rate_limit_every_calls,
        warn_fraction=config.gh_rate_limit_warn_fraction,
        warn_remaining=config.gh_rate_limit_warn_remaining,
    )
    if config.gh_rate_limit_startup:
        gh_audit.check_rate_limit("startup")
    if github:
        _check_github_token_scopes(config, github)

    # Create control plane components
    scheduler = Scheduler(config=config)

    # Create issue resolver for external ID dependencies (M1-010 style)
    issue_resolver = None
    if github and config.repo:
        issue_resolver = GitHubIssueResolver(
            repo=config.repo,
            issue_tracker=github,
            events=events,
        )

    dependency_evaluator = DependencyEvaluator(
        issue_checker=github,
        events=events,
        issue_resolver=issue_resolver,
        repo=config.repo,
        foundation_milestone=config.foundation_milestone,
    ) if github else None
    session_manager = SessionManager(runner=runner, events=events, config=config)
    label_sync = LabelSync(labels=github, events=events, pr_tracker=github) if github else None

    # Create workflow instances
    review_workflow = ReviewWorkflow(config=config, events=events)
    rework_workflow = ReworkWorkflow(config=config, events=events)
    triage_workflow = TriageWorkflow(config=config, events=events)

    # Create the planner with all dependencies
    planner = Planner(
        config=config,
        scheduler=scheduler,
        dependency_evaluator=dependency_evaluator,
        review_workflow=review_workflow,
        rework_workflow=rework_workflow,
        triage_workflow=triage_workflow,
    )

    # Create adapters for IO operations
    worktree_manager = GitWorktreeManager()
    working_copy = GitWorkingCopy()
    command_runner = LocalCommandRunner()

    # Create FreshIssueReader (cache-bypassing reads)
    from ..adapters.github.fresh_issue_reader import GitHubFreshIssueReader
    fresh_issue_reader = GitHubFreshIssueReader(repo=config.repo, config=config) if github else None

    # Create ActionApplier (IO boundary)
    action_applier = ActionApplier(
        labels=github,
        sessions=session_manager,
        events=events,
        repository_host=github,
        worktree_manager=worktree_manager,
        fresh_issue_reader=fresh_issue_reader,
        reconcile=True,
    ) if github else None

    # Create FactGatherer (read-only snapshot creation)
    fact_gatherer = FactGatherer(
        config=config,
        repository_host=github,
        events=events,
    ) if github else None

    # Create PRScanner (for orphaned review/rework discovery)
    from ..control.pr_scanner import PRScanner
    pr_scanner = PRScanner(
        config=config,
        repository=github,
        events=events,
    ) if github else None

    # Create SessionRestorer (for session recovery after restart)
    from ..control.session_restorer import SessionRestorer
    session_restorer = SessionRestorer(
        config=config,
        repository_host=github,
        working_copy=working_copy,
    ) if github else None

    # Create StateMachineManager (centralized state machine management)
    from ..control.state_machine_manager import StateMachineManager
    state_machine_manager = StateMachineManager(
        config=config,
        events=events,
    )

    # Create CompletionProcessor (processes session completion files)
    from ..control.completion_processor import CompletionProcessor
    completion_processor = CompletionProcessor(
        label_adapter=github,
        pr_adapter=github,
        git_adapter=working_copy,
        event_bus=None,  # EventBus removed
        label_config={
            "blocked": config.get_label_blocked(),
            "needs_human": config.get_label_needs_human(),
            "code_reviewed": config.code_reviewed_label or "code-reviewed",
            "needs_rework": config.get_label_needs_rework(),
            "code_review": config.code_review_label or "needs-code-review",
            "in_progress": config.get_label_in_progress(),
        },
    ) if github else None

    # Create SessionController (decides session outcomes)
    from ..control.session_controller import SessionController
    session_controller_instance = SessionController(
        completion_processor=completion_processor,
        events=events,
    ) if completion_processor else None

    # Build the orchestrator with injected dependencies
    from ..execution.hook_verifier import ExecutionHookVerifier
    hook_verifier = ExecutionHookVerifier(config)

    # Create HealthGate for system health checks (capacity, rate limits, etc.)
    health_gate = HealthGate(
        max_concurrent_sessions=config.max_concurrent_sessions,
        rate_limit_threshold=getattr(config, "rate_limit_warn_remaining", 100),
    )

    # Validate all dependencies are present (required for OrchestratorDeps)
    if github is None:
        raise ValueError("GitHubAdapter (repository_host) is required")
    if event_hub is None:
        raise ValueError("EventHub is required")
    if planner is None:
        raise ValueError("Planner is required")
    if session_manager is None:
        raise ValueError("SessionManager is required")
    if label_sync is None:
        raise ValueError("LabelSync is required")
    if action_applier is None:
        raise ValueError("ActionApplier is required")
    if fact_gatherer is None:
        raise ValueError("FactGatherer is required")
    if pr_scanner is None:
        raise ValueError("PRScanner is required")
    if session_restorer is None:
        raise ValueError("SessionRestorer is required")
    if completion_processor is None:
        raise ValueError("CompletionProcessor is required")
    if session_controller_instance is None:
        raise ValueError("SessionController is required")
    if fresh_issue_reader is None:
        raise ValueError("FreshIssueReader is required")

    # Bundle all dependencies into OrchestratorDeps (no nulls, no optionals)
    deps = OrchestratorDeps(
        events=events,
        runner=runner,
        repository_host=github,
        fresh_issue_reader=fresh_issue_reader,
        event_hub=event_hub,
        planner=planner,
        session_manager=session_manager,
        label_sync=label_sync,
        action_applier=action_applier,
        fact_gatherer=fact_gatherer,
        pr_scanner=pr_scanner,
        session_restorer=session_restorer,
        worktree_manager=worktree_manager,
        working_copy=working_copy,
        hook_verifier=hook_verifier,
        command_runner=command_runner,
        state_machine_manager=state_machine_manager,
        completion_processor=completion_processor,
        session_controller=session_controller_instance,
        health_gate=health_gate,
    )

    return Orchestrator(config=config, deps=deps)


def _check_github_token_scopes(config: Config, github: GitHubAdapter) -> None:
    required = {scope.strip() for scope in (config.github_required_scopes or []) if scope.strip()}
    allowed = {scope.strip() for scope in (config.github_allowed_scopes or []) if scope.strip()}
    try:
        scopes = set(github.get_token_scopes())
    except Exception as exc:
        logger.warning("Failed to fetch GitHub token scopes: %s", exc)
        return

    if required and not required.issubset(scopes):
        missing = sorted(required - scopes)
        raise ValueError(f"GitHub token missing required scopes: {missing}")

    if allowed and not scopes.issubset(allowed):
        extra = sorted(scopes - allowed)
        raise ValueError(f"GitHub token has disallowed scopes: {extra}")

    if scopes:
        logger.info("GitHub token scopes: %s", ", ".join(sorted(scopes)))
    else:
        logger.info("GitHub token scopes unavailable (fine-grained token or missing header)")


def build_orchestrator_for_testing(
    config: Config,
    github: GitHubAdapter,  # Required - no more hiding None
    events: EventSink | None = None,
    runner: SessionRunner | None = None,
    planner: Planner | None = None,
    session_manager: SessionManager | None = None,
    action_applier: ActionApplier | None = None,
    fact_gatherer: FactGatherer | None = None,
) -> "Orchestrator":
    """Build an orchestrator for testing with mock dependencies.

    IMPORTANT: github (RepositoryHost) is now REQUIRED. Tests must provide
    a mock/fake GitHub adapter. This follows the "no nulls" principle -
    tests explicitly provide their fakes rather than relying on defaults.

    Args:
        config: Application configuration
        github: Mock GitHubAdapter (required - tests must provide)
        events: Mock EventSink (defaults to NullEventSink - explicit null)
        runner: Mock SessionRunner (defaults to NullSessionRunner - explicit null)
        planner: Mock Planner (defaults to creating one with no dependencies)
        session_manager: Mock SessionManager (defaults to creating one)
        action_applier: Mock ActionApplier (defaults to creating one from github)
        fact_gatherer: Mock FactGatherer (defaults to creating one from github)

    Returns:
        Orchestrator configured with test dependencies
    """
    from ..infra.orchestrator import Orchestrator

    install_gh_guard()

    # Tests must explicitly pass NullEventSink/NullSessionRunner if they don't care
    # We provide sensible defaults but tests should be explicit
    events = events or NullEventSink()
    runner = runner or NullSessionRunner()
    events = SequencedEventSink(events)

    # Create default planner if not provided
    if planner is None:
        scheduler = Scheduler(config=config)
        planner = Planner(
            config=config,
            scheduler=scheduler,
        )

    # Create default session manager if not provided
    if session_manager is None:
        session_manager = SessionManager(runner=runner, events=events, config=config)

    # Create adapters for IO operations
    worktree_manager = GitWorktreeManager()
    working_copy = GitWorkingCopy()
    command_runner = LocalCommandRunner()

    class _TestFreshIssueReader:
        """Fallback FreshIssueReader for tests without network dependencies."""

        def __init__(self, issue_tracker: IssueTracker) -> None:
            self._issue_tracker = issue_tracker

        def read_issue_labels(self, issue_number: int) -> list[str]:
            return self._issue_tracker.get_issue_labels(issue_number)

    fresh_issue_reader = _TestFreshIssueReader(github)

    # Create default action applier
    if action_applier is None:
        action_applier = ActionApplier(
            labels=github,
            sessions=session_manager,
            events=events,
            repository_host=github,
            worktree_manager=worktree_manager,
            fresh_issue_reader=fresh_issue_reader,
            reconcile=False,  # Disable for testing by default
        )

    # Create default fact gatherer
    if fact_gatherer is None:
        fact_gatherer = FactGatherer(
            config=config,
            repository_host=github,
            events=events,
        )

    from ..execution.hook_verifier import ExecutionHookVerifier
    hook_verifier = ExecutionHookVerifier(config)

    # Create HealthGate for testing
    health_gate = HealthGate(
        max_concurrent_sessions=config.max_concurrent_sessions,
        rate_limit_threshold=100,
    )

    # Create PRScanner for testing
    from ..control.pr_scanner import PRScanner
    pr_scanner = PRScanner(
        config=config,
        repository=github,
        events=events,
    )

    # Create SessionRestorer for testing
    from ..control.session_restorer import SessionRestorer
    session_restorer = SessionRestorer(
        config=config,
        repository_host=github,
        working_copy=working_copy,
    )

    # Create StateMachineManager for testing
    from ..control.state_machine_manager import StateMachineManager
    state_machine_manager = StateMachineManager(
        config=config,
        events=events,
    )

    # Create CompletionProcessor for testing
    from ..control.completion_processor import CompletionProcessor
    completion_processor = CompletionProcessor(
        label_adapter=github,
        pr_adapter=github,
        git_adapter=working_copy,
        event_bus=None,
        label_config={
            "blocked": config.get_label_blocked(),
            "needs_human": config.get_label_needs_human(),
            "code_reviewed": config.code_reviewed_label or "code-reviewed",
            "needs_rework": config.get_label_needs_rework(),
            "code_review": config.code_review_label or "needs-code-review",
            "in_progress": config.get_label_in_progress(),
        },
    )

    # Create SessionController for testing
    from ..control.session_controller import SessionController
    session_controller = SessionController(
        completion_processor=completion_processor,
        events=events,
    )

    # Create LabelSync for testing
    label_sync = LabelSync(labels=github, events=events, pr_tracker=github)

    # Create EventHub for testing
    event_hub = EventHub()

    # Bundle all dependencies into OrchestratorDeps (no nulls, no optionals)
    deps = OrchestratorDeps(
        events=events,
        runner=runner,
        repository_host=github,
        fresh_issue_reader=fresh_issue_reader,
        event_hub=event_hub,
        planner=planner,
        session_manager=session_manager,
        label_sync=label_sync,
        action_applier=action_applier,
        fact_gatherer=fact_gatherer,
        pr_scanner=pr_scanner,
        session_restorer=session_restorer,
        worktree_manager=worktree_manager,
        working_copy=working_copy,
        hook_verifier=hook_verifier,
        command_runner=command_runner,
        state_machine_manager=state_machine_manager,
        completion_processor=completion_processor,
        session_controller=session_controller,
        health_gate=health_gate,
    )

    return Orchestrator(config=config, deps=deps)
