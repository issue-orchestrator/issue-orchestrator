"""Persistent-PTY round runner: lifecycle and round handoff behavior.

Uses a self-contained stub agent script written into ``tmp_path`` so the
test does not require shipping a separate test fixture or any real
agent binary. The stub mimics the contract real review-exchange agents
honor: read prompt from stdin, write a JSON response file specified
via ``$STUB_RESPONSE_FILE`` env var, loop until EOF.
"""

from __future__ import annotations

import os
import sys
import textwrap
import time
from pathlib import Path

import pytest

from issue_orchestrator.execution.persistent_round_runner import (
    PersistentRoundError,
    PersistentRoundTimeoutError,
    close_persistent_session,
    open_persistent_session,
    recording_event_count,
    send_round,
)


_STUB_AGENT_SOURCE = textwrap.dedent("""
    import json
    import os
    import sys
    import time
    from pathlib import Path

    response_file = Path(os.environ["STUB_RESPONSE_FILE"])
    fail_on_round = int(os.environ.get("STUB_FAIL_ON_ROUND", "0"))
    sleep_per_round = float(os.environ.get("STUB_SLEEP_SECONDS", "0.02"))

    print("[stub-agent] ready", flush=True)
    round_index = 0
    for raw in sys.stdin:
        prompt = raw.strip()
        if not prompt:
            continue
        round_index += 1
        if fail_on_round and round_index == fail_on_round:
            sys.exit(7)
        print(f"[stub] round {round_index}: {prompt!r}", flush=True)
        time.sleep(sleep_per_round)
        response_file.parent.mkdir(parents=True, exist_ok=True)
        response_file.write_text(
            json.dumps({"round": round_index, "prompt": prompt, "ack": True}),
            encoding="utf-8",
        )
        print(f"[stub] wrote round {round_index}", flush=True)
    print("[stub] EOF", flush=True)
""").strip()


def _write_stub_agent(tmp_path: Path) -> Path:
    path = tmp_path / "stub_agent.py"
    path.write_text(_STUB_AGENT_SOURCE, encoding="utf-8")
    return path


def _stub_command(stub_path: Path) -> list[str]:
    return [sys.executable, "-u", str(stub_path)]


def _stub_env(response_file: Path, **extras: str) -> dict[str, str]:
    env = dict(os.environ)
    env["STUB_RESPONSE_FILE"] = str(response_file)
    env.update(extras)
    return env


class TestPersistentSessionLifecycle:
    def test_one_process_handles_three_sequential_rounds(self, tmp_path: Path) -> None:
        """The headline assertion: one persistent agent process answers
        three sequential round prompts. ``round`` from the response is a
        per-process counter that only persists if the process did."""
        stub = _write_stub_agent(tmp_path)
        response_file = tmp_path / "response.json"

        session = open_persistent_session(
            command=_stub_command(stub),
            working_dir=tmp_path,
            env=_stub_env(response_file),
        )
        try:
            r1 = send_round(session, prompt="alpha", response_file=response_file, timeout_seconds=5)
            r2 = send_round(session, prompt="bravo", response_file=response_file, timeout_seconds=5)
            r3 = send_round(session, prompt="charlie", response_file=response_file, timeout_seconds=5)
        finally:
            close_persistent_session(session)

        assert r1 == {"round": 1, "prompt": "alpha", "ack": True}
        assert r2 == {"round": 2, "prompt": "bravo", "ack": True}
        assert r3 == {"round": 3, "prompt": "charlie", "ack": True}

    def test_response_file_is_re_armed_each_round(self, tmp_path: Path) -> None:
        stub = _write_stub_agent(tmp_path)
        response_file = tmp_path / "response.json"

        session = open_persistent_session(
            command=_stub_command(stub),
            working_dir=tmp_path,
            env=_stub_env(response_file),
        )
        try:
            send_round(session, prompt="round-1", response_file=response_file, timeout_seconds=5)
            assert response_file.exists()
            r2 = send_round(session, prompt="round-2", response_file=response_file, timeout_seconds=5)
        finally:
            close_persistent_session(session)
        # Round 2 lands unambiguously because round 1's file was unlinked.
        assert r2["round"] == 2
        assert r2["prompt"] == "round-2"

    def test_recording_path_captures_continuous_log_across_rounds(
        self, tmp_path: Path,
    ) -> None:
        """One ``terminal-recording.jsonl`` per role spanning the whole
        exchange — the property the session viewer needs for clean replay."""
        stub = _write_stub_agent(tmp_path)
        response_file = tmp_path / "response.json"
        recording = tmp_path / "rec" / "terminal-recording.jsonl"

        session = open_persistent_session(
            command=_stub_command(stub),
            working_dir=tmp_path,
            env=_stub_env(response_file),
            recording_path=recording,
        )
        try:
            send_round(session, prompt="alpha", response_file=response_file, timeout_seconds=5)
            send_round(session, prompt="bravo", response_file=response_file, timeout_seconds=5)
        finally:
            close_persistent_session(session)

        assert recording.exists()
        assert recording_event_count(recording) >= 2
        # The recording is JSONL with base64-encoded data per event. Decode
        # all output events and confirm both round markers appear in order
        # in the same continuous log.
        import base64
        import json as json_mod
        decoded = []
        for line in recording.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json_mod.loads(line)
            if event.get("event_type") == "output" and event.get("data_b64"):
                decoded.append(
                    base64.b64decode(event["data_b64"]).decode("utf-8", errors="replace")
                )
        combined = "".join(decoded)
        pos_a = combined.find("alpha")
        pos_b = combined.find("bravo")
        assert pos_a != -1 and pos_b != -1 and pos_a < pos_b


class TestPersistentSessionFailureModes:
    def test_timeout_raises_when_agent_does_not_respond(self, tmp_path: Path) -> None:
        stub = _write_stub_agent(tmp_path)
        response_file = tmp_path / "response.json"

        session = open_persistent_session(
            command=_stub_command(stub),
            working_dir=tmp_path,
            # Stub sleeps 0.5s per round; timeout is well under that.
            env=_stub_env(response_file, STUB_SLEEP_SECONDS="0.5"),
        )
        try:
            with pytest.raises(PersistentRoundTimeoutError):
                send_round(
                    session,
                    prompt="too slow",
                    response_file=response_file,
                    timeout_seconds=0.05,
                    poll_interval_seconds=0.01,
                )
        finally:
            close_persistent_session(session)

    def test_unexpected_exit_mid_round_raises(self, tmp_path: Path) -> None:
        """Catches the case where the agent dies before responding —
        regression prevention for the orchestrator hanging forever."""
        stub = _write_stub_agent(tmp_path)
        response_file = tmp_path / "response.json"

        session = open_persistent_session(
            command=_stub_command(stub),
            working_dir=tmp_path,
            # Stub will exit non-zero on round 1.
            env=_stub_env(response_file, STUB_FAIL_ON_ROUND="1"),
        )
        try:
            with pytest.raises(PersistentRoundError, match="exited unexpectedly"):
                send_round(
                    session,
                    prompt="will-die",
                    response_file=response_file,
                    timeout_seconds=5,
                    poll_interval_seconds=0.05,
                )
        finally:
            close_persistent_session(session)

    def test_send_round_after_close_raises(self, tmp_path: Path) -> None:
        stub = _write_stub_agent(tmp_path)
        response_file = tmp_path / "response.json"

        session = open_persistent_session(
            command=_stub_command(stub),
            working_dir=tmp_path,
            env=_stub_env(response_file),
        )
        send_round(session, prompt="hi", response_file=response_file, timeout_seconds=5)
        close_persistent_session(session)

        with pytest.raises(PersistentRoundError, match="already closed"):
            send_round(session, prompt="late", response_file=response_file, timeout_seconds=5)


class TestRecordingEventCount:
    def test_returns_zero_for_missing_file(self, tmp_path: Path) -> None:
        assert recording_event_count(tmp_path / "absent.jsonl") == 0

    def test_counts_only_non_blank_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "rec.jsonl"
        path.write_text('{"a":1}\n\n{"b":2}\n  \n{"c":3}\n', encoding="utf-8")
        assert recording_event_count(path) == 3


class TestCloseSession:
    def test_close_terminates_running_agent_and_returns_exit_code(
        self, tmp_path: Path,
    ) -> None:
        stub = _write_stub_agent(tmp_path)
        response_file = tmp_path / "response.json"

        session = open_persistent_session(
            command=_stub_command(stub),
            working_dir=tmp_path,
            env=_stub_env(response_file),
        )
        send_round(session, prompt="hello", response_file=response_file, timeout_seconds=5)
        # Wait briefly so the process is past the post-write barrier.
        time.sleep(0.05)

        rc = close_persistent_session(session, grace_seconds=2.0)

        assert session.closed is True
        # SIGTERM-driven exit codes are 0/-15/143 depending on platform; what
        # matters is the process is no longer alive.
        assert session.proc.poll() is not None
        assert rc is not None

    def test_close_is_idempotent(self, tmp_path: Path) -> None:
        stub = _write_stub_agent(tmp_path)
        response_file = tmp_path / "response.json"
        session = open_persistent_session(
            command=_stub_command(stub),
            working_dir=tmp_path,
            env=_stub_env(response_file),
        )
        close_persistent_session(session)
        # Calling again must not raise.
        close_persistent_session(session)
        assert session.closed is True
