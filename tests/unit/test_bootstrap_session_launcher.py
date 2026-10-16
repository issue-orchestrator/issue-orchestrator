"""The composition root binds where a launch's session starts (#9012 rework).

The facade used to relay every session start through a callback of its own,
though the start needs nothing the facade owns. The factory now starts each
session on the SessionManager that prepared its command, so both halves of a
launch's terminal come from one place, and with the provider's credentials.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from issue_orchestrator.control.needs_human_block import NO_OTHER_NEEDS_HUMAN_CAUSES
from issue_orchestrator.control.session_manager import SessionManager
from issue_orchestrator.domain.launch_prompt_delivery import LaunchCommand, PreparedSessionCommand
from issue_orchestrator.domain.models import AgentConfig, Issue
from issue_orchestrator.entrypoints.bootstrap_session_launcher import build_session_launcher_factory
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.execution.terminal_subprocess import prepare_subprocess_session
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports import NullBoardSnapshotProvider, NullEventSink
from issue_orchestrator.ports.debug_session_launch import DebugSessionRequest
from issue_orchestrator.ports.provider_readiness import NO_PROVIDER_READINESS_PROBE
from issue_orchestrator.ports.session_runner import NullSessionRunner
from tests.callback_endpoint_helpers import ready_callback_endpoint
from tests.run_allocation_helpers import allocation_for

_DEEPSEEK_ENV = {"ANTHROPIC_API_KEY": "sk-factory-not-a-real-key", "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic"}


class _DeepSeekKey:
    def session_env(self, provider: str | None) -> dict[str, str]:
        return dict(_DEEPSEEK_ENV) if provider == "deepseek" else {}


class _RecordingRunner(NullSessionRunner):
    """Prepares as the subprocess terminal does and records each start instead of running it."""

    def __init__(self) -> None:
        self.prepared: list[PreparedSessionCommand] = []
        self.started: list[dict] = []

    def prepare_session(self, command: LaunchCommand, working_dir: Path) -> PreparedSessionCommand:
        prepared = prepare_subprocess_session(command, working_dir)
        self.prepared.append(prepared)
        return prepared

    def create_session(self, session_id, prepared, title, session_name, secret_env=None) -> bool:
        self.started.append({"session_name": session_name, "prepared": prepared, "secret_env": secret_env})
        return True


def test_a_launch_starts_on_the_session_manager_that_prepared_it_with_its_credentials(tmp_path: Path) -> None:
    config = Config(repo="example/repo", repo_root=tmp_path)
    config.agents = {
        "agent:deepseek": AgentConfig(prompt_path=tmp_path / "prompt.md", provider="deepseek", model="deepseek-v4-pro"),
    }
    runner, events, output = _RecordingRunner(), NullEventSink(), FileSystemSessionOutput()
    factory = build_session_launcher_factory(
        config=config, events=events, repository_host=MagicMock(), action_applier=MagicMock(),
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
    # Only what the facade owns: no session-start callback among them.
    launcher = factory(
        board_snapshot_provider=NullBoardSnapshotProvider(), blocked_item_triage=MagicMock(),
        session_exists_fn=lambda name: False, get_issue_machine=lambda issue: MagicMock(),
        get_session_machine=lambda *args: MagicMock(), get_review_machine=lambda *args: MagicMock(),
        refresh_issue_fn=None, dependency_evaluator=None,
    )
    worktree = tmp_path / "repo-123"
    worktree.mkdir()
    issue = Issue(number=123, title="Fix checkout", labels=["agent:deepseek"], repo="example/repo")

    outcome = launcher.launch_debug_session(DebugSessionRequest(123, worktree, issue))

    assert outcome.launched, outcome
    (prepared,) = runner.prepared
    (started,) = runner.started
    assert started["session_name"] == "debug-123"
    assert started["prepared"] is prepared
    assert started["secret_env"] == _DEEPSEEK_ENV
