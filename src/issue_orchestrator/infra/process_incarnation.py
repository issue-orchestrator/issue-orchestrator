"""Authoritative Linux process-incarnation identity."""

from __future__ import annotations

import sys
from pathlib import Path


LINUX_PROCESS_INCARNATION_PREFIX = "linux-proc-v1:"


def linux_process_incarnation(pid: int) -> str:
    """Bind a PID to one boot and its exact kernel start tick."""
    if sys.platform != "linux":
        raise OSError("Linux process incarnation identity is unavailable")
    boot_id = (
        Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    )
    if not boot_id or ":" in boot_id:
        raise OSError("kernel boot identity is unavailable")
    fields = _process_stat_fields(pid)
    start_ticks = int(fields[19])  # /proc/<pid>/stat field 22
    if start_ticks < 0:
        raise OSError("process start ticks are invalid")
    return f"{LINUX_PROCESS_INCARNATION_PREFIX}{boot_id}:{start_ticks}"


def strongest_process_incarnation(pid: int, historical_started_at: str) -> str:
    """Use exact Linux identity when available, retaining legacy hosts safely."""
    try:
        return linux_process_incarnation(pid)
    except (OSError, ValueError):
        return historical_started_at


def is_linux_process_incarnation(value: str) -> bool:
    """Return whether a persisted identity has exact Linux incarnation data."""
    if not value.startswith(LINUX_PROCESS_INCARNATION_PREFIX):
        return False
    payload = value.removeprefix(LINUX_PROCESS_INCARNATION_PREFIX)
    boot_id, separator, ticks = payload.rpartition(":")
    return bool(boot_id and separator and ticks.isdecimal())


def _process_stat_fields(pid: int) -> list[str]:
    raw = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
    command_end = raw.rfind(")")
    if command_end < 0:
        raise OSError("process stat has no command terminator")
    fields = raw[command_end + 2 :].split()
    if len(fields) <= 19:
        raise OSError("process stat is missing its start time")
    return fields
