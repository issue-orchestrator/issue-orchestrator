"""Linux pidfd primitives for lifetime-pinned single-process signalling."""

from __future__ import annotations

import os
import select
import signal
import sys
import time
from math import ceil
from typing import Callable, cast

from .process_incarnation import linux_process_incarnation


class LinuxPidfdRuntime:
    """Small OS boundary whose handle cannot retarget after PID reuse."""

    def supported(self) -> bool:
        if sys.platform != "linux":
            return False
        handle: int | None = None
        usable = False
        try:
            self.process_incarnation(os.getpid())
            handle = self.open(os.getpid())
            self.send(handle, 0)
            usable = True
        except (OSError, ValueError):
            usable = False
        finally:
            if handle is not None:
                try:
                    self.close(handle)
                except OSError:
                    usable = False
        return usable

    def open(self, pid: int) -> int:
        opener = getattr(os, "pidfd_open", None)
        if not callable(opener):
            raise OSError("pidfd_open is unavailable")
        return cast(Callable[[int, int], int], opener)(pid, 0)

    def process_incarnation(self, pid: int) -> str:
        return linux_process_incarnation(pid)

    def send(self, handle: int, sig: int | signal.Signals) -> None:
        sender = getattr(signal, "pidfd_send_signal", None)
        if not callable(sender):
            raise OSError("pidfd_send_signal is unavailable")
        sender(handle, sig, None, 0)

    def wait_for_exit(self, handle: int, timeout_seconds: float) -> bool:
        deadline = time.monotonic() + timeout_seconds
        poller = select.poll()
        poller.register(handle, select.POLLIN)
        while True:
            remaining = max(0.0, deadline - time.monotonic())
            try:
                events = poller.poll(ceil(remaining * 1000))
            except InterruptedError:
                continue
            for _fd, mask in events:
                if mask & (select.POLLNVAL | select.POLLERR):
                    raise OSError("pidfd exit observation failed")
                if mask & (select.POLLIN | select.POLLHUP):
                    return True
            if remaining <= 0:
                return False

    def close(self, handle: int) -> None:
        os.close(handle)
