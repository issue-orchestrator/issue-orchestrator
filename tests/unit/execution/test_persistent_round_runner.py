"""Persistent-PTY round runner: lifecycle, round handoff, and partial-write
tolerance.

Tests use a self-contained stub agent that supports a control-file gate
(``STUB_RESPONSE_GATE``) and an opt-in partial-write race
(``STUB_PARTIAL_WRITE_FIRST``). Coordination happens via gate files and
injected ``now``/``sleep`` callables, never via real wall-clock waits, in
keeping with ``tests/unit/AGENTS.md``'s ban on timing-based unit
coordination.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import textwrap
from pathlib import Path
from typing import Callable

import pytest

from issue_orchestrator.execution.persistent_round_runner import (
    CorruptRecordingError,
    PersistentRoundError,
    PersistentRoundTimeoutError,
    close_persistent_session,
    open_persistent_session,
    recording_event_count,
    send_round,
)


# ---------------------------------------------------------------------------
# Stub agent
# ---------------------------------------------------------------------------

_STUB_AGENT_SOURCE = textwrap.dedent("""
    import json
    import os
    import sys
    import time
    from pathlib import Path

    response_file = Path(os.environ["STUB_RESPONSE_FILE"])
    gate_file = os.environ.get("STUB_RESPONSE_GATE")
    fail_on_round = int(os.environ.get("STUB_FAIL_ON_ROUND", "0"))
    partial_first = os.environ.get("STUB_PARTIAL_WRITE_FIRST", "0") == "1"


    def _wait_for_gate() -> None:
        if not gate_file:
            return
        gate_path = Path(gate_file)
        # Polite spin — duration is determined by when the test creates the
        # gate file, not by any stub-side sleep budget.
        while not gate_path.exists():
            time.sleep(0.01)


    print("[stub-agent] ready", flush=True)
    round_index = 0
    for raw in sys.stdin:
        prompt = raw.strip()
        if not prompt:
            continue
        round_index += 1
        if fail_on_round and round_index == fail_on_round:
            sys.exit(7)
        response_file.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps({"round": round_index, "prompt": prompt, "ack": True})
        if partial_first and round_index == 1:
            # Race seed: write a partially-written JSON document, wait for
            # the test to flip the gate, then complete the write. The test
            # asserts send_round() keeps polling instead of exploding on
            # the partial JSON.
            with open(response_file, "w") as f:
                f.write("{")
                f.flush()
            print(f"[stub] partial write round {round_index}", flush=True)
            _wait_for_gate()
            response_file.write_text(payload, encoding="utf-8")
            print(f"[stub] full write round {round_index}", flush=True)
            continue
        _wait_for_gate()
        response_file.write_text(payload, encoding="utf-8")
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


# ---------------------------------------------------------------------------
# Deterministic clock + sleep for tests that drive timeouts
# ---------------------------------------------------------------------------


class _FakeClock:
    """Monotonic clock that advances on each injected ``sleep`` call."""

    def __init__(self) -> None:
        self.value = 0.0

    def now(self) -> float:
        return self.value

    def make_sleeper(
        self, on_sleep: Callable[[float], None] | None = None,
    ) -> Callable[[float], None]:
        def _sleep(seconds: float) -> None:
            self.value += seconds
            if on_sleep is not None:
                on_sleep(seconds)
        return _sleep


def _wait_until(predicate: Callable[[], bool], timeout: float = 5.0) -> None:
    """Test-internal helper: poll predicate until True or assert.

    Used to wait on cross-process state (response file appears, process exits)
    without coordinating timing — the deadline is a safety net so a stuck
    test fails fast instead of hanging CI forever.
    """
    import time as _time
    deadline = _time.monotonic() + timeout
    while _time.monotonic() < deadline:
        if predicate():
            return
        _time.sleep(0.005)
    raise AssertionError(f"Predicate did not become true within {timeout}s")


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestPersistentSessionLifecycle:
    def test_open_session_starts_slave_in_noncanonical_mode(self, tmp_path: Path) -> None:
        probe = tmp_path / "termios.json"
        script = tmp_path / "termios_probe.py"
        script.write_text(textwrap.dedent("""
            import json
            import os
            import sys
            import termios
            from pathlib import Path

            attrs = termios.tcgetattr(sys.stdin.fileno())
            Path(os.environ["TERMIO_PROBE_PATH"]).write_text(
                json.dumps({
                    "icanon": bool(attrs[3] & termios.ICANON),
                    "echo": bool(attrs[3] & termios.ECHO),
                }),
                encoding="utf-8",
            )
            for _raw in sys.stdin:
                pass
        """).strip(), encoding="utf-8")
        env = dict(os.environ)
        env["TERMIO_PROBE_PATH"] = str(probe)

        session = open_persistent_session(
            command=[sys.executable, "-u", str(script)],
            working_dir=tmp_path,
            env=env,
        )
        try:
            _wait_until(lambda: probe.exists() and probe.stat().st_size > 0)
        finally:
            close_persistent_session(session)

        state = json.loads(probe.read_text(encoding="utf-8"))
        assert state == {"icanon": False, "echo": False}

    def test_one_process_handles_three_sequential_rounds(self, tmp_path: Path) -> None:
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

    def test_send_round_accepts_prompt_larger_than_canonical_line_limit(
        self, tmp_path: Path,
    ) -> None:
        stub = _write_stub_agent(tmp_path)
        response_file = tmp_path / "response.json"
        long_prompt = "x" * 5000

        session = open_persistent_session(
            command=_stub_command(stub),
            working_dir=tmp_path,
            env=_stub_env(response_file),
        )
        try:
            response = send_round(
                session,
                prompt=long_prompt,
                response_file=response_file,
                timeout_seconds=5,
            )
        finally:
            close_persistent_session(session)

        assert response == {"round": 1, "prompt": long_prompt, "ack": True}

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
        assert r2["round"] == 2 and r2["prompt"] == "round-2"

    def test_recording_path_captures_continuous_log_across_rounds(
        self, tmp_path: Path,
    ) -> None:
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
        import base64
        import json as json_mod
        decoded: list[str] = []
        for line in recording.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            event = json_mod.loads(line)
            if event.get("event_type") == "output" and event.get("data_b64"):
                decoded.append(
                    base64.b64decode(event["data_b64"]).decode("utf-8", errors="replace")
                )
        combined = "".join(decoded)
        pos_a = combined.find("wrote round 1")
        pos_b = combined.find("wrote round 2")
        assert pos_a != -1 and pos_b != -1 and pos_a < pos_b


# ---------------------------------------------------------------------------
# Failure modes — driven by gates / injected clock instead of wall-clock races
# ---------------------------------------------------------------------------


class TestPersistentSessionFailureModes:
    def test_timeout_is_driven_by_injected_clock_not_wall_time(
        self, tmp_path: Path,
    ) -> None:
        """The stub is gated and never writes a response. The runner's
        injected clock advances past the deadline on the first ``sleep``
        call, so the timeout fires immediately in real time. No
        wall-clock coordination."""
        stub = _write_stub_agent(tmp_path)
        response_file = tmp_path / "response.json"
        gate = tmp_path / "gate.never"  # never created

        session = open_persistent_session(
            command=_stub_command(stub),
            working_dir=tmp_path,
            env=_stub_env(response_file, STUB_RESPONSE_GATE=str(gate)),
        )
        try:
            clock = _FakeClock()
            # Sleep callable jumps the clock past the deadline immediately.
            sleeper = clock.make_sleeper(on_sleep=lambda _: clock.__setattr__(
                "value", max(clock.value, 100.0)
            ))
            with pytest.raises(PersistentRoundTimeoutError):
                send_round(
                    session,
                    prompt="never-responds",
                    response_file=response_file,
                    timeout_seconds=1.0,
                    poll_interval_seconds=0.01,
                    now=clock.now,
                    sleep=sleeper,
                )
        finally:
            close_persistent_session(session)

    def test_unexpected_exit_mid_round_raises(self, tmp_path: Path) -> None:
        stub = _write_stub_agent(tmp_path)
        response_file = tmp_path / "response.json"

        session = open_persistent_session(
            command=_stub_command(stub),
            working_dir=tmp_path,
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


# ---------------------------------------------------------------------------
# Partial-write race tolerance — the regression that reviewer #6143 caught
# ---------------------------------------------------------------------------


class TestPartialWriteTolerance:
    def test_send_round_keeps_polling_through_partial_write(
        self, tmp_path: Path,
    ) -> None:
        """The stub writes a half-formed JSON document, waits for the test
        to flip a gate, then writes the complete document. The runner must
        treat the partial JSON as 'still being written' and keep polling,
        not raise PersistentRoundError on the first JSONDecodeError.
        """
        stub = _write_stub_agent(tmp_path)
        response_file = tmp_path / "response.json"
        gate = tmp_path / "complete-write.gate"

        session = open_persistent_session(
            command=_stub_command(stub),
            working_dir=tmp_path,
            env=_stub_env(
                response_file,
                STUB_PARTIAL_WRITE_FIRST="1",
                STUB_RESPONSE_GATE=str(gate),
            ),
        )
        try:
            # Drive the partial-write race deterministically: a sleeper
            # that flips the gate after the runner has observed the
            # partial-JSON state. The first poll iteration happens before
            # the stub has even started; subsequent polls find partial
            # content and return None; once the gate flips, the stub
            # finishes the write and the next poll succeeds.
            polled = {"count": 0}

            def stepping_sleeper(_seconds: float) -> None:
                polled["count"] += 1
                if polled["count"] == 2:
                    # By now the stub is past its partial write and parked
                    # on the gate. Flip it to let the write complete.
                    gate.touch()

            response = send_round(
                session,
                prompt="partial-write",
                response_file=response_file,
                timeout_seconds=5.0,
                poll_interval_seconds=0.05,
                sleep=stepping_sleeper,
            )
            assert response == {"round": 1, "prompt": "partial-write", "ack": True}
            # The runner did NOT bail out on the partial-write state.
            assert polled["count"] >= 2
        finally:
            close_persistent_session(session)


# ---------------------------------------------------------------------------
# PTY prompt-write loop — locks in the #6160 e2e regression fix
# ---------------------------------------------------------------------------


class TestWriteFullHandlesNonBlockingPtyWrites:
    """The PTY master fd is non-blocking, so ``os.write`` may return fewer
    bytes than requested or raise ``BlockingIOError`` when the kernel's
    PTY input buffer is nearly full. The previous code path called
    ``os.write(fd, payload)`` once and ignored the return value, so any
    unwritten suffix was silently dropped — the agent received a
    truncated prompt and the round hung forever.

    These tests pin ``_write_full`` against three failure modes:
    short writes, ``BlockingIOError``, and zero-byte writes — all on
    a deadline so a kernel that never drains raises the timeout error.
    """

    def test_loops_until_full_payload_is_written_through_short_writes(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from issue_orchestrator.execution import persistent_round_runner
        from issue_orchestrator.execution.persistent_round_runner import (
            _write_full,  # noqa: PLC2701 — private helper is the contract under test
        )

        payload = b"abcdefghij"
        observed: list[bytes] = []
        # Simulate a kernel that only accepts 3 bytes per call until done.
        def fake_write(_fd: int, buf: bytes) -> int:
            n = min(3, len(buf))
            observed.append(buf[:n])
            return n

        monkeypatch.setattr(persistent_round_runner.os, "write", fake_write)
        clock = _FakeClock()
        sleeper = clock.make_sleeper()

        written = _write_full(
            fd=99, payload=payload,
            deadline=clock.now() + 5.0, now=clock.now, sleep=sleeper,
        )
        assert written == len(payload)
        assert b"".join(observed) == payload

    def test_retries_on_blocking_io_error(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from issue_orchestrator.execution import persistent_round_runner
        from issue_orchestrator.execution.persistent_round_runner import (
            _write_full,  # noqa: PLC2701 — private helper is the contract under test
        )

        payload = b"hello"
        attempts = {"count": 0}
        def fake_write(_fd: int, buf: bytes) -> int:
            attempts["count"] += 1
            if attempts["count"] <= 2:
                raise BlockingIOError("buffer full")
            return len(buf)

        monkeypatch.setattr(persistent_round_runner.os, "write", fake_write)
        clock = _FakeClock()
        sleeper = clock.make_sleeper()

        written = _write_full(
            fd=99, payload=payload,
            deadline=clock.now() + 5.0, now=clock.now, sleep=sleeper,
        )
        assert written == len(payload)
        assert attempts["count"] == 3

    def test_raises_timeout_when_kernel_never_drains(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from issue_orchestrator.execution import persistent_round_runner
        from issue_orchestrator.execution.persistent_round_runner import (
            _write_full,  # noqa: PLC2701 — private helper is the contract under test
        )

        def always_blocking(_fd: int, _buf: bytes) -> int:
            raise BlockingIOError("buffer perpetually full")

        monkeypatch.setattr(persistent_round_runner.os, "write", always_blocking)
        clock = _FakeClock()
        # Deadline already in the past so the very first iteration trips.
        sleeper = clock.make_sleeper()

        with pytest.raises(PersistentRoundTimeoutError, match="Could not write"):
            _write_full(
                fd=99, payload=b"x",
                deadline=clock.now() - 1.0, now=clock.now, sleep=sleeper,
            )

    def test_closed_fd_surfaces_as_round_error(
        self, monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        from issue_orchestrator.execution import persistent_round_runner
        from issue_orchestrator.execution.persistent_round_runner import (
            _write_full,  # noqa: PLC2701 — private helper is the contract under test
        )

        def closed_fd(_fd: int, _buf: bytes) -> int:
            raise OSError("bad file descriptor")

        monkeypatch.setattr(persistent_round_runner.os, "write", closed_fd)
        clock = _FakeClock()

        with pytest.raises(PersistentRoundError, match="bad file descriptor"):
            _write_full(
                fd=99,
                payload=b"x",
                deadline=clock.now() + 5.0,
                now=clock.now,
                sleep=clock.make_sleeper(),
                role_label="coder@round-1",
                pid=123,
            )

    def test_send_round_uses_separate_write_deadline(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
    ) -> None:
        from issue_orchestrator.execution import persistent_round_runner

        class _Proc:
            pid = 456

            def poll(self) -> None:
                return None

        captured: dict[str, float] = {}

        def fake_write_full(_fd: int, _payload: bytes, **kwargs: object) -> int:
            captured["deadline"] = kwargs["deadline"]  # type: ignore[assignment]
            raise PersistentRoundTimeoutError("stop after write deadline capture")

        monkeypatch.setattr(persistent_round_runner, "_write_full", fake_write_full)
        session = persistent_round_runner.PersistentSession(
            proc=_Proc(),  # type: ignore[arg-type]
            master_fd=99,
        )
        clock = _FakeClock()
        clock.value = 10.0

        with pytest.raises(PersistentRoundTimeoutError, match="deadline capture"):
            send_round(
                session,
                prompt="hello",
                response_file=tmp_path / "response.json",
                timeout_seconds=100.0,
                write_timeout_seconds=3.0,
                now=clock.now,
                sleep=clock.make_sleeper(),
            )

        assert captured["deadline"] == 13.0

    def test_send_round_logs_prompt_write_timeout_context(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        from issue_orchestrator.execution import persistent_round_runner

        class _Proc:
            pid = 789

            def poll(self) -> None:
                return None

        def fake_write_full(_fd: int, _payload: bytes, **_kwargs: object) -> int:
            raise PersistentRoundTimeoutError(
                "Could not write 42 bytes to PTY fd=99 role=reviewer@round-1 "
                "within deadline (0 bytes accepted before timeout)"
            )

        monkeypatch.setattr(persistent_round_runner, "_write_full", fake_write_full)
        session = persistent_round_runner.PersistentSession(
            proc=_Proc(),  # type: ignore[arg-type]
            master_fd=99,
        )
        clock = _FakeClock()

        with caplog.at_level(
            logging.WARNING,
            logger="issue_orchestrator.execution.persistent_round_runner",
        ):
            with pytest.raises(PersistentRoundTimeoutError):
                send_round(
                    session,
                    prompt="hello",
                    response_file=tmp_path / "response.json",
                    timeout_seconds=100.0,
                    write_timeout_seconds=3.0,
                    now=clock.now,
                    sleep=clock.make_sleeper(),
                    role_label="reviewer@round-1",
                )

        messages = "\n".join(caplog.messages)
        assert "prompt write timeout role=reviewer@round-1 pid=789" in messages
        assert "likely_stale_persistent_session=True" in messages
        assert "0 bytes accepted before timeout" in messages


# ---------------------------------------------------------------------------
# Recording event count
# ---------------------------------------------------------------------------


class TestRecordingEventCount:
    def test_default_raises_for_missing_recording(self, tmp_path: Path) -> None:
        """Per session-replay contract: a missing recording when one is
        expected is a caller bug, not a zero-event signal that would
        produce wrong-but-plausible chapter offsets."""
        with pytest.raises(FileNotFoundError):
            recording_event_count(tmp_path / "absent.jsonl")

    def test_explicit_opt_out_returns_zero_for_missing(self, tmp_path: Path) -> None:
        """Bootstrap and test paths that genuinely have no recording yet
        opt out of the existence check."""
        assert recording_event_count(
            tmp_path / "absent.jsonl",
            require_recording=False,
        ) == 0

    def test_counts_valid_recording_events_skipping_blank_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "rec.jsonl"
        path.write_text(
            '{"schema_version":1,"event_type":"resize","offset_ms":0,"rows":40,"cols":120}\n\n'
            '{"schema_version":1,"event_type":"output","offset_ms":12,"data_b64":"aGk="}\n  \n'
            '{"schema_version":1,"event_type":"output","offset_ms":99,"data_b64":"YnllCg=="}\n',
            encoding="utf-8",
        )
        assert recording_event_count(path) == 3

    def test_raises_on_malformed_json_line(self, tmp_path: Path) -> None:
        """A corrupt recording must surface loudly — the offset feeds into
        chapters.json and the session viewer scrubs to it. A wrong-but-
        plausible count is worse than a loud failure."""
        path = tmp_path / "rec.jsonl"
        path.write_text(
            '{"schema_version":1,"event_type":"output","offset_ms":0,"data_b64":"aGk="}\n'
            "not-json\n",
            encoding="utf-8",
        )
        with pytest.raises(CorruptRecordingError, match="Malformed JSON"):
            recording_event_count(path)

    def test_raises_when_event_is_not_an_object(self, tmp_path: Path) -> None:
        path = tmp_path / "rec.jsonl"
        path.write_text('"just a string"\n', encoding="utf-8")
        with pytest.raises(CorruptRecordingError, match="not a JSON object"):
            recording_event_count(path)

    def test_raises_when_event_type_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "rec.jsonl"
        path.write_text(
            '{"schema_version":1,"offset_ms":0,"data_b64":"aGk="}\n', encoding="utf-8",
        )
        with pytest.raises(CorruptRecordingError, match="missing event_type"):
            recording_event_count(path)

    def test_raises_when_schema_version_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "rec.jsonl"
        path.write_text(
            '{"event_type":"output","offset_ms":0,"data_b64":"aGk="}\n', encoding="utf-8",
        )
        with pytest.raises(CorruptRecordingError, match="schema_version"):
            recording_event_count(path)

    def test_raises_when_offset_ms_missing(self, tmp_path: Path) -> None:
        path = tmp_path / "rec.jsonl"
        path.write_text(
            '{"schema_version":1,"event_type":"output","data_b64":"aGk="}\n',
            encoding="utf-8",
        )
        with pytest.raises(CorruptRecordingError, match="offset_ms"):
            recording_event_count(path)

    def test_raises_when_output_event_lacks_data_b64(self, tmp_path: Path) -> None:
        """Replay can't render an output event without payload bytes — that
        line must not advance the chapter offset."""
        path = tmp_path / "rec.jsonl"
        path.write_text(
            '{"schema_version":1,"event_type":"output","offset_ms":0}\n',
            encoding="utf-8",
        )
        with pytest.raises(CorruptRecordingError, match="data_b64"):
            recording_event_count(path)

    def test_raises_when_resize_event_lacks_rows(self, tmp_path: Path) -> None:
        path = tmp_path / "rec.jsonl"
        path.write_text(
            '{"schema_version":1,"event_type":"resize","offset_ms":0,"cols":120}\n',
            encoding="utf-8",
        )
        with pytest.raises(CorruptRecordingError, match="missing integer rows"):
            recording_event_count(path)

    def test_raises_when_resize_event_lacks_cols(self, tmp_path: Path) -> None:
        path = tmp_path / "rec.jsonl"
        path.write_text(
            '{"schema_version":1,"event_type":"resize","offset_ms":0,"rows":40}\n',
            encoding="utf-8",
        )
        with pytest.raises(CorruptRecordingError, match="missing integer cols"):
            recording_event_count(path)

    def test_raises_when_output_data_b64_is_not_valid_base64(self, tmp_path: Path) -> None:
        """An output event whose ``data_b64`` is non-empty but not actually
        base64 will crash the browser replay decoder at scrub time, so it
        must not advance the chapter offset."""
        path = tmp_path / "rec.jsonl"
        path.write_text(
            '{"schema_version":1,"event_type":"output","offset_ms":0,'
            '"data_b64":"@@@@"}\n',
            encoding="utf-8",
        )
        with pytest.raises(CorruptRecordingError, match="not valid base64"):
            recording_event_count(path)

    def test_raises_on_unsupported_event_type(self, tmp_path: Path) -> None:
        path = tmp_path / "rec.jsonl"
        path.write_text(
            '{"schema_version":1,"event_type":"junk","offset_ms":0}\n',
            encoding="utf-8",
        )
        with pytest.raises(CorruptRecordingError, match="unsupported event_type"):
            recording_event_count(path)


# ---------------------------------------------------------------------------
# Close
# ---------------------------------------------------------------------------


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

        rc = close_persistent_session(session, grace_seconds=2.0)
        # The close path waits for the process internally; once it returns,
        # the process must be reaped (no race window to wait around).
        assert session.closed is True
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
        close_persistent_session(session)
        assert session.closed is True
