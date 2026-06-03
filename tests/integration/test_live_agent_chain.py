"""Integration test: real Claude through the full live launch chain.

Exercises the EXACT path the orchestrator uses in production:

    AgentRunner.start() → pexpect PTY → bash -lc → provider_runner
        → SubprocessAgentRunner → Popen(/bin/sh -c "claude ...")

This test exists because unit tests and mocks repeatedly passed while
the live system failed.  We need at least one test that proves the full
chain produces output and exits cleanly with a real Claude process.

Requires: Claude CLI installed and authenticated (skips otherwise).
"""

from __future__ import annotations

import base64
import json
import os
import shlex
import shutil
import subprocess
import tempfile
import threading
import time
from pathlib import Path

import pytest

from issue_orchestrator.execution.agent_runner import AgentRunner
from issue_orchestrator.execution.agent_runner_types import AgentSpec
from issue_orchestrator.execution.persistent_round_runner import (
    close_persistent_session,
    open_persistent_session,
    send_round,
)


def _decoded_output(path: Path) -> str:
    """Decode the base64-JSONL terminal recording into raw stdout text.

    ``AgentRunner`` records session output as JSONL events with the payload
    base64-encoded under ``data_b64``. Assertions over the live process
    stdout must decode those payloads; substring checks against the raw
    JSONL bytes would silently miss matches.
    """
    if not path.exists():
        return ""
    chunks: list[str] = []
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        if not raw_line.strip():
            continue
        try:
            event = json.loads(raw_line)
        except json.JSONDecodeError:
            continue
        if event.get("event_type") != "output":
            continue
        data_b64 = event.get("data_b64")
        if isinstance(data_b64, str) and data_b64:
            chunks.append(base64.b64decode(data_b64).decode("utf-8", errors="ignore"))
    return "".join(chunks)

# ---------------------------------------------------------------------------
# Markers / skip conditions
# ---------------------------------------------------------------------------

_CLAUDE_INSTALLED = shutil.which("claude") is not None


def _claude_authenticated() -> bool:
    """Check if Claude CLI is installed AND authenticated.

    Runs a minimal -p invocation.  Must scrub CLAUDECODE from the
    environment so the probe works when tests run inside a Claude Code
    session (nested-session guard).
    """
    if not _CLAUDE_INSTALLED:
        return False
    try:
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        result = subprocess.run(
            ["claude", "-p", "--model", "haiku", "Reply with OK"],
            capture_output=True, text=True, timeout=30, env=env,
        )
        return result.returncode == 0
    except (subprocess.SubprocessError, OSError):
        return False


_CLAUDE_READY = _claude_authenticated()


@pytest.mark.skipif(not _CLAUDE_READY, reason="Claude CLI not installed or not authenticated")
class TestLiveAgentChain:
    """Prove the full pexpect → bash → provider_runner → Claude chain works."""

    @staticmethod
    def _venv_path_prefix() -> str:
        venv_bin = Path(__file__).resolve().parents[2] / ".venv" / "bin"
        return f"{venv_bin}:{os.environ.get('PATH', '')}"

    # ------------------------------------------------------------------
    # Layer 1: SubprocessAgentRunner → Claude (no PTY, -p mode)
    # ------------------------------------------------------------------

    def test_subprocess_runner_direct(self, tmp_path: Path) -> None:
        """SubprocessAgentRunner → Claude -p works (inner layer only)."""
        from issue_orchestrator.execution.subprocess_runner import SubprocessAgentRunner

        run_dir = tmp_path / "run"
        run_dir.mkdir()

        spec = AgentSpec(
            command=[
                "/bin/sh", "-c",
                "claude --permission-mode bypassPermissions --model haiku "
                "-p 'Reply with exactly: SUBPROCESS_TEST_OK'",
            ],
            working_dir=tmp_path,
            timeout_seconds=60,
            output_dir=run_dir,
        )

        result = SubprocessAgentRunner().run(spec)

        assert result.exit_code == 0, (
            f"Claude exited with code {result.exit_code}. stderr: {result.stderr}"
        )

    # ------------------------------------------------------------------
    # Layer 2: pexpect PTY → Claude -p (no provider_runner)
    # ------------------------------------------------------------------

    def test_pexpect_pty_direct_claude_p_mode(self, tmp_path: Path) -> None:
        """pexpect PTY → Claude -p works and produces log output."""
        log_path = tmp_path / "ui-session.log"
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        cmd = (
            f'export PATH="{self._venv_path_prefix()}" && '
            f"claude --permission-mode bypassPermissions --model haiku "
            f"-p 'Reply with exactly: PTY_DIRECT_TEST_OK'"
        )

        spec = AgentSpec(
            command=["/bin/bash", "-lc", cmd],
            working_dir=tmp_path,
            timeout_seconds=60,
            log_path=log_path,
            output_dir=run_dir,
        )

        result = AgentRunner().run(spec)

        assert result.exit_code == 0, (
            f"exit_code={result.exit_code}, "
            f"log:\n{log_path.read_text() if log_path.exists() else '<missing>'}"
        )

        decoded = _decoded_output(log_path)
        assert "PTY_DIRECT_TEST_OK" in decoded, (
            f"Claude output not in decoded log. Decoded content:\n{decoded}"
        )

    # ------------------------------------------------------------------
    # Layer 3: pexpect PTY → provider_runner → SubprocessAgentRunner → Claude -p
    # ------------------------------------------------------------------

    def test_full_chain_p_mode(self, tmp_path: Path) -> None:
        """Full chain with -p mode: pexpect → provider_runner → Claude."""
        log_path = tmp_path / "ui-session.log"
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        claude_cmd = (
            "claude --permission-mode bypassPermissions --model haiku "
            "-p 'Reply with exactly: FULL_CHAIN_P_TEST_OK'"
        )

        provider_runner_cmd = (
            f"python -m issue_orchestrator.entrypoints.cli_tools.provider_runner "
            f"--command {claude_cmd!r} "
            f"--timeout-seconds 60 "
            f"--max-attempts 1 "
            f"--run-dir {run_dir}"
        )

        full_cmd = (
            f'export PATH="{self._venv_path_prefix()}" && {provider_runner_cmd}'
        )

        spec = AgentSpec(
            command=["/bin/bash", "-lc", full_cmd],
            working_dir=tmp_path,
            timeout_seconds=120,
            log_path=log_path,
            output_dir=run_dir,
        )

        result = AgentRunner().run(spec)
        raw_log = log_path.read_text() if log_path.exists() else "<missing>"
        decoded = _decoded_output(log_path)

        assert result.exit_code == 0, (
            f"exit_code={result.exit_code}, log:\n{raw_log}"
        )

        assert "FULL_CHAIN_P_TEST_OK" in decoded, (
            f"Claude output not in decoded log. Decoded content:\n{decoded}"
        )

    # ------------------------------------------------------------------
    # Layer 4: THE LIVE PATH — -p mode with --append-system-prompt,
    # matching the orchestrator's production invocation pattern
    # ------------------------------------------------------------------

    def test_full_chain_production_flags(self, tmp_path: Path) -> None:
        """Full chain with production flags: -p + --append-system-prompt.

        The orchestrator uses:
            claude -p --permission-mode bypassPermissions --model haiku
                   --append-system-prompt 'system prompt'
                   'initial user prompt'

        This is what runs in production. Previously used interactive mode
        (no -p) which stalled because SubprocessAgentRunner uses stdin=DEVNULL.
        """
        log_path = tmp_path / "ui-session.log"
        run_dir = tmp_path / "run"
        run_dir.mkdir()

        system_prompt = "You are a test agent. Complete the task and exit."
        user_prompt = (
            "Reply with exactly PRODUCTION_FLAGS_OK and then use coding-done "
            "to report completion. If coding-done is not available, just reply."
        )

        # Match the exact live invocation: -p mode with
        # --append-system-prompt and positional prompt.
        # Use shlex.quote at each nesting level, matching production quoting.
        claude_cmd = (
            f"claude -p --permission-mode bypassPermissions --model haiku "
            f"--append-system-prompt {shlex.quote(system_prompt)} "
            f"{shlex.quote(user_prompt)}"
        )

        # provider_runner --command takes the whole claude invocation as
        # a single string argument — quote it for the outer shell
        provider_runner_cmd = (
            f"python -m issue_orchestrator.entrypoints.cli_tools.provider_runner "
            f"--command {shlex.quote(claude_cmd)} "
            f"--timeout-seconds 90 "
            f"--max-attempts 1 "
            f"--run-dir {run_dir}"
        )

        full_cmd = (
            f'export PATH="{self._venv_path_prefix()}" && {provider_runner_cmd}'
        )

        spec = AgentSpec(
            command=["/bin/bash", "-lc", full_cmd],
            working_dir=tmp_path,
            timeout_seconds=120,
            log_path=log_path,
            output_dir=run_dir,
        )

        runner = AgentRunner()
        session = runner.start(spec)

        # Mimic terminal_subprocess._start_session_watcher
        result_holder: list = []

        def _watch() -> None:
            result_holder.append(session.wait(timeout=120))

        watcher = threading.Thread(target=_watch, daemon=True)
        watcher.start()

        # Monitor progress — detect stalls
        deadline = time.monotonic() + 120
        last_size = 0
        stall_start: float | None = None
        STALL_THRESHOLD = 30  # seconds without log growth = stalled

        while time.monotonic() < deadline:
            if not session.is_alive() and not watcher.is_alive():
                break

            if log_path.exists():
                current_size = log_path.stat().st_size
                if current_size != last_size:
                    last_size = current_size
                    stall_start = None
                elif stall_start is None:
                    stall_start = time.monotonic()
                elif time.monotonic() - stall_start > STALL_THRESHOLD:
                    # Output stalled — capture state for diagnosis
                    log_snapshot = log_path.read_text()
                    session.kill()
                    pytest.fail(
                        f"Log output stalled for {STALL_THRESHOLD}s at "
                        f"{current_size} bytes. Session alive={session.is_alive()}. "
                        f"Log content:\n{log_snapshot}"
                    )

            time.sleep(1)

        watcher.join(timeout=10)

        assert result_holder, "Watcher thread never completed"
        result = result_holder[0]

        raw_log = log_path.read_text() if log_path.exists() else "<missing>"
        decoded = _decoded_output(log_path)

        # Must have produced some output
        assert len(raw_log) > 0, (
            f"Log is empty — likely stuck at startup. "
            f"Content:\n{raw_log}"
        )

        # Must NOT contain raw bun internals (check decoded stdout, not the
        # JSONL wrapper — base64 would mask the marker either way, but the
        # intent is to check what the agent actually emitted).
        assert "/$bunfs/" not in decoded, (
            f"Log contains bun runtime internals:\n{decoded}"
        )

        # Should exit cleanly
        assert result.exit_code == 0, (
            f"exit_code={result.exit_code}, timed_out={result.timed_out}. "
            f"Log:\n{raw_log}"
        )

    # ------------------------------------------------------------------
    # Layer 5: THE PERSISTENT PATH — open_persistent_session + send_round.
    #
    # This is the review-exchange path (a long-lived interactive claude driven
    # across rounds by injecting prompts on its PTY), distinct from the -p
    # one-shot layers above. It is where the tixmeup #277/#290 hang lived:
    # send_round used to terminate prompts with "\n", which a raw-mode TUI does
    # NOT treat as Enter, so the prompt rendered into the input box but was
    # never submitted and the round hung. send_round now submits with "\r".
    # This proves the fix against REAL claude across TWO rounds (the exact
    # scenario that hung), which no stub can establish.
    # ------------------------------------------------------------------

    def test_persistent_send_round_two_rounds_real_claude(self) -> None:
        # Run under the repo worktree (a trusted git repo) so the interactive
        # first-run "trust this folder?" dialog never blocks — mirrors
        # production, where review-exchange worktrees are trusted. /tmp would
        # trigger that dialog for an interactive session.
        repo_root = Path(__file__).resolve().parents[2]
        work_dir = Path(tempfile.mkdtemp(prefix=".live-send-round-", dir=repo_root))
        response_file = work_dir / "review-response.json"
        system_prompt = (
            "You are an automated review-exchange test agent. Every time you "
            "receive a message, immediately use the Bash tool to run exactly:\n"
            '  printf \'{"response_type":"ok"}\' > "$ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE"\n'
            "then keep waiting for the next message. Do not exit on your own."
        )
        # Scrub CLAUDECODE so the nested-session guard does not block claude
        # when these tests run inside a Claude Code session (same as the
        # auth probe above).
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        env["PATH"] = self._venv_path_prefix()
        env["ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE"] = str(response_file)

        session = open_persistent_session(
            command=[
                "claude", "--model", "haiku",
                "--permission-mode", "bypassPermissions",
                "--append-system-prompt", system_prompt,
            ],
            working_dir=work_dir,
            env=env,
        )
        try:
            # Let claude boot to its idle input prompt (trusted dir -> no dialog).
            from issue_orchestrator.execution.persistent_round_runner import (
                _drain_pty_output,
            )
            boot_deadline = time.monotonic() + 25
            while time.monotonic() < boot_deadline:
                _drain_pty_output(session)
                if session.proc.poll() is not None:
                    pytest.fail(f"claude exited during boot, code={session.proc.poll()}")
                time.sleep(0.3)

            for n in (1, 2):
                response_file.unlink(missing_ok=True)
                result = send_round(
                    session,
                    prompt=f"round {n}: respond now",
                    response_file=response_file,
                    timeout_seconds=60,
                    poll_interval_seconds=0.3,
                    role_label=f"coder@round-{n}",
                )
                assert result == {"response_type": "ok"}, (
                    f"round {n} not answered — a \\r-terminated prompt must "
                    f"submit to real claude. Got: {result}"
                )
                assert session.proc.poll() is None, (
                    f"claude exited after round {n}"
                )
        finally:
            close_persistent_session(session)
            shutil.rmtree(work_dir, ignore_errors=True)

    @staticmethod
    def _wait_for_pty_idle(
        session: object, *, quiet_seconds: float, max_wait: float,
    ) -> None:
        """Drain the PTY until output stays quiet for ``quiet_seconds``.

        codex queues stdin typed while its TUI is "Working" (it shows "tab to
        queue message") instead of submitting it, so the next round must only
        be injected once the agent is back at its idle input prompt. In
        production the coder rework step between reviewer rounds provides this
        settle time for free; a direct back-to-back driver must wait
        explicitly.
        """
        from issue_orchestrator.execution.persistent_round_runner import (
            _drain_pty_output,
        )

        deadline = time.monotonic() + max_wait
        last_activity = time.monotonic()
        while time.monotonic() < deadline:
            if _drain_pty_output(session) > 0:
                last_activity = time.monotonic()
            elif time.monotonic() - last_activity >= quiet_seconds:
                return
            time.sleep(0.2)

    def test_persistent_send_round_multi_round_real_codex_interactive(self) -> None:
        """The reviewer is INTERACTIVE codex (the production default): one
        persistent TUI process across multiple rounds. Round 1 is the
        launch-arg task; rounds 2 and 3 are injected with ``send_round`` and
        MUST submit via the two-write prompt+Enter contract — codex treats a
        \\r batched with the prompt text as a literal newline in its input box
        (renders, never submits), which is exactly the tixmeup #277/#290 hang
        class. The command is built via the codex provider so this test tracks
        the real production invocation."""
        if shutil.which("codex") is None:
            pytest.skip("codex CLI not installed")
        from issue_orchestrator.execution.agent_runner_providers import get_provider
        from issue_orchestrator.execution.persistent_round_runner import (
            _drain_pty_output,
        )

        repo_root = Path(__file__).resolve().parents[2]
        work_dir = Path(tempfile.mkdtemp(prefix=".live-codex-int-", dir=repo_root))
        response_file = work_dir / "review-response.json"
        task = (
            "Run exactly this one shell command and nothing else (no "
            "sleeping, no waiting commands): "
            'printf \'{"response_type":"ok"}\' > '
            '"$ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE"'
        )
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
        env["ISSUE_ORCHESTRATOR_REVIEW_RESPONSE_FILE"] = str(response_file)
        command = get_provider("codex").build_command(
            task, execution_mode="interactive", approval_mode="full-auto",
        )

        session = open_persistent_session(
            command=command, working_dir=work_dir, env=env,
        )
        try:
            # Round 1 is the launch-arg task — wait for codex to complete it.
            r1_deadline = time.monotonic() + 120
            while time.monotonic() < r1_deadline:
                _drain_pty_output(session)
                if response_file.exists():
                    break
                if session.proc.poll() is not None:
                    pytest.fail(
                        f"codex exited during round 1, code={session.proc.poll()}"
                    )
                time.sleep(0.3)
            else:
                pytest.fail("interactive codex did not finish its launch-arg task in 120s")
            assert session.proc.poll() is None, (
                "interactive codex must stay alive after round 1 — it is a "
                "persistent TUI, not the one-shot exec mode"
            )

            for n in (2, 3):
                self._wait_for_pty_idle(session, quiet_seconds=3.0, max_wait=30.0)
                response_file.unlink(missing_ok=True)
                result = send_round(
                    session,
                    prompt=task,
                    response_file=response_file,
                    timeout_seconds=90,
                    poll_interval_seconds=0.3,
                    role_label=f"reviewer@round-{n}",
                )
                assert result == {"response_type": "ok"}, (
                    f"round {n} not answered — the prompt+separate-Enter "
                    f"submit must reach interactive codex. Got: {result}"
                )
                assert session.proc.poll() is None, f"codex exited after round {n}"
        finally:
            close_persistent_session(session)
            shutil.rmtree(work_dir, ignore_errors=True)
