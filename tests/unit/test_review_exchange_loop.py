"""Tests for review exchange loop behaviors."""

from __future__ import annotations

from pathlib import Path
import base64
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.review_exchange_loop import (
    REVIEW_RESPONSE_FILENAME,
    _build_env_overrides,
    _is_interactive_provider,
    _parse_exchange_response,
    _resolve_provider,
    _run_agent_round,
    _run_interactive_round,
    run_review_exchange_loop,
)
from issue_orchestrator.control.isolation import GRADLE_USER_HOME_ENV, get_gradle_user_home
from issue_orchestrator.events import EventContext, EventName
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.infra.env import ENV_PREFIX
from issue_orchestrator.ports import TraceEvent


class _CollectingEventSink:
    """Minimal EventSink that records every published event for assertions."""

    def __init__(self) -> None:
        self.events: list[TraceEvent] = []

    def publish(self, event: TraceEvent) -> None:
        self.events.append(event)


@pytest.fixture(autouse=True)
def _force_non_interactive(monkeypatch):
    """Existing tests use DummyRunner.run(). Force non-interactive path."""
    monkeypatch.setattr(
        "issue_orchestrator.control.review_exchange_loop._is_interactive_provider",
        lambda _config: False,
    )


def _write_response_file(spec, json_text: str) -> None:
    """Simulate an agent writing its response to the review response file."""
    response_path = spec.env_overrides.get(f"{ENV_PREFIX}REVIEW_RESPONSE_FILE")
    if response_path:
        Path(response_path).parent.mkdir(parents=True, exist_ok=True)
        Path(response_path).write_text(json_text, encoding="utf-8")


def test_review_exchange_env_overrides_include_per_worktree_gradle_home(tmp_path: Path) -> None:
    """Review exchange agent runner env should use the worktree-local Gradle registry."""
    worktree = tmp_path / "worktree"
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-1"

    env = _build_env_overrides(
        run_dir,
        worktree_path=worktree,
        role="reviewer",
        agent_label="agent:reviewer",
        web_port=None,
        issue_number=4057,
        session_name="review-exchange-1",
    )

    assert env[GRADLE_USER_HOME_ENV] == str(get_gradle_user_home(worktree))


def test_agent_round_returns_error_on_nonzero_exit(tmp_path: Path) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Prompt")

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-1"
    run_dir.mkdir(parents=True)
    exchange_dir = run_dir / "review-exchange"
    exchange_dir.mkdir()

    agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")

    class DummyRunner:
        def run(self, _spec):
            return SimpleNamespace(
                succeeded=False,
                exit_code=2,
                timed_out=False,
                stderr="boom",
            )

    response = _run_agent_round(
        session_output=FileSystemSessionOutput(),
        runner=DummyRunner(),
        worktree_path=worktree,
        run_dir=run_dir,
        exchange_dir=exchange_dir,
        round_index=1,
        issue_number=1,
        issue_title="Test",
        session_name="review-exchange-1",
        agent=agent,
        role="reviewer",
        agent_label="agent:reviewer",
        prompt_text="Review prompt",
        web_port=None,
    )

    assert response.response_type == "error"
    assert "exit_code=2" in response.response_text


def test_resolve_provider_uses_ai_system_when_provider_missing(tmp_path: Path) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Prompt")
    agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")
    assert _resolve_provider(agent) == "claude-code"


def test_run_agent_round_uses_provider_mode_when_ai_system_is_provider(tmp_path: Path) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Prompt")

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-1"
    run_dir.mkdir(parents=True)
    exchange_dir = run_dir / "review-exchange"
    exchange_dir.mkdir()

    agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")
    seen_command: list[str] = []

    class DummyRunner:
        def run(self, spec):
            nonlocal seen_command
            seen_command = spec.command
            _write_response_file(spec, '{"response_type":"ok","response_text":"looks good"}\n')
            return SimpleNamespace(
                succeeded=True,
                exit_code=0,
                timed_out=False,
                stderr="",
            )

    response = _run_agent_round(
        session_output=FileSystemSessionOutput(),
        runner=DummyRunner(),
        worktree_path=worktree,
        run_dir=run_dir,
        exchange_dir=exchange_dir,
        round_index=1,
        issue_number=1,
        issue_title="Test",
        session_name="review-exchange-1",
        agent=agent,
        role="reviewer",
        agent_label="agent:reviewer",
        prompt_text="Review prompt",
        web_port=None,
    )

    assert response.response_type == "ok"
    # Provider mode: no -p flag, but claude executable and permission mode present
    assert "claude" in seen_command
    assert "--permission-mode" in seen_command


def test_run_agent_round_writes_run_scoped_provider_runner_logs(tmp_path: Path) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Prompt")

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-1"
    run_dir.mkdir(parents=True)
    exchange_dir = run_dir / "review-exchange"
    exchange_dir.mkdir()

    agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")

    class DummyRunner:
        def run(self, spec):
            _write_response_file(spec, '{"response_type":"ok","response_text":"looks good"}\n')
            return SimpleNamespace(
                succeeded=True,
                exit_code=0,
                timed_out=False,
                stderr="runner-note",
            )

    response = _run_agent_round(
        session_output=FileSystemSessionOutput(),
        runner=DummyRunner(),
        worktree_path=worktree,
        run_dir=run_dir,
        exchange_dir=exchange_dir,
        round_index=1,
        issue_number=1,
        issue_title="Test",
        session_name="review-exchange-1",
        agent=agent,
        role="reviewer",
        agent_label="agent:reviewer",
        prompt_text="Review prompt",
        web_port=None,
    )

    assert response.response_type == "ok"
    stdout_log = run_dir / "provider-runner" / "stdout.log"
    stderr_log = run_dir / "provider-runner" / "stderr.log"
    assert stdout_log.exists()
    assert stderr_log.exists()
    assert "round=1 role=reviewer" in stdout_log.read_text(encoding="utf-8")
    assert '"response_type":"ok"' in stdout_log.read_text(encoding="utf-8")
    assert "runner-note" in stderr_log.read_text(encoding="utf-8")


def test_run_agent_round_writes_clean_ui_session_log(tmp_path: Path) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Prompt")

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-1"
    run_dir.mkdir(parents=True)
    exchange_dir = run_dir / "review-exchange"
    exchange_dir.mkdir()

    agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")

    class DummyRunner:
        def run(self, spec):
            _write_response_file(
                spec,
                '{"response_type":"changes_requested","response_text":"Line one\\n✶ Thinking…\\nLine two"}\n',
            )
            return SimpleNamespace(
                succeeded=True,
                exit_code=0,
                timed_out=False,
                stderr="Recentactivity\nrunner-note",
            )

    response = _run_agent_round(
        session_output=FileSystemSessionOutput(),
        runner=DummyRunner(),
        worktree_path=worktree,
        run_dir=run_dir,
        exchange_dir=exchange_dir,
        round_index=1,
        issue_number=1,
        issue_title="Test",
        session_name="review-exchange-1",
        agent=agent,
        role="reviewer",
        agent_label="agent:reviewer",
        prompt_text="Review prompt\n✶ Thinking…",
        web_port=None,
    )

    assert response.response_type == "changes_requested"
    events = [
        json.loads(line)
        for line in (run_dir / "terminal-recording.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    content = "".join(
        base64.b64decode(event["data_b64"]).decode("utf-8", errors="ignore")
        for event in events
        if event.get("event_type") == "output" and event.get("data_b64")
    )
    transcript = (run_dir / "review-exchange" / "transcript.log").read_text(encoding="utf-8")
    assert content == ""
    assert "Review prompt" in transcript
    assert "runner-note" in transcript
    assert "Thinking" not in transcript
    assert "Recentactivity" not in transcript


def test_run_agent_round_calls_prompt_ready_after_transcript_is_written(tmp_path: Path) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Prompt")

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-1"
    run_dir.mkdir(parents=True)
    exchange_dir = run_dir / "review-exchange"
    exchange_dir.mkdir()

    agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")
    snapshot: dict[str, str | bool] = {}

    class DummyRunner:
        def run(self, spec):
            _write_response_file(spec, '{"response_type":"ok","response_text":"looks good"}\n')
            return SimpleNamespace(
                succeeded=True,
                exit_code=0,
                timed_out=False,
                stderr="",
            )

    def _on_prompt_ready() -> None:
        transcript = run_dir / "review-exchange" / "transcript.log"
        snapshot["exists"] = transcript.exists()
        snapshot["content"] = transcript.read_text(encoding="utf-8") if transcript.exists() else ""

    response = _run_agent_round(
        session_output=FileSystemSessionOutput(),
        runner=DummyRunner(),
        worktree_path=worktree,
        run_dir=run_dir,
        exchange_dir=exchange_dir,
        round_index=1,
        issue_number=1,
        issue_title="Test",
        session_name="review-exchange-1",
        agent=agent,
        role="reviewer",
        agent_label="agent:reviewer",
        prompt_text="Review prompt",
        web_port=None,
        on_prompt_ready=_on_prompt_ready,
    )

    assert response.response_type == "ok"
    assert snapshot["exists"] is True
    assert "Review prompt" in str(snapshot["content"])


def test_review_exchange_run_manifest_includes_claude_log_dir(tmp_path: Path, monkeypatch) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Prompt")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-exchange"
    run_dir.mkdir(parents=True)

    session_output = MagicMock()
    session_output.start_run.return_value = SimpleNamespace(run_dir=run_dir, run_id="run-exchange")

    class DummyRunner:
        def run(self, spec):
            _write_response_file(spec, '{"response_type":"ok","response_text":"looks good"}\n')
            return SimpleNamespace(
                succeeded=True,
                exit_code=0,
                timed_out=False,
                stderr="",
            )

    monkeypatch.setattr("issue_orchestrator.control.review_exchange_loop.AgentRunner", DummyRunner)

    coder_agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")
    reviewer_agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")

    outcome = run_review_exchange_loop(
        session_output=session_output,
        worktree_path=worktree,
        issue_number=4057,
        issue_title="Test",
        coder_label="agent:backend",
        reviewer_label="agent:reviewer",
        coder_agent=coder_agent,
        reviewer_agent=reviewer_agent,
        max_rounds=1,
        max_no_progress=1,
        require_validation=False,
        web_port=None,
    )

    assert outcome.status == "ok"
    kwargs = session_output.start_run.call_args.kwargs
    assert kwargs["claude_log_dir"].endswith("-worktree")
    assert kwargs["orchestrator_log"].endswith(".issue-orchestrator/state/logs/orchestrator.log")


def test_review_exchange_retries_coder_when_completion_artifact_missing(tmp_path: Path, monkeypatch) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Prompt")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-exchange"
    run_dir.mkdir(parents=True)

    session_output = MagicMock()
    session_output.start_run.return_value = SimpleNamespace(run_dir=run_dir, run_id="run-exchange")

    class DummyRunner:
        def __init__(self) -> None:
            self.coder_calls = 0

        def run(self, spec):
            role = Path(spec.output_dir).name
            if role == "reviewer":
                _write_response_file(spec, '{"response_type":"changes_requested","getting_closer":true,"response_text":"fix"}\n')
                return SimpleNamespace(
                    succeeded=True,
                    exit_code=0,
                    timed_out=False,
                    stderr="",
                )
            self.coder_calls += 1
            if self.coder_calls == 2:
                (run_dir / "completion-coder.json").write_text("{}", encoding="utf-8")
            _write_response_file(spec, '{"response_type":"ok","response_text":"updated"}\n')
            return SimpleNamespace(
                succeeded=True,
                exit_code=0,
                timed_out=False,
                stderr="",
            )

    runner = DummyRunner()
    monkeypatch.setattr(
        "issue_orchestrator.control.review_exchange_loop.AgentRunner", lambda: runner
    )

    coder_agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")
    reviewer_agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")

    outcome = run_review_exchange_loop(
        session_output=session_output,
        worktree_path=worktree,
        issue_number=4057,
        issue_title="Test",
        coder_label="agent:backend",
        reviewer_label="agent:reviewer",
        coder_agent=coder_agent,
        reviewer_agent=reviewer_agent,
        max_rounds=1,
        max_no_progress=1,
        require_validation=False,
        web_port=None,
    )

    assert runner.coder_calls == 2
    assert outcome.status == "stopped"
    assert outcome.reason == "max_rounds_exceeded"


def test_review_exchange_fails_after_protocol_retries_exhausted(tmp_path: Path, monkeypatch) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Prompt")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-exchange"
    run_dir.mkdir(parents=True)

    session_output = MagicMock()
    session_output.start_run.return_value = SimpleNamespace(run_dir=run_dir, run_id="run-exchange")

    class DummyRunner:
        def __init__(self) -> None:
            self.coder_calls = 0

        def run(self, spec):
            role = Path(spec.output_dir).name
            if role == "reviewer":
                _write_response_file(spec, '{"response_type":"changes_requested","getting_closer":true,"response_text":"fix"}\n')
                return SimpleNamespace(
                    succeeded=True,
                    exit_code=0,
                    timed_out=False,
                    stderr="",
                )
            self.coder_calls += 1
            _write_response_file(spec, '{"response_type":"ok","response_text":"updated"}\n')
            return SimpleNamespace(
                succeeded=True,
                exit_code=0,
                timed_out=False,
                stderr="",
            )

    runner = DummyRunner()
    monkeypatch.setattr(
        "issue_orchestrator.control.review_exchange_loop.AgentRunner", lambda: runner
    )

    coder_agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")
    reviewer_agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")

    outcome = run_review_exchange_loop(
        session_output=session_output,
        worktree_path=worktree,
        issue_number=4057,
        issue_title="Test",
        coder_label="agent:backend",
        reviewer_label="agent:reviewer",
        coder_agent=coder_agent,
        reviewer_agent=reviewer_agent,
        max_rounds=1,
        max_no_progress=1,
        require_validation=False,
        web_port=None,
    )

    assert runner.coder_calls == 3
    assert outcome.status == "error"
    assert outcome.reason == "coder_protocol_violation"


def test_review_exchange_retries_when_validation_artifact_missing(tmp_path: Path, monkeypatch) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Prompt")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-exchange"
    run_dir.mkdir(parents=True)

    session_output = MagicMock()
    session_output.start_run.return_value = SimpleNamespace(run_dir=run_dir, run_id="run-exchange")

    class DummyRunner:
        def __init__(self) -> None:
            self.coder_calls = 0

        def run(self, spec):
            role = Path(spec.output_dir).name
            if role == "reviewer":
                _write_response_file(spec, '{"response_type":"changes_requested","getting_closer":true,"response_text":"fix"}')
                return SimpleNamespace(
                    succeeded=True,
                    exit_code=0,
                    timed_out=False,
                    stderr="",
                )
            self.coder_calls += 1
            # First coder pass writes completion only; second pass writes validation too.
            (run_dir / "completion-coder.json").write_text("{}", encoding="utf-8")
            if self.coder_calls == 2:
                (run_dir / "validation-record.json").write_text(
                    '{"passed": true}',
                    encoding="utf-8",
                )
            return SimpleNamespace(
                succeeded=True,
                exit_code=0,
                timed_out=False,
                stderr="",
            )

    runner = DummyRunner()
    monkeypatch.setattr(
        "issue_orchestrator.control.review_exchange_loop.AgentRunner", lambda: runner
    )

    coder_agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")
    reviewer_agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")

    outcome = run_review_exchange_loop(
        session_output=session_output,
        worktree_path=worktree,
        issue_number=4057,
        issue_title="Test",
        coder_label="agent:backend",
        reviewer_label="agent:reviewer",
        coder_agent=coder_agent,
        reviewer_agent=reviewer_agent,
        max_rounds=1,
        max_no_progress=1,
        require_validation=True,
        web_port=None,
    )

    assert runner.coder_calls == 2
    assert outcome.status == "stopped"
    assert outcome.reason == "max_rounds_exceeded"


def test_review_exchange_calls_on_started_before_agent_rounds(tmp_path: Path, monkeypatch) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Prompt")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-exchange"
    run_dir.mkdir(parents=True)

    session_output = MagicMock()
    session_output.start_run.return_value = SimpleNamespace(run_dir=run_dir, run_id="run-exchange")

    callback_state = {"started": False, "run_dir": None}

    def _on_started(path: Path) -> None:
        callback_state["started"] = True
        callback_state["run_dir"] = path

    class DummyRunner:
        def run(self, spec):
            assert callback_state["started"] is True
            assert callback_state["run_dir"] == run_dir
            role = Path(spec.output_dir).name
            if role == "reviewer":
                _write_response_file(spec, '{"response_type":"ok","getting_closer":true,"response_text":"approved"}')
                return SimpleNamespace(
                    succeeded=True,
                    exit_code=0,
                    timed_out=False,
                    stderr="",
                )
            return SimpleNamespace(
                succeeded=True,
                exit_code=0,
                timed_out=False,
                stderr="",
            )

    monkeypatch.setattr("issue_orchestrator.control.review_exchange_loop.AgentRunner", DummyRunner)

    coder_agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")
    reviewer_agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")

    outcome = run_review_exchange_loop(
        session_output=session_output,
        worktree_path=worktree,
        issue_number=4057,
        issue_title="Test",
        coder_label="agent:backend",
        reviewer_label="agent:reviewer",
        coder_agent=coder_agent,
        reviewer_agent=reviewer_agent,
        max_rounds=1,
        max_no_progress=1,
        require_validation=False,
        web_port=None,
        on_started=_on_started,
    )

    assert callback_state["started"] is True
    assert callback_state["run_dir"] == run_dir
    assert outcome.status == "ok"


def test_parse_exchange_response_accepts_embedded_json_from_claude_result_event() -> None:
    stdout = (
        '{"type":"stream_event","event":{"type":"message_stop"}}\n'
        '{"type":"result","subtype":"success","result":"The validation record is missing.\\n\\n'
        '{\\"response_type\\":\\"changes_requested\\",\\"getting_closer\\":true,'
        '\\"response_text\\":\\"Run make validate via coding-done.\\"}"}\n'
    )

    response = _parse_exchange_response(stdout)

    assert response is not None
    assert response.response_type == "changes_requested"
    assert response.response_text == "Run make validate via coding-done."
    assert response.getting_closer is True


def test_parse_exchange_response_prefers_last_protocol_json_in_embedded_text() -> None:
    stdout = (
        '{"type":"result","subtype":"success","result":"First attempt: '
        '{\\"response_type\\":\\"disagree\\",\\"response_text\\":\\"old\\"} '
        'final: {\\"response_type\\":\\"ok\\",\\"response_text\\":\\"done\\"}"}\n'
    )

    response = _parse_exchange_response(stdout)

    assert response is not None
    assert response.response_type == "ok"
    assert response.response_text == "done"


def test_parse_exchange_response_repairs_multiline_response_text() -> None:
    stdout = (
        '{"response_type":"changes_requested","getting_closer":true,'
        '"response_text":"Three issues to fix before approval:\n'
        '1. Add the missing UI rendering.\n'
        '2. Tighten the public contract typing.\n'
        '3. Remove the fail-soft exception handler."}\n'
    )

    response = _parse_exchange_response(stdout)

    assert response is not None
    assert response.response_type == "changes_requested"
    assert response.getting_closer is True
    assert "Three issues to fix before approval" in response.response_text
    assert "Add the missing UI rendering." in response.response_text


def test_parse_exchange_response_repairs_tab_characters_inside_response_text() -> None:
    stdout = (
        '{"response_type":"changes_requested","getting_closer":true,'
        '"response_text":"Checklist:\n\t1. Add the UI.\n\t2. Tighten the contract."}\n'
    )

    response = _parse_exchange_response(stdout)

    assert response is not None
    assert response.response_type == "changes_requested"
    assert "\t1. Add the UI." in response.response_text


def test_review_exchange_emits_role_events_from_active_path(
    tmp_path: Path, monkeypatch,
) -> None:
    """The active control-layer path must emit ROLE_PROMPTED + ROLE_FEEDBACK
    for both reviewer and coder, alongside the existing ROUND events.

    Regression for the PR 6138 review finding: events were only emitted from
    ``execution/review_exchange_local_loop.py`` (a path used only by unit
    tests) so production review exchanges shipped role-blind timelines.
    """
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Prompt")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-exchange"
    run_dir.mkdir(parents=True)

    session_output = MagicMock()
    session_output.start_run.return_value = SimpleNamespace(
        run_dir=run_dir, run_id="run-exchange",
    )

    class DummyRunner:
        def run(self, spec):
            role = Path(spec.output_dir).name
            if role == "reviewer":
                _write_response_file(
                    spec,
                    '{"response_type":"changes_requested","getting_closer":true,'
                    '"response_text":"fix the typo"}\n',
                )
            else:  # coder
                _write_response_file(
                    spec,
                    '{"response_type":"ok","response_text":"applied"}\n',
                )
                (run_dir / "completion-coder.json").write_text("{}", encoding="utf-8")
            return SimpleNamespace(succeeded=True, exit_code=0, timed_out=False, stderr="")

    monkeypatch.setattr(
        "issue_orchestrator.control.review_exchange_loop.AgentRunner", DummyRunner,
    )

    sink = _CollectingEventSink()
    ctx = EventContext()

    coder_agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")
    reviewer_agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")

    run_review_exchange_loop(
        session_output=session_output,
        worktree_path=worktree,
        issue_number=4057,
        issue_title="Test",
        coder_label="agent:backend",
        reviewer_label="agent:reviewer",
        coder_agent=coder_agent,
        reviewer_agent=reviewer_agent,
        max_rounds=1,
        max_no_progress=2,
        require_validation=False,
        web_port=None,
        events=sink,
        event_context=ctx,
    )

    # Sequence within round 1: reviewer prompted → reviewer feedback →
    # coder prompted → coder feedback. ROUND_STARTED still fires alongside
    # the reviewer ROLE_PROMPTED.
    role_events = [
        (event.event_type.value, event.data.get("role"), event.data.get("response_type"))
        for event in sink.events
        if event.event_type in (
            EventName.REVIEW_EXCHANGE_ROLE_PROMPTED,
            EventName.REVIEW_EXCHANGE_ROLE_FEEDBACK,
        )
    ]
    assert role_events == [
        ("review_exchange.role_prompted", "reviewer", None),
        ("review_exchange.role_feedback", "reviewer", "changes_requested"),
        ("review_exchange.role_prompted", "coder", None),
        ("review_exchange.role_feedback", "coder", "ok"),
    ]
    # The existing round event must still fire — we are adding events,
    # not replacing.
    assert any(
        event.event_type == EventName.REVIEW_EXCHANGE_ROUND_STARTED
        for event in sink.events
    )


def test_review_exchange_emits_role_timeout_when_coder_protocol_fails(
    tmp_path: Path, monkeypatch,
) -> None:
    """Coder protocol_error path must emit ROLE_TIMEOUT(coder) before bailing out.

    Regression for the PR 6138 finding: the active path had no per-role
    timeout signal, so failures looked indistinguishable from successful
    rounds in the timeline.
    """
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Prompt")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-exchange"
    run_dir.mkdir(parents=True)

    session_output = MagicMock()
    session_output.start_run.return_value = SimpleNamespace(
        run_dir=run_dir, run_id="run-exchange",
    )

    class DummyRunner:
        def run(self, spec):
            role = Path(spec.output_dir).name
            if role == "reviewer":
                _write_response_file(
                    spec,
                    '{"response_type":"changes_requested","getting_closer":true,'
                    '"response_text":"fix"}\n',
                )
                return SimpleNamespace(
                    succeeded=True, exit_code=0, timed_out=False, stderr="",
                )
            # Coder never produces the expected completion file; protocol error.
            _write_response_file(spec, '{"response_type":"ok","response_text":"x"}\n')
            return SimpleNamespace(
                succeeded=True, exit_code=0, timed_out=False, stderr="",
            )

    monkeypatch.setattr(
        "issue_orchestrator.control.review_exchange_loop.AgentRunner", DummyRunner,
    )

    sink = _CollectingEventSink()
    ctx = EventContext()
    coder_agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")
    reviewer_agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")

    run_review_exchange_loop(
        session_output=session_output,
        worktree_path=worktree,
        issue_number=4057,
        issue_title="Test",
        coder_label="agent:backend",
        reviewer_label="agent:reviewer",
        coder_agent=coder_agent,
        reviewer_agent=reviewer_agent,
        max_rounds=1,
        max_no_progress=2,
        require_validation=False,
        web_port=None,
        events=sink,
        event_context=ctx,
    )

    timeout_events = [
        event for event in sink.events
        if event.event_type is EventName.REVIEW_EXCHANGE_ROLE_TIMEOUT
    ]
    assert len(timeout_events) == 1
    payload = timeout_events[0].data
    assert payload["role"] == "coder"
    assert payload["reason"] == "protocol_error"
    assert payload["round_index"] == 1


def test_review_exchange_seeds_initial_validation_record(tmp_path: Path, monkeypatch) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Prompt")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-exchange"
    run_dir.mkdir(parents=True)
    validation_src = tmp_path / "validation-record.json"
    validation_src.write_text(json.dumps({"passed": True}), encoding="utf-8")

    session_output = MagicMock()
    session_output.start_run.return_value = SimpleNamespace(run_dir=run_dir, run_id="run-exchange")

    class DummyRunner:
        def run(self, spec):
            role = Path(spec.output_dir).name
            if role == "reviewer":
                # Validation should already be present in the review-exchange run dir.
                assert (run_dir / "validation-record.json").exists()
                _write_response_file(spec, '{"response_type":"ok","getting_closer":true,"response_text":"approved"}')
                return SimpleNamespace(
                    succeeded=True,
                    exit_code=0,
                    timed_out=False,
                    stderr="",
                )
            return SimpleNamespace(
                succeeded=True,
                exit_code=0,
                timed_out=False,
                stderr="",
            )

    monkeypatch.setattr("issue_orchestrator.control.review_exchange_loop.AgentRunner", DummyRunner)

    coder_agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")
    reviewer_agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")

    outcome = run_review_exchange_loop(
        session_output=session_output,
        worktree_path=worktree,
        issue_number=4057,
        issue_title="Test",
        coder_label="agent:backend",
        reviewer_label="agent:reviewer",
        coder_agent=coder_agent,
        reviewer_agent=reviewer_agent,
        max_rounds=1,
        max_no_progress=1,
        require_validation=True,
        initial_validation_record_path=validation_src,
        web_port=None,
    )

    assert outcome.status == "ok"


class TestInteractiveRound:
    """Tests for _run_interactive_round — subprocess-based poll-and-kill path."""

    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch):
        monkeypatch.setattr("issue_orchestrator.execution.interactive_round.time.sleep", lambda _: None)

    def test_interactive_round_polls_response_file(self, tmp_path: Path, monkeypatch) -> None:
        """Interactive round starts subprocess, polls for response file, kills."""
        from issue_orchestrator.execution.agent_runner_types import AgentSpec

        round_dir = tmp_path / "round"
        round_dir.mkdir()
        response_file = tmp_path / "review-response.json"

        spec = AgentSpec(
            command=["claude", "do stuff"],
            working_dir=tmp_path,
            timeout_seconds=30,
            log_path=round_dir / "agent.log",
            output_dir=round_dir,
        )

        poll_count = 0

        class FakeProc:
            pid = 12345
            returncode = -9

            def poll(self):
                nonlocal poll_count
                poll_count += 1
                if poll_count >= 2:
                    response_file.write_text(
                        '{"response_type":"ok","response_text":"approved"}',
                        encoding="utf-8",
                    )
                return None  # still running

            def wait(self, timeout=None):
                pass

        fake_proc = FakeProc()
        monkeypatch.setattr(
            "issue_orchestrator.execution.interactive_round.subprocess.Popen",
            lambda *a, **kw: fake_proc,
        )
        monkeypatch.setattr("issue_orchestrator.execution.interactive_round.os.getpgid", lambda pid: pid)
        monkeypatch.setattr("issue_orchestrator.execution.interactive_round.os.killpg", lambda pgid, sig: None)

        # _run_interactive_round delegates to runner.run_interactive
        runner = MagicMock()
        from issue_orchestrator.execution.interactive_round import run_interactive_round
        runner.run_interactive.side_effect = lambda s, rf: run_interactive_round(s, rf)

        result = _run_interactive_round(runner, spec, response_file)

        assert response_file.exists()
        assert result.exit_code == -9

    def test_interactive_round_writes_review_output_to_terminal_recording(self, tmp_path: Path) -> None:
        from issue_orchestrator.execution.agent_runner_types import AgentSpec
        from issue_orchestrator.execution.interactive_round import run_interactive_round

        round_dir = tmp_path / "round"
        round_dir.mkdir()
        aggregate_recording = tmp_path / "terminal-recording.jsonl"
        response_file = tmp_path / "review-response.json"
        command = [
            "python3",
            "-c",
            (
                "from pathlib import Path; "
                "import time; "
                "print('review-hi', flush=True); "
                f"Path({str(response_file)!r}).write_text("
                "'{\"response_type\":\"ok\",\"response_text\":\"approved\"}',"
                " encoding='utf-8'); "
                "time.sleep(5)"
            ),
        ]
        spec = AgentSpec(
            command=command,
            working_dir=tmp_path,
            timeout_seconds=10,
            log_path=round_dir / "terminal-recording.jsonl",
            additional_recording_paths=[aggregate_recording],
            mirror_log_path=round_dir / "agent-output.log",
            output_dir=round_dir,
        )

        result = run_interactive_round(spec, response_file)

        assert response_file.exists()
        assert result.timed_out is False
        events = [
            json.loads(line)
            for line in spec.log_path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        content = "".join(
            base64.b64decode(event["data_b64"]).decode("utf-8", errors="ignore")
            for event in events
            if event.get("event_type") == "output" and event.get("data_b64")
        )
        assert "review-hi" in content
        assert "review-hi" in spec.mirror_log_path.read_text(encoding="utf-8")
        aggregate_events = [
            json.loads(line)
            for line in aggregate_recording.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
        aggregate_content = "".join(
            base64.b64decode(event["data_b64"]).decode("utf-8", errors="ignore")
            for event in aggregate_events
            if event.get("event_type") == "output" and event.get("data_b64")
        )
        assert "review-hi" in aggregate_content

    def test_interactive_nonzero_exit_succeeds_when_response_file_present(
        self, tmp_path: Path, monkeypatch,
    ) -> None:
        """Killed interactive session returns non-zero exit, but response file is valid.

        The orchestrator kills the TUI after the response file appears,
        producing a non-zero exit code.  The round should succeed because
        the response file is present and parseable.
        """
        monkeypatch.setattr(
            "issue_orchestrator.control.review_exchange_loop._is_interactive_provider",
            lambda _config: True,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-1"
        run_dir.mkdir(parents=True)
        exchange_dir = run_dir / "review-exchange"
        exchange_dir.mkdir()

        response_file = run_dir / REVIEW_RESPONSE_FILENAME

        class FakeProc:
            pid = 12345
            returncode = -9

            def poll(self):
                # Write response and report process exited
                response_file.write_text(
                    '{"response_type":"ok","response_text":"approved"}',
                    encoding="utf-8",
                )
                return -9

            def wait(self, timeout=None):
                pass

        from issue_orchestrator.execution.interactive_round import run_interactive_round
        monkeypatch.setattr(
            "issue_orchestrator.execution.interactive_round.subprocess.Popen",
            lambda *a, **kw: FakeProc(),
        )

        prompt_path = tmp_path / "prompt.md"
        prompt_path.write_text("Prompt")
        agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")

        runner = MagicMock()
        runner.run_interactive.side_effect = lambda s, rf: run_interactive_round(s, rf)

        response = _run_agent_round(
            session_output=FileSystemSessionOutput(),
            runner=runner,
            worktree_path=worktree,
            run_dir=run_dir,
            exchange_dir=exchange_dir,
            round_index=1,
            issue_number=1,
            issue_title="Test",
            session_name="review-exchange-1",
            agent=agent,
            role="reviewer",
            agent_label="agent:reviewer",
            prompt_text="Review prompt",
            web_port=None,
        )

        # Should succeed despite non-zero exit code
        assert response.response_type == "ok"
        assert response.response_text == "approved"

    def test_interactive_cleanup_timeout_still_succeeds_when_response_file_present(
        self, tmp_path: Path, monkeypatch, caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Cleanup-timeout after a captured response should not fail the review."""
        monkeypatch.setattr(
            "issue_orchestrator.control.review_exchange_loop._is_interactive_provider",
            lambda _config: True,
        )

        worktree = tmp_path / "worktree"
        worktree.mkdir()
        run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-1"
        run_dir.mkdir(parents=True)
        exchange_dir = run_dir / "review-exchange"
        exchange_dir.mkdir()

        response_file = run_dir / REVIEW_RESPONSE_FILENAME

        class FakeProc:
            pid = 12345
            returncode = None

            def __init__(self) -> None:
                self._response_written = False
                self._wait_calls = 0

            def poll(self):
                if not self._response_written:
                    response_file.write_text(
                        '{"response_type":"ok","response_text":"approved"}',
                        encoding="utf-8",
                    )
                    self._response_written = True
                return None

            def wait(self, timeout=None):
                self._wait_calls += 1
                raise subprocess.TimeoutExpired(["claude"], timeout)

        from issue_orchestrator.execution.interactive_round import run_interactive_round

        monkeypatch.setattr(
            "issue_orchestrator.execution.interactive_round.subprocess.Popen",
            lambda *a, **kw: FakeProc(),
        )
        monkeypatch.setattr("issue_orchestrator.execution.interactive_round.os.getpgid", lambda pid: pid)
        monkeypatch.setattr("issue_orchestrator.execution.interactive_round.os.killpg", lambda pgid, sig: None)

        prompt_path = tmp_path / "prompt.md"
        prompt_path.write_text("Prompt")
        agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")

        runner = MagicMock()
        runner.run_interactive.side_effect = lambda s, rf: run_interactive_round(s, rf)

        with caplog.at_level("WARNING"):
            response = _run_agent_round(
                session_output=FileSystemSessionOutput(),
                runner=runner,
                worktree_path=worktree,
                run_dir=run_dir,
                exchange_dir=exchange_dir,
                round_index=1,
                issue_number=1,
                issue_title="Test",
                session_name="review-exchange-1",
                agent=agent,
                role="reviewer",
                agent_label="agent:reviewer",
                prompt_text="Review prompt",
                web_port=None,
            )

        assert response.response_type == "ok"
        assert response.response_text == "approved"
        assert "preserving captured response" in caplog.text


def test_review_exchange_retries_when_validation_failed(tmp_path: Path, monkeypatch) -> None:
    """Coder round should retry when validation-record.json exists but passed=false."""
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("Prompt")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    run_dir = worktree / ".issue-orchestrator" / "sessions" / "run-exchange"
    run_dir.mkdir(parents=True)

    session_output = MagicMock()
    session_output.start_run.return_value = SimpleNamespace(run_dir=run_dir, run_id="run-exchange")

    class DummyRunner:
        def __init__(self) -> None:
            self.coder_calls = 0

        def run(self, spec):
            role = Path(spec.output_dir).name
            if role == "reviewer":
                _write_response_file(spec, '{"response_type":"changes_requested","getting_closer":true,"response_text":"fix"}')
                return SimpleNamespace(
                    succeeded=True,
                    exit_code=0,
                    timed_out=False,
                    stderr="",
                )
            self.coder_calls += 1
            (run_dir / "completion-coder.json").write_text("{}", encoding="utf-8")
            if self.coder_calls <= 1:
                # First pass: validation exists but failed
                (run_dir / "validation-record.json").write_text(
                    json.dumps({"passed": False, "exit_code": 1}),
                    encoding="utf-8",
                )
            else:
                # Second pass: validation passes
                (run_dir / "validation-record.json").write_text(
                    json.dumps({"passed": True, "exit_code": 0}),
                    encoding="utf-8",
                )
            return SimpleNamespace(
                succeeded=True,
                exit_code=0,
                timed_out=False,
                stderr="",
            )

    runner = DummyRunner()
    monkeypatch.setattr(
        "issue_orchestrator.control.review_exchange_loop.AgentRunner", lambda: runner
    )

    coder_agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")
    reviewer_agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")

    outcome = run_review_exchange_loop(
        session_output=session_output,
        worktree_path=worktree,
        issue_number=4057,
        issue_title="Test",
        coder_label="agent:backend",
        reviewer_label="agent:reviewer",
        coder_agent=coder_agent,
        reviewer_agent=reviewer_agent,
        max_rounds=1,
        max_no_progress=1,
        require_validation=True,
        web_port=None,
    )

    # Coder was called twice: first with failed validation, then retried
    assert runner.coder_calls == 2
    assert outcome.status == "stopped"
    assert outcome.reason == "max_rounds_exceeded"


def test_review_exchange_validation_failure_includes_stderr(tmp_path: Path, monkeypatch) -> None:
    """When validation fails, the retry feedback should include stderr output."""
    from issue_orchestrator.control.review_exchange_loop import _validate_coder_protocol

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "completion-coder.json").write_text("{}", encoding="utf-8")
    stderr_path = run_dir / "validation-stderr.log"
    stderr_path.write_text("error: unused import 'os'\n", encoding="utf-8")
    (run_dir / "validation-record.json").write_text(
        json.dumps({
            "passed": False,
            "exit_code": 1,
            "stderr_path": str(stderr_path),
        }),
        encoding="utf-8",
    )

    error = _validate_coder_protocol(run_dir, require_validation=True)
    assert error is not None
    assert "validation failed" in error
    assert "exit_code=1" in error
    assert "unused import" in error


def test_review_exchange_validation_malformed_stderr_path_no_crash(tmp_path: Path) -> None:
    """Malformed stderr_path (e.g. directory) must not crash — returns error string."""
    from issue_orchestrator.control.review_exchange_loop import _validate_coder_protocol

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "completion-coder.json").write_text("{}", encoding="utf-8")
    # Point stderr_path at a directory instead of a file
    bad_dir = run_dir / "not-a-file"
    bad_dir.mkdir()
    (run_dir / "validation-record.json").write_text(
        json.dumps({
            "passed": False,
            "exit_code": 1,
            "stderr_path": str(bad_dir),
        }),
        encoding="utf-8",
    )

    # Must return error string, not raise
    error = _validate_coder_protocol(run_dir, require_validation=True)
    assert error is not None
    assert "validation failed" in error


def test_review_exchange_validation_passed_returns_none(tmp_path: Path) -> None:
    """When validation passes, _validate_coder_protocol should return None."""
    from issue_orchestrator.control.review_exchange_loop import _validate_coder_protocol

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "completion-coder.json").write_text("{}", encoding="utf-8")
    (run_dir / "validation-record.json").write_text(
        json.dumps({"passed": True, "exit_code": 0}),
        encoding="utf-8",
    )

    error = _validate_coder_protocol(run_dir, require_validation=True)
    assert error is None
