"""Standalone subprocess stream capture primitives.

This module intentionally has no orchestrator-specific dependencies.
It captures process stdout/stderr incrementally into files while also
returning decoded text for caller-side classification/reporting.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import threading
from typing import Callable


@dataclass(frozen=True)
class StreamCaptureResult:
    exit_code: int | None
    timed_out: bool
    stdout: str
    stderr: str


def _drain_stream_to_file(
    stream,
    output_file,
    captured_chunks: list[bytes],
) -> None:
    while True:
        chunk = stream.read(4096)
        if not chunk:
            break
        output_file.write(chunk)
        output_file.flush()
        captured_chunks.append(chunk)


def capture_process_output(
    process: subprocess.Popen,
    *,
    stdout_path: Path,
    stderr_path: Path,
    timeout_seconds: int,
    join_timeout_seconds: int = 5,
    on_timeout: Callable[[], None] | None = None,
) -> StreamCaptureResult:
    """Capture process streams incrementally and return final decoded output."""
    if process.stdout is None or process.stderr is None:
        raise RuntimeError("Process must be started with stdout=PIPE and stderr=PIPE")

    stdout_path.parent.mkdir(parents=True, exist_ok=True)
    stderr_path.parent.mkdir(parents=True, exist_ok=True)

    timed_out = False
    exit_code: int | None = None
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []

    with open(stdout_path, "wb") as stdout_file, open(stderr_path, "wb") as stderr_file:
        stdout_thread = threading.Thread(
            target=_drain_stream_to_file,
            args=(process.stdout, stdout_file, stdout_chunks),
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=_drain_stream_to_file,
            args=(process.stderr, stderr_file, stderr_chunks),
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()

        try:
            exit_code = process.wait(timeout=timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            if on_timeout is not None:
                on_timeout()
            try:
                exit_code = process.wait(timeout=join_timeout_seconds)
            except subprocess.TimeoutExpired:
                exit_code = None

        stdout_thread.join(timeout=join_timeout_seconds)
        stderr_thread.join(timeout=join_timeout_seconds)

    stdout = b"".join(stdout_chunks).decode("utf-8", errors="replace")
    stderr = b"".join(stderr_chunks).decode("utf-8", errors="replace")
    return StreamCaptureResult(
        exit_code=exit_code,
        timed_out=timed_out,
        stdout=stdout,
        stderr=stderr,
    )
