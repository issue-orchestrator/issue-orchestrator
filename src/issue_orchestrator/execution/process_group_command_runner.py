"""Bounded POSIX command runner that owns descendants through final cleanup."""

import os
from pathlib import Path
import signal
import subprocess
import tempfile
import time

from ..ports.command_runner import CommandResult, OutputNewlines
from ..infra.shutdown_signals import child_signal_reset_preexec


class ProcessGroupCommandRunner:
    def run(self, command: str | list[str], *, cwd: Path | None = None,
            env: dict[str, str] | None = None, timeout_seconds: int | None = None,
            shell: bool = False, newlines: OutputNewlines = OutputNewlines.TRANSLATED) -> CommandResult:
        if timeout_seconds is None or timeout_seconds <= 0:
            raise ValueError("Owned validation commands require a positive deadline")
        if not hasattr(os, "waitid") or not hasattr(os, "killpg"):
            raise RuntimeError("Owned validation commands require POSIX process groups")
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            process = subprocess.Popen(command, cwd=cwd, env=env, shell=shell,
                                       stdout=stdout, stderr=stderr, start_new_session=True,
                                       preexec_fn=child_signal_reset_preexec())
            deadline = time.monotonic() + timeout_seconds
            timed_out = False
            try:
                # WNOWAIT reserves the leader's PID until the entire group is
                # killed. A grandchild closing its output cannot escape cleanup.
                while os.waitid(os.P_PID, process.pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
                    if time.monotonic() >= deadline:
                        timed_out = True
                        break
                    time.sleep(0.02)
            finally:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                process.wait()
            stdout.seek(0)
            stderr.seek(0)
            out = newlines.decode(stdout.read(), errors="replace")
            err = newlines.decode(stderr.read(), errors="replace")
            if newlines is OutputNewlines.TRANSLATED:
                out = out.replace("\r\n", "\n").replace("\r", "\n")
                err = err.replace("\r\n", "\n").replace("\r", "\n")
            return CommandResult(process.returncode, out, err, timed_out)
