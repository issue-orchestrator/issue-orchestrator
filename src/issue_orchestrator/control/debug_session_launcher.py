"""An operator's interactive debug session, launched like every agent session (#9012).

The launch shares the launcher's collaborators rather than re-assembling them:
the one launch-prompt composition (#8141), the persisted prompt file the agent
is told to read, the provider readiness gate, the terminal's exact preflight and
the credentialed start. Every refusal is a typed
:class:`~..ports.debug_session_launch.DebugSessionOutcome` for the transport to
phrase.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

from ..domain.issue_run_allocation import IssueRunAllocation
from ..domain.issue_run_evidence import IssueRunEvidenceUnavailable
from ..domain.launch_prompt import LaunchPromptUnavailable
from ..domain.launch_prompt_delivery import LaunchCommand, LaunchCommandTooLong
from ..domain.models import get_completion_path
from ..domain.session_key import SessionKey
from ..domain.session_kind import SessionKind
from ..ports.debug_session_launch import DebugSessionOutcome, DebugSessionRequest, DebugSessionStatus
from .launch_guards import callback_endpoint_not_ready
from .launch_terminal import LaunchTerminal
from .session_manager import SessionRef
from .session_rework_launcher import PromptPersister, ProviderReadinessChecker, SessionEnvBuilder

if TYPE_CHECKING:
    from ..domain.models import AgentConfig
    from ..infra.config import Config
    from ..ports import Issue as IssueProtocol, RepositoryHost
    from ..ports.agent_callback_endpoint import AgentCallbackEndpoint
    from ..ports.issue_run_allocator import IssueRunAllocator
    from ..ports.launch_prompt import LaunchPromptProvider
    from ..ports.session_output import SessionOutput

logger = logging.getLogger(__name__)

#: What the agent is told about the session it is in, ahead of the issue.
DEBUG_SESSION_CONTEXT = (
    "This is an INTERACTIVE DEBUG SESSION. A previous automated run failed or was blocked. "
    "Work with the user to investigate and fix the issue. When done, the user will run "
    "'coding-done --resume' to continue the orchestrator flow."
)


@dataclass(frozen=True, slots=True)
class DebugLaunchDependencies:
    """The launcher's collaborators a debug launch shares."""

    config: "Config"
    repository_host: "RepositoryHost"
    session_exists: Callable[[str], bool]
    agent_callback_endpoint: "AgentCallbackEndpoint"
    check_provider_ready: ProviderReadinessChecker
    launch_prompt: "LaunchPromptProvider"
    issue_run_allocator: "IssueRunAllocator"
    session_output: "SessionOutput"
    persist_session_prompt: PromptPersister
    build_session_env: SessionEnvBuilder
    terminal: LaunchTerminal


@dataclass(frozen=True, slots=True)
class _DebugTarget:
    """The issue a debug launch is for, resolved, with the agent that runs it."""

    request: DebugSessionRequest
    session_name: str
    issue: "IssueProtocol"
    agent_label: str
    agent_config: "AgentConfig"

    def outcome(self, status: DebugSessionStatus, reason: str = "") -> DebugSessionOutcome:
        return DebugSessionOutcome(
            status, self.request.issue_number, self.session_name, self.request.worktree_path,
            self.agent_label, reason,
        )


def launch_debug_session(deps: DebugLaunchDependencies, request: DebugSessionRequest) -> DebugSessionOutcome:
    """Launch an interactive debug session for *request*'s issue, or say why not."""
    target = _debug_target(deps, request)
    if isinstance(target, DebugSessionOutcome):
        return target
    if refused := (
        callback_endpoint_not_ready(deps.agent_callback_endpoint)
        or deps.check_provider_ready(target.agent_config, request.issue_number)
    ):
        return target.outcome(DebugSessionStatus.UNAVAILABLE, refused.reason)
    return _start(deps, target)


def _debug_target(deps: DebugLaunchDependencies, request: DebugSessionRequest) -> "_DebugTarget | DebugSessionOutcome":
    """Resolve the issue and its agent, or the outcome that stops the launch first."""
    session_name = SessionRef.for_debug(request.issue_number).name

    def stopped(status: DebugSessionStatus, agent_label: str | None = None) -> DebugSessionOutcome:
        return DebugSessionOutcome(status, request.issue_number, session_name, request.worktree_path, agent_label)

    if not request.worktree_path.exists():
        return stopped(DebugSessionStatus.WORKTREE_MISSING)
    issue = request.cached_issue or _read_issue(deps.repository_host, request.issue_number)
    if issue is None:
        return stopped(DebugSessionStatus.ISSUE_NOT_FOUND)
    agent_label = issue.agent_type
    if not agent_label:
        return stopped(DebugSessionStatus.NO_AGENT_LABEL)
    agent_config = deps.config.agents.get(agent_label)
    if agent_config is None:
        return stopped(DebugSessionStatus.NO_AGENT_CONFIG, agent_label)
    if deps.session_exists(session_name):
        return stopped(DebugSessionStatus.ALREADY_RUNNING, agent_label)
    return _DebugTarget(request, session_name, issue, agent_label, agent_config)


def _start(deps: DebugLaunchDependencies, target: _DebugTarget) -> DebugSessionOutcome:
    """Compose, persist, prepare, then start the session from the prepared command."""
    number, worktree, session_name = target.request.issue_number, target.request.worktree_path, target.session_name
    # The engine's one composition of a launch prompt: the issue's standing
    # rulings first, the coder addendum after (#8141); unavailable refuses.
    prepared_prompt = deps.launch_prompt.prepare(kind=SessionKind.CODE, issue_number=number)
    if isinstance(prepared_prompt, LaunchPromptUnavailable):
        return target.outcome(DebugSessionStatus.UNAVAILABLE, prepared_prompt.reason)
    debug_prompt = prepared_prompt.compose(target.agent_config.render_initial_prompt(
        issue_number=number, issue_title=target.issue.title, worktree=worktree,
        existing_work=DEBUG_SESSION_CONTEXT, task_kind=SessionKind.CODE.value,
    ))
    try:
        run = deps.issue_run_allocator.allocate(IssueRunAllocation(
            terminal_id=session_name,
            worktree_path=worktree,
            session_name=session_name,
            session_key=SessionKey(target.issue.key, SessionKind.CODE),
            issue_number=number,
            agent_label=target.agent_label,
            backend=deps.config.terminal_adapter or "subprocess",
        ))
    except IssueRunEvidenceUnavailable as exc:
        return target.outcome(DebugSessionStatus.UNAVAILABLE, str(exc))
    # Delivered from its run's file, never on the command line (#9012).
    launch_prompt = deps.persist_session_prompt(run.run_dir, debug_prompt)
    base_command = target.agent_config.get_command_for_prompt(
        launch_prompt, issue_number=number, issue_title=target.issue.title, worktree=worktree,
        task_kind=SessionKind.CODE.value,
    )
    completion_path = get_completion_path(target.agent_label, run_dir=run.run_dir.name)
    command = LaunchCommand.composed(deps.build_session_env(
        completion_path=completion_path, session_id=session_name, agent_label=target.agent_label,
        issue_number=number, run_assets=run, worktree_path=worktree,
    ), base_command)
    try:
        prepared = deps.terminal.prepare(command, worktree)
    except LaunchCommandTooLong as exc:
        return target.outcome(DebugSessionStatus.COMMAND_TOO_LONG, str(exc))
    deps.session_output.update_manifest(
        run.run_dir,
        {"completion_path": completion_path, "issue_number": number, "agent_label": target.agent_label},
    )
    logger.info(
        "[debug-session] Launching for issue #%d: session=%s worktree=%s agent=%s",
        number, session_name, worktree, target.agent_label,
    )
    if not deps.terminal.start(session_name, prepared, f"Debug #{number}", target.agent_config):
        return target.outcome(DebugSessionStatus.SPAWN_FAILED)
    return target.outcome(DebugSessionStatus.LAUNCHED)


def _read_issue(repository_host: "RepositoryHost", number: int) -> "IssueProtocol | None":
    """The issue from the host, or ``None`` when it cannot be read."""
    try:
        return repository_host.get_issue(number)
    except Exception as exc:  # an unreadable issue is "not found" to the operator
        logger.warning("Could not fetch issue #%d: %s", number, exc)
        return None
