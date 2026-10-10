"""Command-backed actions for Control Center endpoints.

This module centralizes behavior for UI-triggered actions so endpoint handlers
are thin adapters and tests can exercise command objects directly.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional, Protocol, cast


from ..domain.pause_state import PauseActor, PauseTransitionStatus
from ..domain.repository_launch_selection import (
    RepositoryConfigurationIdentity,
    RepositoryLaunchSelection,
)
from ..ports.repository_engine_supervisor import SupervisorOps
from .engine_command_failure import (
    ENGINE_COMMAND_TIMEOUT_SECONDS,
    EngineCommandFailure,
    engine_answer_failure,
)
from .orchestrator_http_api import OrchestratorAsyncHttpApi, guarded_engine_command
from .repository_engine_start import StartRepositoryEngineCommand
from .control_center_worktree_audit import ControlCenterWorktreeAuditOwner
from ..ports.repository_host import (
    RepositoryHostError,
    repository_host_failure_payload,
    repository_host_failure_status,
)

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ActionResult:
    """Result payload and status code for command execution."""

    payload: dict[str, Any]
    status_code: int = 200


@dataclass(frozen=True)
class RepoActionRequest:
    repo_root: Path


@dataclass(frozen=True)
class ConfiguredRepoActionRequest:
    repo_root: Path
    selection: RepositoryLaunchSelection


@dataclass(frozen=True)
class SelectLaunchConfigurationRequest:
    repo_root: Path
    selection: RepositoryLaunchSelection


@dataclass(frozen=True)
class RefreshActionRequest:
    repo_root: Path
    inflight_stable_ids: Optional[list[str]] = None


@dataclass(frozen=True)
class DoctorActionRequest:
    repo_root: Path
    selection: RepositoryLaunchSelection


@dataclass(frozen=True)
class AuditActionRequest:
    repo_root: Path
    selection: RepositoryLaunchSelection
    issue_number: int | None = None


@dataclass(frozen=True)
class TraceActionRequest:
    repo_root: Path
    issue_number: int
    limit: int = 100


class AsyncCommand(Protocol):
    async def execute(self, request: Any) -> ActionResult: ...


class EngineCommandForwarder:
    """Forward one operator command to a running Repository Engine (#8222).

    The single owner of the Control Center's engine passthrough: it resolves
    the engine's port, sends the command with a budget sized for the engine's
    state-lock wait, and turns every transport, status or body failure into a
    typed ``passthrough_failed`` payload whose ``detail`` names the cause.
    Anything else is a Control Center bug and propagates.
    """

    def __init__(
        self,
        supervisor: SupervisorOps,
        *,
        timeout_seconds: float = ENGINE_COMMAND_TIMEOUT_SECONDS,
    ) -> None:
        self._supervisor = supervisor
        self._timeout_seconds = timeout_seconds

    @property
    def timeout_seconds(self) -> float:
        return self._timeout_seconds

    async def forward(
        self,
        repo_root: Path,
        command: str,
        send: Callable[[OrchestratorAsyncHttpApi], Awaitable[object]],
        *,
        accepted: str,
    ) -> ActionResult:
        """Send ``command``; ``accepted`` is the answer ``status`` meaning it worked."""
        status_info = self._supervisor.status(repo_root)
        if status_info.state != "running" or status_info.port is None:
            return ActionResult(
                {
                    "error": "not_running",
                    "state": status_info.state,
                    "detail": f"The repository engine is not running (state: {status_info.state}).",
                },
                status_code=400,
            )
        base_url = f"http://127.0.0.1:{status_info.port}"
        api = OrchestratorAsyncHttpApi(
            base_url_provider=lambda: base_url,
            timeout_seconds=self._timeout_seconds,
            pause_actor=PauseActor.CONTROL_CENTER,
        )
        try:
            answer = await guarded_engine_command(
                send(api),
                command=command,
                url=base_url,
                timeout_seconds=self._timeout_seconds,
            )
        finally:
            await api.close()
        if isinstance(answer, EngineCommandFailure):
            return self._failed(answer)
        refusal = engine_answer_failure(answer, command=command, url=base_url, accepted=accepted)
        if refusal is not None:
            return self._failed(refusal)
        return ActionResult(cast(dict[str, Any], answer))

    @staticmethod
    def _failed(failure: EngineCommandFailure) -> ActionResult:
        logger.warning("[control-center] engine %s failed: %s", failure.command, failure.detail)
        return ActionResult(failure.to_payload(), status_code=failure.http_status)


class PauseOrchestratorCommand:
    """Pause a running orchestrator via passthrough API."""

    def __init__(self, forwarder: EngineCommandForwarder) -> None:
        self._forwarder = forwarder

    async def execute(self, request: RepoActionRequest) -> ActionResult:
        return await self._forwarder.forward(
            request.repo_root, "pause", lambda api: api.pause(), accepted=str(PauseTransitionStatus.PAUSED)
        )


class ResumeOrchestratorCommand:
    """Resume a running orchestrator via passthrough API."""

    def __init__(self, forwarder: EngineCommandForwarder) -> None:
        self._forwarder = forwarder

    async def execute(self, request: RepoActionRequest) -> ActionResult:
        return await self._forwarder.forward(
            request.repo_root, "resume", lambda api: api.resume(), accepted=str(PauseTransitionStatus.RESUMED)
        )


class RefreshOrchestratorCommand:
    """Trigger refresh on a running orchestrator via passthrough API."""

    def __init__(self, forwarder: EngineCommandForwarder) -> None:
        self._forwarder = forwarder

    async def execute(self, request: RefreshActionRequest) -> ActionResult:
        inflight = list(request.inflight_stable_ids or [])
        return await self._forwarder.forward(
            request.repo_root,
            "refresh",
            lambda api: api.refresh(inflight),
            accepted="refresh_requested",
        )


class SelectLaunchConfigurationCommand:
    """Own mode/config selection lifecycle policy and persistence."""

    def __init__(self, supervisor: SupervisorOps) -> None:
        self._supervisor = supervisor

    async def execute(
        self,
        request: SelectLaunchConfigurationRequest,
    ) -> ActionResult:
        from ..infra.repo_lock import (
            RepositoryLifecycleBusy,
            exclusive_repository_lifecycle,
        )

        try:
            with exclusive_repository_lifecycle(request.repo_root):
                return self._execute_guarded(request)
        except RepositoryLifecycleBusy:
            return self._engine_running_result()

    def _execute_guarded(
        self,
        request: SelectLaunchConfigurationRequest,
    ) -> ActionResult:
        from ..infra.config import list_configs
        from ..infra.repo_registry import set_selected_launch_selection
        from .control_center_runtime import detect_repository_orchestrators

        statuses = self._supervisor.status_all_instances(
            request.repo_root,
            request.selection.config.value,
            mode=request.selection.mode.value,
        )
        blocking_states = sorted(
            {
                instance.state
                for instance in statuses.instances
                if instance.state not in {"stopped", "failed"}
            }
        )
        if blocking_states:
            result = self._engine_running_result()
            result.payload["states"] = blocking_states
            return result

        orphans = detect_repository_orchestrators(request.repo_root)
        if orphans:
            return ActionResult(
                {
                    "error": "engine_running",
                    "detail": (
                        "Stop the untracked Repository Engine before changing its "
                        "mode or config."
                    ),
                    "ports": [orphan["port"] for orphan in orphans],
                },
                status_code=409,
            )

        if request.selection.config.value not in list_configs(
            request.repo_root,
            request.selection.mode,
        ):
            return ActionResult(
                {
                    "error": "config_not_found",
                    "detail": (
                        f"Configuration {request.selection.mode.value!r}/"
                        f"{request.selection.config.value!r} does not exist"
                    ),
                },
                status_code=404,
            )

        if not set_selected_launch_selection(request.repo_root, request.selection):
            return ActionResult({"error": "Repo not found"}, status_code=404)
        return ActionResult({"status": "ok", **request.selection.to_dict()})

    @staticmethod
    def _engine_running_result() -> ActionResult:
        return ActionResult(
            {
                "error": "engine_running",
                "detail": (
                    "Stop the Repository Engine before changing its mode or config."
                ),
            },
            status_code=409,
        )


class DoctorCommand:
    """Run repository doctor checks."""

    async def execute(self, request: DoctorActionRequest) -> ActionResult:
        from ..infra.config import Config, get_config_path, list_configs
        from ..infra.doctor import run_doctor
        from ..execution.command_runner import LocalCommandRunner

        config = None
        config_path = get_config_path(
            request.repo_root,
            request.selection.config.value,
            request.selection.mode,
        )
        available = list_configs(request.repo_root, request.selection.mode)
        if request.selection.config.value in available:
            try:
                config = Config.load(config_path)
            except Exception:
                config = None

        result = run_doctor(
            config=config, config_path=config_path, runner=LocalCommandRunner()
        )
        return ActionResult(dict(result.to_dict()))


class AuditIssuesCommand:
    """Audit queued/blocked issue reasons."""

    async def execute(self, request: AuditActionRequest) -> ActionResult:
        from ..infra.audit import audit_queue
        from ..execution.providers import create_repository_host
        from ..execution.git_working_copy import GitWorkingCopy
        from ..infra.analysis import extract_issue_branches
        from .control_center_runtime import load_config_for_selection

        try:
            config = load_config_for_selection(request.repo_root, request.selection)
        except FileNotFoundError:
            return ActionResult({"error": "Config not found for repo"}, status_code=404)

        if not config.repo:
            return ActionResult({"error": "No repository configured"}, status_code=400)

        try:
            issue_tracker = create_repository_host(config.repo, config=config)
            working_copy = GitWorkingCopy()
            issue_branches = extract_issue_branches(
                working_copy.list_remote_branches(config.repo_root),
            )
            entries = audit_queue(
                config,
                state=None,
                issue_tracker=issue_tracker,
                issue_branches=issue_branches,
                known_proposals=_stored_known_proposals(config),
            )
            if request.issue_number is not None:
                entries = [
                    entry
                    for entry in entries
                    if entry.issue.number == request.issue_number
                ]
            return ActionResult(
                {
                    "entries": [
                        {
                            "issue_number": entry.issue.number,
                            "title": entry.issue.title,
                            "status": entry.status.value,
                            "reason": entry.detail,
                            "labels": list(entry.issue.labels),
                            "agent": entry.issue.agent_type,
                            "priority": entry.issue.priority,
                        }
                        for entry in entries
                    ],
                }
            )
        except RepositoryHostError as exc:
            return ActionResult(
                repository_host_failure_payload(exc),
                status_code=repository_host_failure_status(exc),
            )
        except Exception as exc:
            return ActionResult({"error": str(exc)}, status_code=500)


class TraceIssueCommand:
    """Load issue trace entries from orchestrator logs."""

    async def execute(self, request: TraceActionRequest) -> ActionResult:
        log_file = (
            request.repo_root
            / ".issue-orchestrator"
            / "state"
            / "logs"
            / "orchestrator.log"
        )
        if not log_file.exists():
            return ActionResult(
                {
                    "entries": [],
                    "message": "No log file found. Has the orchestrator run for this repo?",
                }
            )

        try:
            lines = log_file.read_text().splitlines()
            last_start = 0
            for i, line in enumerate(lines):
                if "Starting orchestrator" in line:
                    last_start = i

            pattern = re.compile(
                rf"\[issue-{request.issue_number}\]|"
                rf"issue={request.issue_number}(?![0-9])|"
                rf"issue_number={request.issue_number}(?![0-9])|"
                rf"issue #{request.issue_number}(?![0-9])",
            )
            matches: list[str] = []
            for line in lines[last_start:]:
                if pattern.search(line):
                    matches.append(line)
                    if len(matches) >= request.limit:
                        break
            return ActionResult(
                {
                    "entries": matches,
                    "total": len(matches),
                    "truncated": len(matches) >= request.limit,
                }
            )
        except Exception as exc:
            return ActionResult({"error": str(exc)}, status_code=500)


class ListStaleWorktreesCommand:
    """Audit registered worktrees with the startup cleanup policy (read-only)."""

    def __init__(self, audit_owner: ControlCenterWorktreeAuditOwner) -> None:
        self._audit_owner = audit_owner

    async def execute(self, request: ConfiguredRepoActionRequest) -> ActionResult:
        try:
            report = self._audit_owner.audit(request.repo_root, request.selection)
            return ActionResult(report.to_payload())
        except Exception as exc:
            return ActionResult({"error": str(exc)}, status_code=500)


class InitializeLabelsCommand:
    """Initialize or refresh GitHub labels for a repository."""

    async def execute(self, request: ConfiguredRepoActionRequest) -> ActionResult:
        from ..execution.providers import create_repository_host
        from .control_center_runtime import load_config_for_selection

        try:
            config = load_config_for_selection(request.repo_root, request.selection)
        except FileNotFoundError:
            return ActionResult({"error": "Config not found for repo"}, status_code=404)

        if not config.repo:
            return ActionResult({"error": "No repository configured"}, status_code=400)

        try:
            from ..control.label_manager import LabelManager

            _lm = LabelManager(config)
            client = create_repository_host(config.repo, config=config)
            labels = _lm.repository_initialization_labels(list(config.agents))
            created: list[str] = []
            updated: list[str] = []
            failed: list[str] = []
            existing = {label.get("name") for label in client.list_labels()}
            for label in labels:
                try:
                    client.create_label(label, force=True)
                    if label in existing:
                        updated.append(label)
                    else:
                        created.append(label)
                except Exception:
                    failed.append(label)
            return ActionResult(
                {
                    "created": created,
                    "updated": updated,
                    "failed": failed,
                }
            )
        except Exception as exc:
            return ActionResult({"error": str(exc)}, status_code=500)


class ControlCenterActions:
    """Owns command objects behind Control Center action endpoints."""

    def __init__(
        self,
        supervisor: SupervisorOps,
        *,
        pause_cmd: PauseOrchestratorCommand | None = None,
        resume_cmd: ResumeOrchestratorCommand | None = None,
        refresh_cmd: RefreshOrchestratorCommand | None = None,
        doctor_cmd: DoctorCommand | None = None,
        audit_cmd: AuditIssuesCommand | None = None,
        trace_cmd: TraceIssueCommand | None = None,
        labels_cmd: InitializeLabelsCommand | None = None,
        stale_worktrees_cmd: ListStaleWorktreesCommand | None = None,
        select_launch_config_cmd: SelectLaunchConfigurationCommand | None = None,
        start_repo_engine_cmd: StartRepositoryEngineCommand | None = None,
    ) -> None:
        self.supervisor = supervisor
        forwarder = EngineCommandForwarder(supervisor)
        self.pause_cmd: PauseOrchestratorCommand = (
            pause_cmd or PauseOrchestratorCommand(forwarder)
        )
        self.resume_cmd: ResumeOrchestratorCommand = (
            resume_cmd or ResumeOrchestratorCommand(forwarder)
        )
        self.refresh_cmd: RefreshOrchestratorCommand = (
            refresh_cmd or RefreshOrchestratorCommand(forwarder)
        )
        self.doctor_cmd: DoctorCommand = doctor_cmd or DoctorCommand()
        self.audit_cmd: AuditIssuesCommand = audit_cmd or AuditIssuesCommand()
        self.trace_cmd: TraceIssueCommand = trace_cmd or TraceIssueCommand()
        self.labels_cmd: InitializeLabelsCommand = (
            labels_cmd or InitializeLabelsCommand()
        )
        if stale_worktrees_cmd is None:
            from ..control.worktree_reconciliation import WorktreeAuditOwner
            from .control_center_worktree_audit import (
                HttpRepositoryEngineStatusReader,
                RepositoryEngineWorktreeActivityReader,
            )
            from .worktree_adapter import GitWorktreeManager

            stale_worktrees_cmd = ListStaleWorktreesCommand(
                ControlCenterWorktreeAuditOwner(
                    lambda root: WorktreeAuditOwner(GitWorktreeManager(root)),
                    RepositoryEngineWorktreeActivityReader(
                        supervisor,
                        HttpRepositoryEngineStatusReader(),
                    ),
                )
            )
        self.stale_worktrees_cmd = stale_worktrees_cmd
        self.select_launch_config_cmd: SelectLaunchConfigurationCommand = (
            select_launch_config_cmd or SelectLaunchConfigurationCommand(supervisor)
        )
        self.start_repo_engine_cmd: StartRepositoryEngineCommand = (
            start_repo_engine_cmd or StartRepositoryEngineCommand(supervisor)
        )

    def effective_launch_selection(
        self,
        repo_root: Path,
    ) -> RepositoryLaunchSelection:
        """Return the live selection when active, otherwise registry desired state."""
        from .control_center_runtime import get_effective_launch_selection

        return get_effective_launch_selection(repo_root, self.supervisor)

    def effective_configuration_identity(
        self,
        repo_root: Path,
    ) -> RepositoryConfigurationIdentity:
        """Return effective live-or-desired mode/config/fingerprint identity."""
        from .control_center_runtime import get_effective_configuration_identity

        return get_effective_configuration_identity(repo_root, self.supervisor)


def _stored_known_proposals(config) -> frozenset[int]:
    """The authority store's proposal identity, for an audit run without a
    live engine (#7763 review r27 F1)."""
    from ..control.tech_lead_approval import stored_known_proposals
    from ..infra.tech_lead_authority_store import SqliteTechLeadAuthorityStore

    return stored_known_proposals(SqliteTechLeadAuthorityStore.for_repo(config.repo_root), config.repo)
