"""Process-group subprocess helpers for hook verification probes."""

from __future__ import annotations

import os
import signal
import subprocess
from pathlib import Path


def run_command_in_process_group(
    argv: list[str],
    *,
    cwd: Path,
    env: dict[str, str],
    timeout: int,
) -> subprocess.CompletedProcess[str]:
    """Run a CLI command with timeout ownership over its whole process group."""
    process = subprocess.Popen(
        argv,
        cwd=cwd,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        terminate_process_group(process)
        raise subprocess.TimeoutExpired(
            cmd=argv,
            timeout=timeout,
            output=exc.output,
            stderr=exc.stderr,
        ) from exc
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)


def terminate_process_group(process: subprocess.Popen[str]) -> None:
    try:
        os.killpg(process.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    except OSError:
        process.terminate()

    try:
        process.communicate(timeout=5)
        return
    except subprocess.TimeoutExpired:
        pass

    try:
        os.killpg(process.pid, signal.SIGKILL)
    except ProcessLookupError:
        return
    except OSError:
        process.kill()
    process.communicate()
