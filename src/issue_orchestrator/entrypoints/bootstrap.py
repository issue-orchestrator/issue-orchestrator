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
import os
from typing import TYPE_CHECKING

from ..infra.config import Config
from ..infra.env import ENV_PREFIX
from ..adapters.github.repo import get_repo_from_git, GitRepoError
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
from ..adapters.github.claim_adapter import GitHubClaimAdapter
from ..execution.verification_service import DefaultVerificationService
from ..ports.verification import VerificationBudget
from ..execution.worktree_adapter import GitWorktreeManager
from ..execution.git_working_copy import GitWorkingCopy
from ..execution.command_runner import LocalCommandRunner
from ..execution.session_output_adapter import FileSystemSessionOutput
from ..control.dependency_evaluator import DependencyEvaluator
from ..control.workflows import ReviewWorkflow, ReworkWorkflow, TriageWorkflow
from ..control.claim_gate import ClaimGate
from ..control.lease_renewer import LeaseRenewer
from ..infra import gh_audit
from ..ports.claim_manager import NullClaimManager
from ..domain.lease_config import LeaseConfig

if TYPE_CHECKING:
    from ..infra.orchestrator import Orchestrator
    from ..control.pr_scanner import PRScanner
    from ..control.session_restorer import SessionRestorer
    from ..control.completion_processor import CompletionProcessor
    from ..control.completion_observer import CompletionObserver
    from ..control.publish_executor import PublishJobExecutor
    from ..control.session_controller import SessionController
    from ..adapters.github.fresh_issue_reader import GitHubFreshIssueReader

logger = logging.getLogger(__name__)


def _resolve_repo(config: Config) -> str | None:
    """Resolve repo name from config or auto-detect from git remote."""
    repo = config.repo
    if not repo:
        try:
            repo = get_repo_from_git()
            logger.info("Auto-detected repository from git remote: %s", repo)
            config.repo = repo
        except GitRepoError as e:
            logger.warning("Could not auto-detect repository: %s", e)
            repo = None
    return repo


def _create_github_adapter(repo: str, config: Config) -> GitHubAdapter:
    """Create GitHub adapter with cache and verification service."""
    cache_ttl = float(max(0, getattr(config, "queue_refresh_seconds", 0)))
    github_cache = GitHubCache(default_ttl=cache_ttl)

    default_budget = VerificationBudget(
        timeout_seconds=config.gh_write_verify_timeout_seconds,
        max_attempts=20,
        initial_delay_ms=config.gh_write_verify_initial_delay_ms,
        max_delay_ms=config.gh_write_verify_max_delay_ms,
        backoff_factor=config.gh_write_verify_backoff,
        jitter_ms=config.gh_write_verify_jitter_ms,
    )
    verification_service = DefaultVerificationService(default_budget=default_budget)

    return GitHubAdapter(
        repo,
        config=config,
        cache=github_cache,
        verification_service=verification_service,
    )


def _setup_event_sinks(
    base_events: PluggyEventSink,
    github: GitHubAdapter | None,
) -> tuple[EventSink, EventHub | None]:
    """Set up event sinks and event hub."""
    event_hub = EventHub() if github else None
    if event_hub:
        events = CompositeEventSink(base_events, event_hub)
    else:
        events = base_events
    events = SequencedEventSink(events)
    return events, event_hub


def _configure_gh_audit(
    config: Config,
    events: EventSink,
    github: GitHubAdapter | None,
) -> None:
    """Configure GitHub audit logging."""
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


def _create_claim_components(
    config: Config,
    github: GitHubAdapter | None,
    events: EventSink,
) -> tuple[ClaimGate, LeaseRenewer, LeaseConfig, NullClaimManager | GitHubClaimAdapter]:
    """Create claim management components."""
    if github and config.claims.enabled:
        lease_config = LeaseConfig(
            lease_seconds=config.claims.lease_seconds,
            renew_interval_seconds=config.claims.renew_before_expiry_seconds,
            convergence_timeout_seconds=config.claims.convergence_timeout_seconds,
            convergence_poll_min_ms=config.claims.convergence_poll_min_ms,
            convergence_poll_max_ms=config.claims.convergence_poll_max_ms,
            convergence_required_wins=config.claims.convergence_required_wins,
        )
        claimant_id = config.claims.claimant_id or f"orchestrator-{os.getpid()}"
        claim_manager = GitHubClaimAdapter(
            client=github.http_client,
            claimant_id=claimant_id,
            config=lease_config,
            events=events,
            label_adapter=github,
        )
        logger.info("Claims enabled: claimant_id=%s, lease=%ds", claimant_id, lease_config.lease_seconds)
    else:
        lease_config = LeaseConfig()
        claim_manager = NullClaimManager()

    claim_gate = ClaimGate(claim_manager=claim_manager, events=events)
    lease_renewer = LeaseRenewer(
        claim_manager=claim_manager,
        events=events,
        config=lease_config,
    )
    return claim_gate, lease_renewer, lease_config, claim_manager


def _create_planner(
    config: Config,
    github: GitHubAdapter | None,
    events: EventSink,
) -> tuple[Planner, Scheduler, DependencyEvaluator | None, LabelSync | None]:
    """Create planner and supporting control plane components."""
    scheduler = Scheduler(config=config)

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

    label_sync = LabelSync(labels=github, events=events, pr_tracker=github) if github else None

    review_workflow = ReviewWorkflow(config=config, events=events)
    rework_workflow = ReworkWorkflow(config=config, events=events)
    triage_workflow = TriageWorkflow(config=config, events=events)

    planner = Planner(
        config=config,
        scheduler=scheduler,
        dependency_evaluator=dependency_evaluator,
        review_workflow=review_workflow,
        rework_workflow=rework_workflow,
        triage_workflow=triage_workflow,
    )
    return planner, scheduler, dependency_evaluator, label_sync


def _create_io_adapters() -> tuple[
    GitWorktreeManager,
    GitWorkingCopy,
    LocalCommandRunner,
    FileSystemSessionOutput,
]:
    """Create IO adapter instances."""
    return (
        GitWorktreeManager(),
        GitWorkingCopy(),
        LocalCommandRunner(),
        FileSystemSessionOutput(),
    )


def _create_completion_components(
    config: Config,
    github: GitHubAdapter | None,
    events: EventSink,
    working_copy: GitWorkingCopy,
    session_output: FileSystemSessionOutput,
    command_runner: LocalCommandRunner,
) -> tuple["CompletionProcessor | None", "SessionController | None"]:
    """Create completion processor and session controller."""
    from ..control.completion_processor import CompletionProcessor
    from ..control.session_controller import SessionController

    completion_processor = CompletionProcessor(
        label_adapter=github,
        pr_adapter=github,
        git_adapter=working_copy,
        session_output=session_output,
        event_bus=None,
        label_config={
            "blocked": config.get_label_blocked(),
            "needs_human": config.get_label_needs_human(),
            "code_reviewed": config.code_reviewed_label or "code-reviewed",
            "needs_rework": config.get_label_needs_rework(),
            "code_review": config.code_review_label or "needs-code-review",
            "in_progress": config.get_label_in_progress(),
        },
        config=config,
    ) if github else None

    session_controller_instance = SessionController(
        completion_processor=completion_processor,
        events=events,
        session_output=session_output,
        working_copy=working_copy,
        command_runner=command_runner if config.validation and config.validation.cmd else None,
        validation_cmd=config.validation.cmd if config.validation else None,
        validation_timeout_seconds=config.validation.timeout_seconds if config.validation else 300,
    ) if completion_processor else None

    return completion_processor, session_controller_instance


def _create_async_completion_components(
    completion_processor: "CompletionProcessor",
    events: EventSink,
    session_output: FileSystemSessionOutput,
    command_runner: LocalCommandRunner,
    config: Config,
    enable_persistence: bool = True,
) -> tuple["CompletionObserver", "PublishJobExecutor"]:
    """Create async completion processing components.

    These components enable non-blocking completion handling:
    - CompletionObserver: Fast observation of completions (no I/O)
    - PublishJobExecutor: Background execution of publish jobs
    - JobStore: SQLite persistence for crash recovery (optional)

    Args:
        completion_processor: For executing publish actions
        events: Event sink for job lifecycle events
        session_output: For reading session logs
        command_runner: For running validation commands
        config: Application configuration
        enable_persistence: Whether to enable SQLite job persistence

    Returns:
        Tuple of (CompletionObserver, PublishJobExecutor)
    """
    from ..control.completion_observer import CompletionObserver
    from ..control.publish_executor import PublishJobExecutor, ExecutorConfig
    from ..control.job_store import JobStore, get_default_db_path

    completion_observer = CompletionObserver(session_output=session_output)

    executor_config = ExecutorConfig(
        max_workers=2,  # Max concurrent publish jobs
        job_timeout_seconds=600,  # 10 minutes max per job
        enable_validation=config.validation.cmd is not None if config.validation else False,
        validation_cmd=config.validation.cmd if config.validation else None,
        validation_timeout_seconds=config.validation.timeout_seconds if config.validation else 300,
    )

    # Create job store for persistence (crash recovery)
    job_store = None
    if enable_persistence:
        db_path = get_default_db_path(config.repo_root)
        job_store = JobStore(db_path)
        logger.info("[BOOTSTRAP] Job store enabled: %s", db_path)

    publish_executor = PublishJobExecutor(
        completion_processor=completion_processor,
        events=events,
        config=executor_config,
        command_runner=command_runner if config.validation and config.validation.cmd else None,
        job_store=job_store,
    )

    return completion_observer, publish_executor


def _validate_required_deps(
    github: GitHubAdapter | None,
    event_hub: EventHub | None,
    planner: Planner | None,
    session_manager: SessionManager | None,
    label_sync: LabelSync | None,
    action_applier: ActionApplier | None,
    fact_gatherer: FactGatherer | None,
    pr_scanner: "PRScanner | None",
    session_restorer: "SessionRestorer | None",
    completion_processor: "CompletionProcessor | None",
    session_controller_instance: "SessionController | None",
    fresh_issue_reader: "GitHubFreshIssueReader | None",
) -> None:
    """Validate all required dependencies are present."""
    # GitHub requires special error message
    if github is None:
        raise ValueError(
            "Could not determine GitHub repository.\n\n"
            "Either:\n"
            "  1. Set 'repo.name' in your config file:\n"
            "       repo:\n"
            "         name: owner/repo-name\n\n"
            "  2. Or ensure you're running from a git repo with a GitHub remote:\n"
            "       git remote get-url origin\n"
            "       # Should show: https://github.com/owner/repo.git"
        )
    # Check all other required deps with a data-driven approach
    deps_to_check = [
        (event_hub, "EventHub"),
        (planner, "Planner"),
        (session_manager, "SessionManager"),
        (label_sync, "LabelSync"),
        (action_applier, "ActionApplier"),
        (fact_gatherer, "FactGatherer"),
        (pr_scanner, "PRScanner"),
        (session_restorer, "SessionRestorer"),
        (completion_processor, "CompletionProcessor"),
        (session_controller_instance, "SessionController"),
        (fresh_issue_reader, "FreshIssueReader"),
    ]
    for dep, name in deps_to_check:
        if dep is None:
            raise ValueError(f"{name} is required")


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
    from ..infra.orchestrator import Orchestrator
    from ..control.pr_scanner import PRScanner
    from ..control.session_restorer import SessionRestorer
    from ..control.state_machine_manager import StateMachineManager
    from ..adapters.github.fresh_issue_reader import GitHubFreshIssueReader
    from ..execution.hook_verifier import ExecutionHookVerifier
    from ..execution.triage_downloader import TriageDownloader

    install_gh_guard()

    # Make repo root visible to terminal plugins.
    os.environ[f"{ENV_PREFIX}REPO_ROOT"] = str(config.repo_root)

    # Create the pluggy plugin manager and register SSE plugin
    pm = create_plugin_manager(
        terminal_plugin=config.terminal_adapter,
        ui_mode=config.ui_mode,
    )
    if enable_sse:
        try:
            pm.register(LifecycleSSEPlugin(), name="lifecycle_sse")
            logger.info("SSE lifecycle plugin registered")
        except Exception as e:
            logger.warning("Failed to register SSE plugin: %s", e)

    # Create port adapters
    base_events = PluggyEventSink(pm)
    runner = PluggySessionRunner(pm)

    # Resolve repo and create GitHub adapter
    repo = _resolve_repo(config)
    github = _create_github_adapter(repo, config) if repo else None

    # Set up event sinks
    events, event_hub = _setup_event_sinks(base_events, github)

    # Configure GitHub audit logging
    _configure_gh_audit(config, events, github)
    if github:
        _check_github_token_scopes(config, github)

    # Create claim management components
    claim_gate, lease_renewer, _lease_config, claim_manager = _create_claim_components(
        config, github, events
    )

    # Create planner and control plane components
    planner, _scheduler, _dependency_evaluator, label_sync = _create_planner(config, github, events)
    session_manager = SessionManager(runner=runner, events=events, config=config)

    # Create IO adapters
    worktree_manager, working_copy, command_runner, session_output = _create_io_adapters()

    # Create manifest downloader for triage sessions
    manifest_downloader = TriageDownloader(
        repository_host=github,
        command_runner=command_runner,
    ) if github else None

    # Create cache-bypassing reader
    fresh_issue_reader = GitHubFreshIssueReader(repo=config.repo, config=config) if github else None

    # Create action applier (IO boundary)
    action_applier = ActionApplier(
        labels=github,
        sessions=session_manager,
        events=events,
        repository_host=github,
        worktree_manager=worktree_manager,
        fresh_issue_reader=fresh_issue_reader,
        reconcile=True,
    ) if github else None

    # Create fact gatherer (read-only snapshot creation)
    fact_gatherer = FactGatherer(
        config=config,
        repository_host=github,
        events=events,
    ) if github else None

    # Create PR scanner and session restorer
    pr_scanner = PRScanner(config=config, repository=github, events=events) if github else None
    session_restorer = SessionRestorer(
        config=config, repository_host=github, working_copy=working_copy
    ) if github else None

    # Create state machine manager
    state_machine_manager = StateMachineManager(config=config, events=events)

    # Create completion components
    completion_processor, session_controller_instance = _create_completion_components(
        config, github, events, working_copy, session_output, command_runner
    )

    # Create async completion components (observer + executor)
    completion_observer, publish_executor = _create_async_completion_components(
        completion_processor, events, session_output, command_runner, config
    ) if completion_processor else (None, None)

    # Create hook verifier and health gate
    hook_verifier = ExecutionHookVerifier(config)
    health_gate = HealthGate(
        max_concurrent_sessions=config.max_concurrent_sessions,
        rate_limit_threshold=getattr(config, "rate_limit_warn_remaining", 100),
    )

    # Validate all dependencies are present
    _validate_required_deps(
        github, event_hub, planner, session_manager, label_sync,
        action_applier, fact_gatherer, pr_scanner, session_restorer,
        completion_processor, session_controller_instance, fresh_issue_reader,
    )

    # Type assertions after validation (validation raises if any are None)
    assert github is not None
    assert event_hub is not None
    assert planner is not None
    assert session_manager is not None
    assert label_sync is not None
    assert action_applier is not None
    assert fact_gatherer is not None
    assert pr_scanner is not None
    assert session_restorer is not None
    assert completion_processor is not None
    assert session_controller_instance is not None
    assert fresh_issue_reader is not None
    assert completion_observer is not None
    assert publish_executor is not None
    assert manifest_downloader is not None

    # Wire up worktree removal callback for async completion job tracking
    # When a worktree is removed, mark associated jobs as WORKTREE_GONE
    action_applier.on_worktree_removed = publish_executor.mark_worktree_cleaned

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
        session_output=session_output,
        manifest_downloader=manifest_downloader,
        state_machine_manager=state_machine_manager,
        completion_processor=completion_processor,
        session_controller=session_controller_instance,
        health_gate=health_gate,
        claim_manager=claim_manager,
        claim_gate=claim_gate,
        lease_renewer=lease_renewer,
        completion_observer=completion_observer,
        publish_executor=publish_executor,
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
    session_output = FileSystemSessionOutput()

    from ..execution.triage_downloader import TriageDownloader
    manifest_downloader = TriageDownloader(
        repository_host=github,
        command_runner=command_runner,
    )

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
        session_output=session_output,
        event_bus=None,
        label_config={
            "blocked": config.get_label_blocked(),
            "needs_human": config.get_label_needs_human(),
            "code_reviewed": config.code_reviewed_label or "code-reviewed",
            "needs_rework": config.get_label_needs_rework(),
            "code_review": config.code_review_label or "needs-code-review",
            "in_progress": config.get_label_in_progress(),
        },
        config=config,
    )

    # Create SessionController for testing (with optional validation gate)
    from ..control.session_controller import SessionController
    session_controller = SessionController(
        completion_processor=completion_processor,
        events=events,
        session_output=session_output,
        working_copy=working_copy,
        command_runner=command_runner if config.validation and config.validation.cmd else None,
        validation_cmd=config.validation.cmd if config.validation else None,
        validation_timeout_seconds=config.validation.timeout_seconds if config.validation else 300,
    )

    # Create LabelSync for testing
    label_sync = LabelSync(labels=github, events=events, pr_tracker=github)

    # Create EventHub for testing
    event_hub = EventHub()

    # Create claim components for testing (always use NullClaimManager)
    lease_config = LeaseConfig()
    claim_manager = NullClaimManager()
    claim_gate = ClaimGate(claim_manager=claim_manager, events=events)
    lease_renewer = LeaseRenewer(
        claim_manager=claim_manager,
        events=events,
        config=lease_config,
    )

    # Create async completion components for testing (persistence disabled by default)
    completion_observer, publish_executor = _create_async_completion_components(
        completion_processor, events, session_output, command_runner, config,
        enable_persistence=False,  # Disable SQLite in tests to avoid file I/O
    )

    # Wire up worktree removal callback for async completion job tracking
    action_applier.on_worktree_removed = publish_executor.mark_worktree_cleaned

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
        session_output=session_output,
        manifest_downloader=manifest_downloader,
        state_machine_manager=state_machine_manager,
        completion_processor=completion_processor,
        session_controller=session_controller,
        health_gate=health_gate,
        claim_manager=claim_manager,
        claim_gate=claim_gate,
        lease_renewer=lease_renewer,
        completion_observer=completion_observer,
        publish_executor=publish_executor,
    )

    return Orchestrator(config=config, deps=deps)
