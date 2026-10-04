"""Regression coverage for terminating a PTY agent's child process groups."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from issue_orchestrator.execution.agent_runner import AgentRunner, AgentSpec


def _live(pid: int) -> bool:
    result = subprocess.run(
        ["ps", "-p", str(pid), "-o", "state="],
        capture_output=True,
        text=True,
        check=False,
    )
    return bool(result.stdout.strip()) and not result.stdout.lstrip().startswith("Z")


def test_kill_stops_worker_in_another_group_of_the_agent_session(tmp_path: Path) -> None:
    """A shell/tool child may setpgrp while retaining the agent's session."""
    worker_pid_file = tmp_path / "worker.pid"
    agent_script = tmp_path / "agent.py"
    agent_script.write_text(
        "import subprocess, time\n"
        "from pathlib import Path\n"
        "worker = subprocess.Popen(['/bin/sleep', '300'], process_group=0)\n"
        f"Path({str(worker_pid_file)!r}).write_text(str(worker.pid))\n"
        "time.sleep(300)\n"
    )
    spec = AgentSpec(
        command=[sys.executable, str(agent_script)],
        working_dir=tmp_path,
        timeout_seconds=300,
        output_dir=tmp_path / "output",
    )
    session = AgentRunner().start(spec)
    worker_pid: int | None = None
    try:
        deadline = time.monotonic() + 5
        while not worker_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert worker_pid_file.exists()
        worker_pid = int(worker_pid_file.read_text())
        assert os.getpgid(worker_pid) != os.getpgid(session.pid)
        assert os.getsid(worker_pid) == os.getsid(session.pid)

        session.kill()

        assert not _live(worker_pid)
    finally:
        if worker_pid is not None and _live(worker_pid):
            os.kill(worker_pid, signal.SIGKILL)
        session.kill()
