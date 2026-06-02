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

    target = ss._GracefulTarget()  # noqa: SLF001 — exercises the private dispatch path
    target.attach(_Loop(), lambda: None)
    with caplog.at_level(logging.WARNING):
        ss._start_sigwait_thread(target)  # noqa: SLF001 — exercises the private sigwait dispatch path directly
        assert fired.wait(timeout=5), "on_shutdown was not dispatched"

    # Leave `gate` unset: the daemon thread stays parked (and dies with the
    # process) rather than looping back to a torn-down monkeypatch.
    assert "from pid=424242" in caplog.text
    assert "SIGTERM" in caplog.text


def test_graceful_target_routes_to_loop_after_attach() -> None:
    """Once attached, dispatch schedules the callback on the loop (graceful path)."""
    scheduled: list = []

    class _Loop:
        def call_soon_threadsafe(self, cb):  # noqa: ANN001
            scheduled.append(cb)

    sentinel = lambda: None  # noqa: E731
    target = ss._GracefulTarget()  # noqa: SLF001
    target.attach(_Loop(), sentinel)
    target.dispatch()

    assert scheduled == [sentinel]  # graceful: callback handed to the loop, not exit


def test_graceful_target_forces_exit_in_startup_window(monkeypatch, caplog) -> None:
    """Before a loop is attached, a signal forces an attributed process exit.

    Regression for the startup window (reviewer P2 on PR #6452): blocking
    SIGTERM/SIGINT without a running consumer left a signal pending until
    ``build_orchestrator`` finished, so a slow/hung startup could only be killed
    with SIGKILL. The consumer must instead stop the process here so the engine
    stays stoppable with SIGTERM.
    """
    exited: list[int] = []
    monkeypatch.setattr(ss, "_force_exit", lambda code: exited.append(code))

    target = ss._GracefulTarget()  # noqa: SLF001 — unattached == startup window
    with caplog.at_level(logging.WARNING):
        target.dispatch()

    assert exited == [ss._STARTUP_SIGNAL_EXIT_CODE]  # noqa: SLF001
    assert "before the orchestrator was ready" in caplog.text


def test_begin_shutdown_watch_returns_false_when_unsupported(monkeypatch) -> None:
    """On the asyncio-fallback platform, no early consumer is started.

    Signals aren't blocked there, so the OS default already stops a hung startup;
    the consumer installs later in ``install_attributed_shutdown``.
    """
    monkeypatch.setattr(ss, "supports_sender_attribution", lambda: False)
    assert ss.begin_shutdown_watch() is False


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


# Child process for the STARTUP-WINDOW regression: block signals + start the
# early consumer, then simulate a slow/hung orchestrator build by waiting WITHOUT
# ever attaching a graceful target. A SIGTERM in this window must still stop the
# process — before the fix it stayed pending until build completed.
_STARTUP_WINDOW_CHILD = textwrap.dedent(
    """
    import logging, sys, time
    from issue_orchestrator.infra import shutdown_signals as ss

    logging.basicConfig(filename=sys.argv[1], level=logging.WARNING,
                        format="%(message)s")
    assert ss.block_shutdown_signals() is True
    assert ss.begin_shutdown_watch() is True
    print("READY", flush=True)
    # Never attach a graceful target: stand in for a slow/hung build.
    time.sleep(30)
    print("NOT_STOPPED", flush=True)  # must never be reached
    """
)


@pytest.mark.skipif(
    not ss.supports_sender_attribution(),
    reason="signal-sender capture requires POSIX sigwaitinfo (unavailable on macOS)",
)
def test_startup_window_signal_still_stops_process(tmp_path: Path) -> None:
    """A SIGTERM during the pre-loop startup window stops the process promptly.

    Regression for PR #6452 reviewer P2: with signals blocked but no consumer
    yet, the signal stayed pending and a hung startup needed SIGKILL. Now the
    early consumer attributes it and forces exit.
    """
    log_file = tmp_path / "startup_child.log"
    proc = subprocess.Popen(
        [sys.executable, "-c", _STARTUP_WINDOW_CHILD, str(log_file)],
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
        time.sleep(0.3)  # let the watcher enter sigwaitinfo

        os.kill(proc.pid, signal.SIGTERM)
        # Before the fix this stayed pending and the child slept the full 30s.
        proc.wait(timeout=10)
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)

    assert proc.returncode == ss._STARTUP_SIGNAL_EXIT_CODE, (  # noqa: SLF001
        f"startup-window SIGTERM did not stop the process cleanly; rc={proc.returncode}"
    )
    logged = log_file.read_text(encoding="utf-8")
    assert f"from pid={os.getpid()}" in logged  # sender attributed
    assert "before the orchestrator was ready" in logged  # took the startup-exit path
