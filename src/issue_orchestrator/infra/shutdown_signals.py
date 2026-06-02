"""Attributed shutdown-signal handling.

When an orchestrator dies on a SIGTERM, operators need to know **who** sent it,
not just that one arrived. Python's asyncio ``add_signal_handler`` does not
expose the sender, so a bare ``kill`` from outside the orchestrator system is
unattributable — the handler can only log ``os.getppid()`` (the *parent*), which
is a red herring: the parent is rarely the sender, and blaming it has sent real
investigations down the wrong path.

POSIX *does* report the sender via ``siginfo.si_pid`` — but only through
``sigwaitinfo``/``sigtimedwait``, not through a normal handler. So this module:

1. blocks SIGTERM/SIGINT process-wide (``block_shutdown_signals``, called once at
   the entry point before any threads start, so every thread inherits the block
   and the signals stay pending), and
2. consumes them on a dedicated thread via ``signal.sigwaitinfo``, logging the
   real sender pid/uid and resolved command line, then runs the caller's
   shutdown callback on the event loop.

On platforms without ``sigwaitinfo``/``pthread_sigmask`` (e.g. Windows) it falls
back to asyncio signal handlers — still honest: the fallback message states the
sender is not reported rather than implying the parent sent it.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import subprocess
import threading
from typing import Callable

logger = logging.getLogger(__name__)

# Signals that mean "shut down". SIGTERM is what supervisors/`kill` send;
# SIGINT is Ctrl+C in a foreground terminal.
_SHUTDOWN_SIGNALS: tuple[signal.Signals, ...] = (signal.SIGTERM, signal.SIGINT)


def supports_sender_attribution() -> bool:
    """True if this platform can report the signal sender (POSIX sigwaitinfo)."""
    return hasattr(signal, "sigwaitinfo") and hasattr(signal, "pthread_sigmask")


def block_shutdown_signals() -> bool:
    """Block SIGTERM/SIGINT in this thread (and threads spawned afterward).

    Must be called FIRST in the process entry point, before any threads are
    created, so every thread inherits the block. Then the signals stay pending
    until the dedicated ``sigwaitinfo`` thread dequeues them — which is what lets
    us read the sender. Returns ``True`` if blocking was applied, ``False`` on
    platforms without ``pthread_sigmask`` (caller should use the asyncio
    fallback).
    """
    if not supports_sender_attribution():
        return False
    signal.pthread_sigmask(signal.SIG_BLOCK, set(_SHUTDOWN_SIGNALS))
    return True


def describe_sender(pid: int) -> str:
    """Best-effort command line for a sender pid (it may already be gone)."""
    if pid <= 0:
        return "unknown (not reported)"
    try:
        result = subprocess.run(
            ["ps", "-p", str(pid), "-o", "command="],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except (OSError, subprocess.SubprocessError):
        return "(sender lookup failed)"
    cmd = result.stdout.strip()
    return cmd or "(sender already exited)"


def format_shutdown_signal_log(signum: int, sender_pid: int, sender_uid: int) -> str:
    """Human/operator log line naming the real sender of a shutdown signal."""
    try:
        name = signal.Signals(signum).name
    except ValueError:
        name = f"signal {signum}"
    return (
        f"Received {name} (signum={signum}) from pid={sender_pid} "
        f"(uid={sender_uid}, cmd={describe_sender(sender_pid)!r}); requesting "
        f"shutdown. For attributable stops prefer POST /api/shutdown with a "
        f"'reason'."
    )


def install_attributed_shutdown(
    *,
    loop: asyncio.AbstractEventLoop,
    on_shutdown: Callable[[], None],
) -> None:
    """Detect SIGTERM/SIGINT and run ``on_shutdown`` on ``loop``.

    If ``block_shutdown_signals`` could block the signals on this platform, a
    daemon thread dequeues them with ``sigwaitinfo`` and logs the real sender.
    Otherwise it installs asyncio handlers with an honest "sender not reported"
    message. ``on_shutdown`` always runs on the event loop thread (the same place
    the old asyncio handler ran), so callers can touch loop state safely.
    """
    if supports_sender_attribution():
        _start_sigwait_thread(loop, on_shutdown)
    else:
        _install_asyncio_fallback(loop, on_shutdown)


def _start_sigwait_thread(
    loop: asyncio.AbstractEventLoop,
    on_shutdown: Callable[[], None],
) -> None:
    sigset = set(_SHUTDOWN_SIGNALS)

    def _watch() -> None:
        # Resolve dynamically (not at import): ``sigwaitinfo`` is present on POSIX
        # but absent on macOS/Windows, where this thread is never started. Looking
        # it up via ``getattr`` keeps the type checker from flagging the attribute
        # on stubs that omit it, and respects test monkeypatching of the symbol.
        sigwaitinfo = getattr(signal, "sigwaitinfo", None)
        assert sigwaitinfo is not None  # guaranteed by supports_sender_attribution()
        # Loop so a second signal still re-triggers shutdown (matching the old
        # handler, which fired on every signal) rather than being swallowed.
        while True:
            try:
                info = sigwaitinfo(sigset)
            except (InterruptedError, OSError):
                continue
            logger.warning(
                "%s",
                format_shutdown_signal_log(info.si_signo, info.si_pid, info.si_uid),
            )
            loop.call_soon_threadsafe(on_shutdown)

    threading.Thread(target=_watch, name="shutdown-signal-watch", daemon=True).start()


def _install_asyncio_fallback(
    loop: asyncio.AbstractEventLoop,
    on_shutdown: Callable[[], None],
) -> None:
    def make_handler(sig: signal.Signals) -> Callable[[], None]:
        def handle_signal() -> None:
            try:
                ppid = os.getppid()
            except OSError:
                ppid = -1
            logger.warning(
                "Received %s (signum=%d); this platform does not report the "
                "signal sender (no sigwaitinfo — e.g. macOS), so the source is "
                "unknown. My parent is pid=%d (cmd=%r) — that is NOT necessarily "
                "the sender. For an attributable stop, use POST /api/shutdown "
                "with a 'reason'.",
                sig.name, int(sig), ppid, describe_sender(ppid),
            )
            on_shutdown()
        return handle_signal

    for sig in _SHUTDOWN_SIGNALS:
        loop.add_signal_handler(sig, make_handler(sig))
