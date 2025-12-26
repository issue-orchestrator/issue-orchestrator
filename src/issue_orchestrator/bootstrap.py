"""Composition root for the issue orchestrator.

This module is the ONLY place where dependencies are wired together.
It creates concrete adapters and injects them into the orchestrator.

This is the "app layer" that knows about all concrete implementations
but keeps that knowledge out of the core (orchestrator).

Principle: The orchestrator core imports only Protocols (ports).
           This module imports concrete implementations (adapters).
"""

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from .config import Config
from .ports import EventSink, SessionRunner, NullEventSink, NullSessionRunner
from .execution import (
    create_plugin_manager,
    PluggyEventSink,
    PluggySessionRunner,
    LifecycleIPCPlugin,
    LifecycleSSEPlugin,
    LifecycleLoggingPlugin,
    GitHubAdapter,
)
from .control import (
    Planner,
    Scheduler,
    SessionManager,
    LabelSync,
)
from .control.action_applier import ActionApplier
from .control.fact_gatherer import FactGatherer
from .execution import GitHubIssueResolver
from .execution.worktree_adapter import GitWorktreeManager
from .execution.git_working_copy import GitWorkingCopy
from .control.dependency_evaluator import DependencyEvaluator
from .control.workflows import ReviewWorkflow, ReworkWorkflow, TriageWorkflow

if TYPE_CHECKING:
    from .orchestrator import Orchestrator
    from .ipc.server import EventServer

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
    from .orchestrator import Orchestrator

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

    # Always register logging plugin for visibility into orchestrator events
    logging_plugin = LifecycleLoggingPlugin()
    pm.register(logging_plugin, name="lifecycle_logging")

    # Create port adapters
    events = PluggyEventSink(pm)
    runner = PluggySessionRunner(pm)

    # Create GitHub adapter if repo is configured
    github = None
    if config.repo:
        github = GitHubAdapter(config.repo)

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

    # Create ActionApplier (IO boundary)
    action_applier = ActionApplier(
        labels=github,
        sessions=session_manager,
        events=events,
        repository_host=github,
        worktree_manager=worktree_manager,
        issue_tracker=github,
        reconcile=True,
    ) if github else None

    # Create FactGatherer (read-only snapshot creation)
    fact_gatherer = FactGatherer(
        config=config,
        repository_host=github,
    ) if github else None

    # Create PRScanner (for orphaned review/rework discovery)
    from .control.pr_scanner import PRScanner
    pr_scanner = PRScanner(
        config=config,
        repository=github,
        events=events,
    ) if github else None

    # Create SessionRestorer (for session recovery after restart)
    from .control.session_restorer import SessionRestorer
    session_restorer = SessionRestorer(
        config=config,
        repository_host=github,
    ) if github else None

    # Create StateMachineManager (centralized state machine management)
    from .control.state_machine_manager import StateMachineManager
    state_machine_manager = StateMachineManager(
        config=config,
        events=events,
    )

    # Build the orchestrator with injected dependencies
    return Orchestrator(
        config=config,
        events=events,
        runner=runner,
        _repository_host=github,
        planner=planner,
        session_manager=session_manager,
        label_sync=label_sync,
        action_applier=action_applier,
        fact_gatherer=fact_gatherer,
        pr_scanner=pr_scanner,
        session_restorer=session_restorer,
        worktree_manager=worktree_manager,
        working_copy=working_copy,
        state_machine_manager=state_machine_manager,
    )


def build_orchestrator_for_testing(
    config: Config,
    events: EventSink | None = None,
    runner: SessionRunner | None = None,
    github: GitHubAdapter | None = None,
    planner: Planner | None = None,
    session_manager: SessionManager | None = None,
    action_applier: ActionApplier | None = None,
    fact_gatherer: FactGatherer | None = None,
) -> "Orchestrator":
    """Build an orchestrator for testing with mock dependencies.

    Args:
        config: Application configuration
        events: Optional mock EventSink (defaults to NullEventSink)
        runner: Optional mock SessionRunner (defaults to NullSessionRunner)
        github: Optional mock GitHubAdapter
        planner: Optional mock Planner (defaults to creating one)
        session_manager: Optional mock SessionManager (defaults to creating one)
        action_applier: Optional mock ActionApplier (created from github if available)
        fact_gatherer: Optional mock FactGatherer (created from github if available)

    Returns:
        Orchestrator configured with test dependencies
    """
    from .orchestrator import Orchestrator

    events = events or NullEventSink()
    runner = runner or NullSessionRunner()

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

    # Create default action applier if github is available and none provided
    if action_applier is None and github is not None:
        action_applier = ActionApplier(
            labels=github,
            sessions=session_manager,
            events=events,
            repository_host=github,
            worktree_manager=worktree_manager,
            issue_tracker=github,
            reconcile=False,  # Disable for testing by default
        )

    # Create default fact gatherer if github is available and none provided
    if fact_gatherer is None and github is not None:
        fact_gatherer = FactGatherer(
            config=config,
            repository_host=github,
        )

    return Orchestrator(
        config=config,
        events=events,
        runner=runner,
        _repository_host=github,
        planner=planner,
        session_manager=session_manager,
        action_applier=action_applier,
        fact_gatherer=fact_gatherer,
        worktree_manager=worktree_manager,
        working_copy=working_copy,
    )


async def build_orchestrator_with_ipc(
    config: Config,
    enable_sse: bool = True,
) -> tuple["Orchestrator", "EventServer"]:
    """Build orchestrator with IPC server for external UI processes.

    This variant starts the IPC server and registers the IPC plugin.
    Use this when running in daemon mode with external UI clients.

    Args:
        config: Application configuration
        enable_sse: Whether to also enable SSE

    Returns:
        Tuple of (Orchestrator, EventServer)
    """
    from .orchestrator import Orchestrator
    from .ipc import EventServer

    # Create the pluggy plugin manager
    pm = create_plugin_manager(
        terminal_plugin=config.terminal_adapter,
        ui_mode=config.ui_mode,
    )

    # Start IPC server
    ipc_server = EventServer()
    await ipc_server.start()

    # Register IPC plugin to forward events
    ipc_plugin = LifecycleIPCPlugin(ipc_server)
    pm.register(ipc_plugin, name="lifecycle_ipc")
    logger.info("IPC server started at %s", ipc_server.socket_path)

    # Register SSE plugin if enabled
    if enable_sse:
        try:
            sse_plugin = LifecycleSSEPlugin()
            pm.register(sse_plugin, name="lifecycle_sse")
            logger.info("SSE lifecycle plugin registered")
        except Exception as e:
            logger.warning("Failed to register SSE plugin: %s", e)

    # Always register logging plugin for visibility into orchestrator events
    logging_plugin = LifecycleLoggingPlugin()
    pm.register(logging_plugin, name="lifecycle_logging")

    # Create port adapters
    events = PluggyEventSink(pm)
    runner = PluggySessionRunner(pm)

    # Wire up subprocess events to flow through the main event system
    from .ports.event_sink import TraceEvent

    def handle_subprocess_event(event_name: str, event_data: dict) -> None:
        """Forward subprocess events through the main event system."""
        try:
            trace_event = TraceEvent(name=event_name, data=event_data)
            events.publish(trace_event)
        except Exception as e:
            logger.warning("Failed to forward subprocess event %s: %s", event_name, e)

    ipc_server.set_event_handler(handle_subprocess_event)

    # Create GitHub adapter if repo is configured
    github = None
    if config.repo:
        github = GitHubAdapter(config.repo)

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

    # Create ActionApplier (IO boundary)
    action_applier = ActionApplier(
        labels=github,
        sessions=session_manager,
        events=events,
        repository_host=github,
        worktree_manager=worktree_manager,
        issue_tracker=github,
        reconcile=True,
    ) if github else None

    # Create FactGatherer (read-only snapshot creation)
    fact_gatherer = FactGatherer(
        config=config,
        repository_host=github,
    ) if github else None

    # Create StateMachineManager
    state_machine_manager = StateMachineManager(
        config=config,
        events=events,
    )

    # Build the orchestrator
    orchestrator = Orchestrator(
        config=config,
        events=events,
        runner=runner,
        _repository_host=github,
        planner=planner,
        session_manager=session_manager,
        label_sync=label_sync,
        action_applier=action_applier,
        fact_gatherer=fact_gatherer,
        worktree_manager=worktree_manager,
        working_copy=working_copy,
        state_machine_manager=state_machine_manager,
    )

    return orchestrator, ipc_server
