"""Tests for review exchange loop behaviors."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from issue_orchestrator.control.review_exchange_loop import (
    REVIEW_RESPONSE_FILENAME,
    _parse_exchange_response,
    _run_agent_round,
    _run_interactive_round,
    _resolve_provider,
    run_review_exchange_loop,
)
from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.infra.env import ENV_PREFIX


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


def test_run_agent_round_prefers_provider_mode_when_ai_system_is_provider(tmp_path: Path) -> None:
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
    # Interactive mode: no -p flag, but claude executable and permission mode present
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
    """Tests for _run_interactive_round — the PTY-stdin prompt delivery path."""

    @pytest.fixture(autouse=True)
    def _no_sleep(self, monkeypatch):
        monkeypatch.setattr("issue_orchestrator.control.review_exchange_loop.time.sleep", lambda _: None)

    def test_interactive_round_writes_response(self, tmp_path: Path) -> None:
        """Interactive round starts session, sends prompt, reads response file."""
        from issue_orchestrator.execution.agent_runner_types import AgentSpec

        round_dir = tmp_path / "round"
        round_dir.mkdir()
        response_file = tmp_path / "review-response.json"

        spec = AgentSpec(
            command=["claude"],
            working_dir=tmp_path,
            timeout_seconds=30,
            log_path=round_dir / "agent.log",
            output_dir=round_dir,
        )

        class MockSession:
            def __init__(self):
                self.sent: list[str] = []
                self._alive = True
                self._killed = False

            def send(self, text: str) -> bool:
                self.sent.append(text)
                # Simulate agent writing response on receiving prompt
                response_file.write_text(
                    '{"response_type":"ok","response_text":"approved"}',
                    encoding="utf-8",
                )
                return True

            def is_alive(self) -> bool:
                return self._alive

            def kill(self) -> None:
                self._alive = False
                self._killed = True

            def wait(self, timeout=None):
                return SimpleNamespace(
                    exit_code=0,
                    timed_out=False,
                    duration_seconds=1.0,
                    stderr="",
                    succeeded=True,
                    command=["claude"],
                )

        mock_session = MockSession()

        class MockRunner:
            def start(self, _spec):
                return mock_session

        result = _run_interactive_round(
            MockRunner(),
            spec,
            "Review this code",
            response_file,
        )

        assert mock_session.sent == ["Review this code"]
        assert mock_session._killed
        assert response_file.exists()

    def test_interactive_nonzero_exit_succeeds_when_response_file_present(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        """Killed interactive session returns non-zero exit, but response file is valid.

        This is the normal case: the orchestrator kills the TUI after the agent
        writes the response file, producing a non-zero exit code. The round
        should succeed because the response file is present and parseable.
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

        class MockSession:
            def __init__(self):
                self.sent: list[str] = []

            def send(self, text: str) -> bool:
                self.sent.append(text)
                # Agent writes response before being killed
                response_file.write_text(
                    '{"response_type":"ok","response_text":"approved"}',
                    encoding="utf-8",
                )
                return True

            def is_alive(self) -> bool:
                return False  # Already exited after writing

            def kill(self) -> None:
                pass

            def wait(self, timeout=None):
                # Non-zero exit from kill — this is expected
                return SimpleNamespace(
                    exit_code=-9,
                    timed_out=False,
                    duration_seconds=2.0,
                    stderr="",
                    succeeded=False,
                    command=["claude"],
                )

        class MockRunner:
            def start(self, _spec):
                return MockSession()

        prompt_path = tmp_path / "prompt.md"
        prompt_path.write_text("Prompt")
        agent = AgentConfig(prompt_path=prompt_path, ai_system="claude-code")

        response = _run_agent_round(
            runner=MockRunner(),
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
