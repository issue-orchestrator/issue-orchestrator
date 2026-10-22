"""An operator's debug session is launched by the session launcher (#9012 review r1 F3).

The control API used to assemble this launch itself and start it without its
provider's credentials. Here the launch runs through the real control chain -
the launcher, ``session_routing`` and :class:`SessionManager` - down to the
session-runner port, whose double prepares exactly as the subprocess terminal
does and records each start instead of running it.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.debug_session_launcher import debug_session_request
from issue_orchestrator.control.launch_prompt import IssueLaunchPrompt
from issue_orchestrator.control.session_launcher import SessionLauncher
from issue_orchestrator.control.session_manager import SessionManager
from issue_orchestrator.control.session_routing import session_exists, session_starter
from issue_orchestrator.control.worktree_manager import get_worktree_path
from issue_orchestrator.domain.issue_run_evidence import IssueRunEvidenceUnavailable
from issue_orchestrator.domain.launch_prompt import LaunchPromptUnavailable
from issue_orchestrator.domain.launch_prompt_delivery import LaunchCommand, PreparedSessionCommand
from issue_orchestrator.domain.models import AgentConfig, Issue
from issue_orchestrator.events import EventName
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.execution.terminal_subprocess import prepare_subprocess_session
from issue_orchestrator.infra.agent_callback_endpoint import RuntimeAgentCallbackEndpoint
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports import NullBoardSnapshotProvider
from issue_orchestrator.ports.coder_prompt import NO_CODER_PROMPT_ADDENDUM
from issue_orchestrator.ports.debug_session_launch import (
    DebugSessionOutcome,
    DebugSessionRequest,
    DebugSessionStatus,
)
from issue_orchestrator.ports.session_runner import NullSessionRunner
from tests.callback_endpoint_helpers import ready_callback_endpoint
from tests.launch_prompt_helpers import delivered_prompt
from tests.run_allocation_helpers import MemoryIssueRunLedger, make_session_launcher
from tests.standing_ruling_helpers import IssueBodies, a_ruling, body_with, rulings_owner

_DEEPSEEK_KEY = "sk-debug-not-a-real-key"
_DEEPSEEK_ENV = {"ANTHROPIC_API_KEY": _DEEPSEEK_KEY, "ANTHROPIC_BASE_URL": "https://api.deepseek.com/anthropic"}
_RULING = a_ruling(text="Runtime stamping replaces the walk checker.")


class _DeepSeekKey:
    """Provider credentials: DeepSeek needs a key and an endpoint in its session."""

    def session_env(self, provider: str | None) -> dict[str, str]:
        return dict(_DEEPSEEK_ENV) if provider == "deepseek" else {}


class _RecordingTerminal(NullSessionRunner):
    """A session runner that prepares as the subprocess terminal does and records each start."""

    def __init__(self) -> None:
        self.prepared: list[PreparedSessionCommand] = []
        self.started: list[dict] = []
        self.refuse_start = False

    def prepare_session(self, command: LaunchCommand, working_dir: Path) -> PreparedSessionCommand:
        prepared = prepare_subprocess_session(command, working_dir)
        self.prepared.append(prepared)
        return prepared

    def create_session(self, session_id, prepared, title, session_name, secret_env=None) -> bool:
        if self.refuse_start:
            return False
        self.started.append({
            "session_id": session_id, "prepared": prepared, "title": title,
            "session_name": session_name, "secret_env": secret_env,
        })
        return True

    def session_exists(self, session_id: int, session_name: str) -> bool:
        return any(start["session_name"] == session_name for start in self.started)


class _Events:
    def __init__(self) -> None:
        self.published: list = []

    def publish(self, event) -> None:
        self.published.append(event)

    def named(self, name: EventName) -> list:
        return [event for event in self.published if event.name == name]


@dataclass
class _Rig:
    launcher: SessionLauncher
    terminal: _RecordingTerminal
    events: _Events
    session_output: FileSystemSessionOutput
    ledger: MemoryIssueRunLedger
    host: MagicMock
    worktree: Path
    issue: Issue

    def launch(self, *, cached: bool = True) -> DebugSessionOutcome:
        return self.launcher.launch_debug_session(
            DebugSessionRequest(123, self.worktree, self.issue if cached else None),
        )

    def run_dir(self) -> Path | None:
        return self.session_output.find_run_dir(self.worktree, "debug-123")


def _rig(tmp_path: Path, *, labels=("agent:deepseek",), endpoint=None, launch_prompt=None, ledger=None) -> _Rig:
    config = Config(repo="example/repo", repo_root=tmp_path)
    config.agents = {
        "agent:deepseek": AgentConfig(prompt_path=tmp_path / "prompt.md", provider="deepseek", model="deepseek-v4-pro"),
    }
    events, terminal = _Events(), _RecordingTerminal()
    manager = SessionManager(runner=terminal, events=events, config=config)
    session_output = FileSystemSessionOutput()
    ledger = ledger if ledger is not None else MemoryIssueRunLedger()
    host = MagicMock()
    worktree = tmp_path / "repo-123"
    worktree.mkdir()
    launcher = make_session_launcher(
        issue_run_ledger=ledger,
        config=config,
        events=events,
        repository_host=host,
        action_applier=MagicMock(),
        session_manager=manager,
        worktree_manager=MagicMock(),
        working_copy=MagicMock(),
        command_runner=MagicMock(),
        session_output=session_output,
        manifest_downloader=MagicMock(),
        tech_lead_authority=MagicMock(),
        # As the facade (session_exists) and the composition root (the start) wire them.
        session_exists_fn=lambda name: session_exists(name, manager, events),
        create_session_fn=session_starter(manager, events),
        get_issue_machine=lambda issue: MagicMock(),
        get_session_machine=lambda *args: MagicMock(),
        get_review_machine=lambda *args: MagicMock(),
        board_snapshot_provider=NullBoardSnapshotProvider(),
        agent_callback_endpoint=endpoint if endpoint is not None else ready_callback_endpoint(),
        provider_credentials=_DeepSeekKey(),
        launch_prompt=launch_prompt or IssueLaunchPrompt(
            NO_CODER_PROMPT_ADDENDUM, rulings_owner(IssueBodies({123: body_with(_RULING)})),
        ),
    )
    issue = Issue(number=123, title="Fix checkout", labels=list(labels), repo="example/repo")
    return _Rig(launcher, terminal, events, session_output, ledger, host, worktree, issue)


def test_the_session_runner_is_handed_the_providers_credentials(tmp_path: Path) -> None:
    """The debug route started a DeepSeek session without its key and endpoint;
    the launcher's credentialed start hands them to the session runner."""
    rig = _rig(tmp_path)

    outcome = rig.launch()

    assert outcome.status is DebugSessionStatus.LAUNCHED
    (start,) = rig.terminal.started
    assert (start["session_id"], start["session_name"], start["title"]) == (123, "debug-123", "Debug #123")
    assert start["secret_env"] == _DEEPSEEK_ENV
    # Environment, never argv: ``ps`` is world-readable.
    assert all(_DEEPSEEK_KEY not in argument for argument in start["prepared"].exec_argv)


def test_the_session_starts_from_exactly_what_its_terminal_prepared(tmp_path: Path) -> None:
    rig = _rig(tmp_path)

    rig.launch()

    (prepared,) = rig.terminal.prepared
    assert rig.terminal.started[0]["prepared"] is prepared
    assert prepared.working_dir == rig.worktree


def test_the_agent_is_told_to_read_the_engines_composition(tmp_path: Path) -> None:
    """The one launch-prompt composition (#8141), persisted whole to the run and
    delivered from its file (#9012): the rulings, then the debug context."""
    rig = _rig(tmp_path)

    outcome = rig.launch()

    command = rig.terminal.started[0]["prepared"].launch.text
    prompt = delivered_prompt(command)
    assert prompt.index(_RULING.text) < prompt.index("INTERACTIVE DEBUG SESSION")
    run_dir = rig.run_dir()
    assert run_dir is not None and len(rig.ledger.recorded_runs(123)) == 1
    completion_path = f".issue-orchestrator/sessions/{run_dir.name}/completion-agent_deepseek.json"
    assert "ORCHESTRATOR_SESSION_ID='debug-123'" in command
    assert f"ISSUE_ORCHESTRATOR_RUN_DIR='{run_dir}'" in command
    assert f"ISSUE_ORCHESTRATOR_COMPLETION_PATH='{completion_path}'" in command
    manifest = rig.session_output.read_manifest(run_dir)
    assert manifest is not None and manifest["completion_path"] == completion_path
    assert outcome.agent_label == "agent:deepseek"
    assert [event.data["session_name"] for event in rig.events.named(EventName.SESSION_LAUNCHED)] == ["debug-123"]


def test_an_exact_command_past_the_os_limit_is_refused_before_the_session_starts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#9012 review r1 F2/F3: the terminal copies the inherited PATH into what
    it execs, so the debug launch is held to the exact exec argument too. Over
    the limit, nothing is started and the run is never bound to a completion."""
    rig = _rig(tmp_path)
    monkeypatch.setenv("PATH", "/usr/bin:" + "/opt/it's" * 20_000)

    outcome = rig.launch()

    assert outcome.status is DebugSessionStatus.COMMAND_TOO_LONG and "OS's" in outcome.reason
    assert rig.terminal.started == []
    run_dir = rig.run_dir()
    assert run_dir is not None
    assert "completion_path" not in (rig.session_output.read_manifest(run_dir) or {})
    assert rig.events.named(EventName.SESSION_LAUNCHED) == []


def test_a_cached_issue_is_not_read_again_from_the_host(tmp_path: Path) -> None:
    rig = _rig(tmp_path)

    assert rig.launch(cached=True).launched

    rig.host.get_issue.assert_not_called()


def test_an_issue_missing_from_the_cache_is_read_from_the_host(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    rig.host.get_issue.return_value = rig.issue

    assert rig.launch(cached=False).launched

    rig.host.get_issue.assert_called_once_with(123)


def test_the_request_carries_the_queued_issue_and_the_issues_worktree(tmp_path: Path) -> None:
    """The facade lends its queue cache; the owner picks the issue out of it."""
    config = Config(repo="example/repo", repo_root=tmp_path)
    queued = Issue(number=123, title="Queued", labels=["agent:deepseek"], repo="example/repo")
    neighbour = Issue(number=7, title="Neighbour", labels=["agent:deepseek"], repo="example/repo")

    request = debug_session_request(config, 123, [neighbour, queued])

    assert request == DebugSessionRequest(123, get_worktree_path(config, 123), queued)


def test_an_issue_off_the_queue_is_requested_without_a_cached_copy(tmp_path: Path) -> None:
    """A cache miss leaves the launch to read the issue from the host."""
    config = Config(repo="example/repo", repo_root=tmp_path)
    neighbour = Issue(number=7, title="Neighbour", labels=["agent:deepseek"], repo="example/repo")

    request = debug_session_request(config, 123, [neighbour])

    assert request == DebugSessionRequest(123, get_worktree_path(config, 123), None)


def test_an_issue_the_host_cannot_read_is_not_found(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    rig.host.get_issue.side_effect = RuntimeError("GitHub is down")

    outcome = rig.launch(cached=False)

    assert outcome.status is DebugSessionStatus.ISSUE_NOT_FOUND
    assert rig.terminal.prepared == [] and rig.ledger.recorded_runs(123) == ()


@pytest.mark.parametrize(("arrange", "status"), [
    (lambda rig: rig.worktree.rmdir(), DebugSessionStatus.WORKTREE_MISSING),
    (lambda rig: rig.issue.labels.clear(), DebugSessionStatus.NO_AGENT_LABEL),
    (lambda rig: rig.issue.labels.__setitem__(0, "agent:unknown"), DebugSessionStatus.NO_AGENT_CONFIG),
], ids=["worktree-missing", "no-agent-label", "no-agent-config"])
def test_a_launch_that_cannot_proceed_allocates_nothing(tmp_path: Path, arrange, status) -> None:
    rig = _rig(tmp_path)
    arrange(rig)

    outcome = rig.launch()

    assert outcome.status is status
    assert rig.terminal.prepared == [] and rig.ledger.recorded_runs(123) == ()


def test_a_second_debug_session_is_refused_while_the_first_runs(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    assert rig.launch().launched

    outcome = rig.launch()

    assert outcome.status is DebugSessionStatus.ALREADY_RUNNING
    assert len(rig.terminal.started) == 1 and len(rig.ledger.recorded_runs(123)) == 1


def test_an_unpublished_callback_endpoint_defers_like_every_launch(tmp_path: Path) -> None:
    rig = _rig(tmp_path, endpoint=RuntimeAgentCallbackEndpoint())

    outcome = rig.launch()

    assert outcome.status is DebugSessionStatus.UNAVAILABLE and outcome.reason
    assert rig.terminal.prepared == [] and rig.ledger.recorded_runs(123) == ()


def test_an_unreadable_launch_prompt_input_refuses_the_launch(tmp_path: Path) -> None:
    """#8141: a required input (a ruling, the coder addendum) that cannot be read refuses."""
    launch_prompt = MagicMock()
    launch_prompt.prepare.return_value = LaunchPromptUnavailable("instructions unreadable")
    rig = _rig(tmp_path, launch_prompt=launch_prompt)

    outcome = rig.launch()

    assert (outcome.status, outcome.reason) == (DebugSessionStatus.UNAVAILABLE, "instructions unreadable")
    assert rig.terminal.prepared == [] and rig.ledger.recorded_runs(123) == ()


def test_a_run_the_ledger_cannot_record_is_never_started(tmp_path: Path) -> None:
    ledger = MagicMock(spec=MemoryIssueRunLedger)
    ledger.record_run.side_effect = IssueRunEvidenceUnavailable("disk full")
    rig = _rig(tmp_path, ledger=ledger)

    outcome = rig.launch()

    assert (outcome.status, outcome.reason) == (DebugSessionStatus.UNAVAILABLE, "disk full")
    assert rig.terminal.prepared == [] and rig.terminal.started == []


def test_a_terminal_that_never_comes_up_is_reported(tmp_path: Path) -> None:
    rig = _rig(tmp_path)
    rig.terminal.refuse_start = True

    outcome = rig.launch()

    assert outcome.status is DebugSessionStatus.SPAWN_FAILED
    assert rig.events.named(EventName.SESSION_LAUNCHED) == []
