"""Tests for attributed shutdown-signal handling (infra.shutdown_signals).

Sender attribution relies on POSIX ``sigwaitinfo``, which exists on Linux but
NOT on macOS. So the real-signal end-to-end test is skipped where the syscall
is unavailable, and the capture/logging logic is additionally covered by a
mock-based test that runs everywhere. The fallback path (no attribution) is
covered too, since that's what macOS actually uses.
"""

from __future__ import annotations

import logging
import os
import signal
import subprocess
import sys
import textwrap
import threading
import time
from types import SimpleNamespace
from pathlib import Path

import pytest

from issue_orchestrator.infra import shutdown_signals as ss


def test_supports_sender_attribution_matches_platform() -> None:
    expected = hasattr(signal, "sigwaitinfo") and hasattr(signal, "pthread_sigmask")
    assert ss.supports_sender_attribution() is expected


def test_describe_sender_resolves_a_live_pid() -> None:
    desc = ss.describe_sender(os.getpid())
    assert desc and "exited" not in desc and "failed" not in desc


def test_describe_sender_handles_nonpositive_and_missing() -> None:
    assert ss.describe_sender(0) == "unknown (not reported)"
    assert ss.describe_sender(-1) == "unknown (not reported)"
    # A pid that (almost certainly) does not exist resolves to a benign string,
    # never raises.
    assert isinstance(ss.describe_sender(2_000_000_000), str)


def test_format_shutdown_signal_log_names_signal_and_sender() -> None:
    line = ss.format_shutdown_signal_log(int(signal.SIGTERM), os.getpid(), os.getuid())
    assert "SIGTERM" in line
    assert f"from pid={os.getpid()}" in line
    assert "/api/shutdown" in line  # keeps nudging operators toward attributable stops


def test_fallback_installs_asyncio_handlers_when_unsupported(monkeypatch) -> None:
    monkeypatch.setattr(ss, "supports_sender_attribution", lambda: False)

    registered: list[int] = []

    class _FakeLoop:
        def add_signal_handler(self, sig, _cb):  # noqa: ANN001
            registered.append(int(sig))

    ss.install_attributed_shutdown(loop=_FakeLoop(), on_shutdown=lambda: None)
    assert int(signal.SIGTERM) in registered
    assert int(signal.SIGINT) in registered


def test_sigwait_thread_logs_sender_and_triggers(monkeypatch, caplog) -> None:
    """Exercise the capture+log+dispatch path with a faked sigwaitinfo.

    Runs on every platform (macOS included) by injecting ``signal.sigwaitinfo``,
    so the logic is verified even where the real syscall is absent.
    """
    fake_info = SimpleNamespace(si_signo=int(signal.SIGTERM), si_pid=424242, si_uid=501)
    gate = threading.Event()
    first = {"done": False}

    def fake_sigwaitinfo(_sigset):
        if not first["done"]:
            first["done"] = True
            return fake_info
        # Park the daemon thread for the rest of the test so it never loops back
        # to the (monkeypatched-away) real sigwaitinfo during teardown.
        gate.wait()
        raise InterruptedError

    monkeypatch.setattr(signal, "sigwaitinfo", fake_sigwaitinfo, raising=False)

    fired = threading.Event()

    class _Loop:
        def call_soon_threadsafe(self, cb):  # noqa: ANN001
            cb()
            fired.set()

    with caplog.at_level(logging.WARNING):
        ss._start_sigwait_thread(_Loop(), lambda: None)  # noqa: SLF001 — exercises the private sigwait dispatch path directly
        assert fired.wait(timeout=5), "on_shutdown was not dispatched"

    # Leave `gate` unset: the daemon thread stays parked (and dies with the
    # process) rather than looping back to a torn-down monkeypatch.
    assert "from pid=424242" in caplog.text
    assert "SIGTERM" in caplog.text


# Child process: block the signals, install the attributed handler, log to a
# file, signal READY, then wait for the on_shutdown callback. The parent sends
# SIGTERM and asserts the child's log names the parent as the sender.
_CHILD = textwrap.dedent(
    """
    import asyncio, logging, sys
    from issue_orchestrator.infra import shutdown_signals as ss

    logging.basicConfig(filename=sys.argv[1], level=logging.WARNING,
                        format="%(message)s")
    assert ss.block_shutdown_signals() is True

    async def main():
        loop = asyncio.get_running_loop()
        done = asyncio.Event()
        ss.install_attributed_shutdown(loop=loop, on_shutdown=done.set)
        print("READY", flush=True)
        await asyncio.wait_for(done.wait(), timeout=10)

    asyncio.run(main())
    """
)


@pytest.mark.skipif(
    not ss.supports_sender_attribution(),
    reason="signal-sender capture requires POSIX sigwaitinfo (unavailable on macOS)",
)
def test_sigwaitinfo_captures_real_sender(tmp_path: Path) -> None:
    log_file = tmp_path / "child.log"
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD, str(log_file)],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdout is not None
        deadline = time.time() + 15
        line = ""
        while time.time() < deadline:
            line = proc.stdout.readline()
            if "READY" in line:
                break
        assert "READY" in line, "child never became ready"
        time.sleep(0.3)  # let the sigwait thread enter sigwaitinfo

        os.kill(proc.pid, signal.SIGTERM)
        proc.wait(timeout=15)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

    assert proc.returncode == 0, "child did not shut down cleanly on SIGTERM"
    logged = log_file.read_text(encoding="utf-8")
    # The sender of the SIGTERM is THIS test process — attribution must name it.
    assert f"from pid={os.getpid()}" in logged, (
        f"expected captured sender pid {os.getpid()}; log was:\n{logged}"
    )
    assert "SIGTERM" in logged
