"""Execution-layer helpers for short-lived process management."""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from typing import Final

_TRAY_MODULE: Final[str] = "issue_orchestrator.entrypoints.tray"


@dataclass
class ManagedProcess:
    """Small wrapper around subprocess lifecycle used by entrypoints."""

    process: subprocess.Popen[bytes]

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def returncode(self) -> int | None:
        return self.process.returncode

    def poll(self) -> int | None:
        return self.process.poll()

    def stop(self, *, graceful_timeout_seconds: float = 2.0) -> None:
        """Terminate and escalate to kill if still alive."""
        if self.process.poll() is not None:
            return
        self.process.terminate()
        try:
            self.process.wait(timeout=graceful_timeout_seconds)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait(timeout=graceful_timeout_seconds)


def list_processes_matching(pattern: str) -> list[tuple[int, str]]:
    """Return (pid, command) rows for pgrep pattern matches."""
    try:
        output = subprocess.check_output(
            ["pgrep", "-af", pattern],
            text=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError, ValueError):
        return []

    rows: list[tuple[int, str]] = []
    for line in output.splitlines():
        parts = line.strip().split(maxsplit=1)
        if not parts:
            continue
        try:
            pid = int(parts[0])
        except ValueError:
            continue
        cmd = parts[1] if len(parts) > 1 else ""
        rows.append((pid, cmd))
    return rows


def spawn_tray_helper(*, dashboard_url: str, owner_pid: int) -> ManagedProcess:
    """Start the tray helper process."""
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            _TRAY_MODULE,
            "--dashboard-url",
            dashboard_url,
            "--owner-pid",
            str(owner_pid),
        ],
    )
    return ManagedProcess(process=process)
