"""The composition root binds the terminal a launch uses (#9012 rework).

The facade used to relay every session start and existence check through
callbacks of its own, though neither needs anything the facade owns. The
factory now binds the SessionManager that prepares a launch's command as the
one its conflict checks ask and its session starts on, so a launch's terminal
comes from one place, and starts with the provider's credentials.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.needs_human_block import NO_OTHER_NEEDS_HUMAN_CAUSES
from issue_orchestrator.control.session_launch_types import LaunchDisposition
from issue_orchestrator.control.session_manager import SessionManager
from issue_orchestrator.domain.launch_prompt_delivery import LaunchCommand, PreparedSessionCommand
from issue_orchestrator.domain.external_pr_adoption import LaunchAuthority
from issue_orchestrator.domain.issue_key import GitHubIssueKey
from issue_orchestrator.domain.models import AgentConfig, Issue, OrchestratorState, PendingRework
from issue_orchestrator.entrypoints.bootstrap_session_launcher import build_session_launcher_factory
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.execution.terminal_subprocess import prepare_subprocess_session
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports import NullBoardSnapshotProvider, NullEventSink
from issue_orchestrator.ports.debug_session_launch import DebugSessionRequest, DebugSessionStatus
from issue_orchestrator.ports.pull_request_tracker import PRInfo
from issue_orchestrator.ports.provider_readiness import NO_PROVIDER_READINESS_PROBE
from issue_orchestrator.ports.session_launcher_factory import SessionLauncherFactory
from issue_orchestrator.ports.session_runner import NullSessionRunner
from tests.callback_endpoint_helpers import ready_callback_endpoint
from tests.run_allocation_helpers import allocation_for

_DEEPSEEK_ENV = {"ANTHROPIC_API_KEY": "sk-factory-not-a-real-key", "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic"}


class _DeepSeekKey:
    def session_env(self, provider: str | None) -> dict[str, str]:
        return dict(_DEEPSEEK_ENV) if provider == "deepseek" else {}


class _RecordingRunner(NullSessionRunner):
    """Prepares as the subprocess terminal does and records each start instead of running it."""

    def __init__(self, running: frozenset[str] = frozenset()) -> None:
        self.running = running
        self.prepared: list[PreparedSessionCommand] = []
        self.started: list[dict] = []

    def session_exists(self, session_id: int, session_name: str) -> bool:
        return session_name in self.running

    def prepare_session(self, command: LaunchCommand, working_dir: Path) -> PreparedSessionCommand:
        prepared = prepare_subprocess_session(command, working_dir)
        self.prepared.append(prepared)
        return prepared

    def create_session(self, session_id, prepared, title, session_name, secret_env=None) -> bool:
        self.started.append({"session_name": session_name, "prepared": prepared, "secret_env": secret_env})
        return True


def _factory(runner: _RecordingRunner, tmp_path: Path, repository_host: object | None = None) -> SessionLauncherFactory:
    config = Config(repo="example/repo", repo_root=tmp_path)
    config.agents = {
        "agent:deepseek": AgentConfig(prompt_path=tmp_path / "prompt.md", provider="deepseek", model="deepseek-v4-pro"),
    }
    events, output = NullEventSink(), FileSystemSessionOutput()
    return build_session_launcher_factory(
        config=config, events=events, repository_host=repository_host or MagicMock(), action_applier=MagicMock(),
        session_manager=SessionManager(runner=runner, events=events, config=config),
        worktree_manager=MagicMock(), working_copy=MagicMock(), command_runner=MagicMock(),
        session_output=output,
        issue_run_allocator=allocation_for(output, config),
        manifest_downloader=MagicMock(), tech_lead_authority=MagicMock(),
        validated_work_recovery_authority=MagicMock(), claim_manager=None, provider_resilience=None,
        state_machine_manager=MagicMock(), label_manager=None,
        agent_callback_endpoint=ready_callback_endpoint(),
        provider_readiness_probe=NO_PROVIDER_READINESS_PROBE,
        provider_credentials=_DeepSeekKey(),
        needs_human_block=NO_OTHER_NEEDS_HUMAN_CAUSES,
    )


# Only what the facade owns: no session-start or session-existence callback among them.
_FACADE_OWNED = dict(
    board_snapshot_provider=NullBoardSnapshotProvider(), blocked_item_triage=MagicMock(),
    get_issue_machine=lambda issue: MagicMock(),
    get_session_machine=lambda *args: MagicMock(), get_review_machine=lambda *args: MagicMock(),
    refresh_issue_fn=None, dependency_evaluator=None, state=OrchestratorState,
)


def _debug_request(tmp_path: Path) -> DebugSessionRequest:
    worktree = tmp_path / "repo-123"
    worktree.mkdir()
    issue = Issue(number=123, title="Fix checkout", labels=["agent:deepseek"], repo="example/repo")
    return DebugSessionRequest(123, worktree, issue)


def test_a_launch_starts_on_the_session_manager_that_prepared_it_with_its_credentials(tmp_path: Path) -> None:
    runner = _RecordingRunner()
    launcher = _factory(runner, tmp_path)(**_FACADE_OWNED)

    outcome = launcher.launch_debug_session(_debug_request(tmp_path))

    assert outcome.launched, outcome
    (prepared,) = runner.prepared
    (started,) = runner.started
    assert started["session_name"] == "debug-123"
    assert started["prepared"] is prepared
    assert started["secret_env"] == _DEEPSEEK_ENV


def test_a_session_running_on_that_session_manager_stops_the_launch_before_it_prepares(tmp_path: Path) -> None:
    runner = _RecordingRunner(running=frozenset({"debug-123"}))
    launcher = _factory(runner, tmp_path)(**_FACADE_OWNED)

    outcome = launcher.launch_debug_session(_debug_request(tmp_path))

    assert outcome.status is DebugSessionStatus.ALREADY_RUNNING
    assert runner.prepared == []
    assert runner.started == []


def test_a_caller_cannot_check_session_existence_against_another_terminal(tmp_path: Path) -> None:
    factory = _factory(_RecordingRunner(running=frozenset({"debug-123"})), tmp_path)

    with pytest.raises(TypeError, match="session_exists_fn"):
        factory(**_FACADE_OWNED, session_exists_fn=lambda name: False)  # pyright: ignore[reportCallIssue]


def test_the_factory_binds_the_external_pr_launch_gate_the_facade_no_longer_passes(tmp_path: Path) -> None:
    """#9499: a rework queued under an integration effort's authority, with
    integration mode now off, is withdrawn by the gate the factory binds,
    before anything is prepared or started. Its PR is still there, so nothing
    but that gate withdraws it."""
    runner = _RecordingRunner()
    host = MagicMock()
    host.get_pr.return_value = PRInfo(
        number=456, title="Hand-made #123", url="https://github.com/example/repo/pull/456", branch="123-feature",
        body="Closes #123", state="open", labels=["origin:external"],
    )
    launcher = _factory(runner, tmp_path, host)(**_FACADE_OWNED)
    rework = PendingRework(
        issue_key=GitHubIssueKey(repo="example/repo", external_id="123"), agent_type="agent:deepseek",
        issue_number=123, pr_number=456,
        authority=LaunchAuthority(effort="integration", issue_number=123, pr_number=456, agent="agent:deepseek"),
    )

    result = launcher.launch_rework_session(rework, active_sessions=[])

    assert (result.success, result.disposition) == (False, LaunchDisposition.WITHDRAWN), result
    assert "integration effort" in (result.reason or ""), result
    host.get_pr.assert_not_called()
    assert runner.prepared == []
    assert runner.started == []
