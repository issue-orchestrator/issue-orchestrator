from __future__ import annotations

import os
from pathlib import Path
import signal
import subprocess
import sys

from issue_orchestrator._vendor.agent_runner.stream_capture import capture_process_output


def test_capture_process_output_streams_stdout_and_stderr(tmp_path: Path) -> None:
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sys; print('out-line'); print('err-line', file=sys.stderr)",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )
    result = capture_process_output(
        process,
        stdout_path=tmp_path / "stdout.log",
        stderr_path=tmp_path / "stderr.log",
        timeout_seconds=5,
    )

    assert result.timed_out is False
    assert result.exit_code == 0
    assert "out-line" in result.stdout
    assert "err-line" in result.stderr


def test_capture_process_output_timeout_calls_handler_and_keeps_partial_output(tmp_path: Path) -> None:
    process = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import sys,time; "
                "print('before-timeout'); sys.stdout.flush(); "
                "time.sleep(10)"
            ),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
    )

    def _kill_group() -> None:
        os.killpg(os.getpgid(process.pid), signal.SIGTERM)

    result = capture_process_output(
        process,
        stdout_path=tmp_path / "stdout.log",
        stderr_path=tmp_path / "stderr.log",
        timeout_seconds=1,
        on_timeout=_kill_group,
    )

    assert result.timed_out is True
    assert "before-timeout" in result.stdout
    assert (tmp_path / "stdout.log").read_text(errors="replace").strip().startswith("before-timeout")
