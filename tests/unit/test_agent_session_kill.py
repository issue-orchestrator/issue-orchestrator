"""Regression coverage for terminating a PTY agent's child process groups."""

from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from issue_orchestrator.execution.agent_runner import AgentRunner, AgentSpec, terminate_agent_session
from issue_orchestrator.infra.process_table import ps_command, ps_env
from issue_orchestrator.execution.terminal_subprocess import SubprocessPlugin


def _live(pid: int) -> bool:
    result = subprocess.run(
        ps_command("-p", str(pid), "-o", "state="),
        capture_output=True,
        text=True,
        check=False,
        env=ps_env(),
    )
    return bool(result.stdout.strip()) and not result.stdout.lstrip().startswith("Z")


def test_stale_session_pid_does_not_target_another_session() -> None:
    with subprocess.Popen(['/bin/sleep', '300'], process_group=0) as other:
        try:
            assert os.getsid(other.pid) != other.pid
            terminate_agent_session(other.pid)
            assert other.poll() is None
        finally:
            other.terminate()


def test_kill_sends_one_term_and_allows_graceful_cleanup(tmp_path: Path) -> None:
    ready, cleaned = tmp_path / 'ready', tmp_path / 'cleaned'
    script = tmp_path / 'agent.py'
    script.write_text(
        'import signal, time\nfrom pathlib import Path\n'
        'def shutdown(sig, frame):\n'
        '    time.sleep(0.3)\n'
        f'    Path({str(cleaned)!r}).write_text("done")\n'
        '    raise SystemExit(0)\n'
        'signal.signal(signal.SIGTERM, shutdown)\n'
        f'Path({str(ready)!r}).touch()\n'
        'time.sleep(300)\n'
    )
    session = AgentRunner().start(AgentSpec(
        command=[sys.executable, str(script)], working_dir=tmp_path,
        timeout_seconds=300, output_dir=tmp_path / 'output',
    ))
    try:
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert ready.exists()
        session.kill()
        assert cleaned.read_text() == 'done'
    finally:
        session.kill()


def test_kill_drains_workers_after_wait_closed_the_pty(tmp_path: Path) -> None:
    worker_file = tmp_path / "worker.pid"
    script = tmp_path / "agent.py"
    script.write_text(
        "import subprocess\nfrom pathlib import Path\n"
        "worker = subprocess.Popen(['/bin/sleep', '300'], process_group=0, "
        "stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)\n"
        f"Path({str(worker_file)!r}).write_text(str(worker.pid))\n"
    )
    session = AgentRunner().start(AgentSpec(
        command=[sys.executable, str(script)], working_dir=tmp_path,
        timeout_seconds=300, output_dir=tmp_path / "output",
    ))
    worker_pid = None
    try:
        assert session.wait(timeout=5).exit_code == 0
        worker_pid = int(worker_file.read_text())
        assert _live(worker_pid)
        session.kill()
        assert not _live(worker_pid)
    finally:
        if worker_pid is not None and _live(worker_pid):
            os.kill(worker_pid, signal.SIGKILL)
        session.kill()


@pytest.mark.parametrize("ignore_term", [False, True])
@pytest.mark.parametrize("new_session", [False, True])
@pytest.mark.parametrize("recovered", [False, True])
def test_kill_stops_worker_in_another_group_of_the_agent_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ignore_term: bool,
    new_session: bool, recovered: bool,
) -> None:
    """A shell/tool child may setpgrp while retaining the agent's session."""
    worker_pid_file = tmp_path / "worker.pid"
    agent_script = tmp_path / "agent.py"
    agent_script.write_text(
        "import signal, subprocess, time\n"
        "from pathlib import Path\n"
        f"signal.signal(signal.SIGTERM, signal.SIG_IGN if {ignore_term!r} else signal.SIG_DFL)\n"
        f"worker = subprocess.Popen(['/bin/sleep', '300'], **{({'start_new_session': True} if new_session else {'process_group': 0})!r})\n"
        "signal.signal(signal.SIGTERM, signal.SIG_DFL)\n"
        f"Path({str(worker_pid_file)!r}).write_text(str(worker.pid))\n"
        "time.sleep(300)\n"
    )
    spec = AgentSpec(
        command=[sys.executable, str(agent_script)],
        working_dir=tmp_path,
        timeout_seconds=300,
        output_dir=tmp_path / "output",
    )
    plugin = None
    session = None
    if recovered:
        monkeypatch.setenv("ISSUE_ORCHESTRATOR_REPO_ROOT", str(tmp_path))
        plugin = SubprocessPlugin()
        command = f"export ISSUE_ORCHESTRATOR_RUN_DIR='{tmp_path / 'output'}' && {sys.executable} {agent_script}"
        assert plugin.create_session(42, command, str(tmp_path), "fixture", "issue-42")
    else:
        session = AgentRunner().start(spec)
    worker_pid: int | None = None
    try:
        deadline = time.monotonic() + 5
        while not worker_pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert worker_pid_file.exists()
        worker_pid = int(worker_pid_file.read_text())
        if session is not None:
            assert session.pid is not None
            assert os.getpgid(worker_pid) != os.getpgid(session.pid)
            assert (os.getsid(worker_pid) != os.getsid(session.pid)) == new_session
            session.kill()
        else:
            # A fresh backend has the persisted registry, but no PTY handles.
            assert SubprocessPlugin().kill_session(42, "issue-42")

        assert not _live(worker_pid)
    finally:
        if worker_pid is not None and _live(worker_pid):
            os.kill(worker_pid, signal.SIGKILL)
        if session is not None:
            session.kill()
        if plugin is not None:
            plugin.kill_session(42, "issue-42")
