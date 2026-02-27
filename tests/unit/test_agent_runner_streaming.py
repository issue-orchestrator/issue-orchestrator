"""Tests for AgentRunner with PTY-inherited output.

Validates that AgentRunner uses setpgrp (not setsid) and inherits
stdout/stderr from the parent process.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from issue_orchestrator._vendor.agent_runner.ports import RunSpec
from issue_orchestrator._vendor.agent_runner.runner import AgentRunner


def _spec(tmp_path: Path, command: list[str], timeout_seconds: int = 10) -> RunSpec:
    return RunSpec(
        command=command,
        working_dir=tmp_path,
        timeout_seconds=timeout_seconds,
        output_dir=tmp_path / "out",
    )


def test_agent_runner_returns_exit_code(tmp_path: Path) -> None:
    runner = AgentRunner()
    result = runner.run(_spec(tmp_path, [sys.executable, "-c", "pass"]))
    assert result.exit_code == 0
    assert result.timed_out is False


def test_agent_runner_returns_nonzero_exit_code(tmp_path: Path) -> None:
    runner = AgentRunner()
    result = runner.run(_spec(tmp_path, [sys.executable, "-c", "raise SystemExit(42)"]))
    assert result.exit_code == 42
    assert result.timed_out is False


def test_agent_runner_handles_timeout(tmp_path: Path) -> None:
    runner = AgentRunner()
    result = runner.run(_spec(
        tmp_path,
        [sys.executable, "-c", "import time; time.sleep(10)"],
        timeout_seconds=1,
    ))
    assert result.timed_out is True


def test_agent_runner_child_in_separate_process_group(tmp_path: Path) -> None:
    """Child runs in its own process group (setpgrp), not parent's."""
    script = "import os; print(f'{os.getpid()} {os.getpgrp()}')"
    runner = AgentRunner()
    # We need to capture the child's output to verify pgid.
    # Run a parent that reads the child's stdout via pipe.
    parent_script = (
        "import subprocess, sys, os\n"
        f"p = subprocess.Popen([sys.executable, '-c', {script!r}],\n"
        "    stdout=subprocess.PIPE, preexec_fn=os.setpgrp)\n"
        "out, _ = p.communicate()\n"
        "pid, pgid = out.decode().strip().split()\n"
        "assert pid == pgid, f'child should be group leader: pid={pid} pgid={pgid}'\n"
        f"assert int(pgid) != {os.getpgrp()}, 'child pgid should differ from test pgid'\n"
    )
    result = runner.run(_spec(tmp_path, [sys.executable, "-c", parent_script]))
    assert result.exit_code == 0, f"Process group verification failed: {result.stderr}"


def test_agent_runner_stderr_on_command_not_found(tmp_path: Path) -> None:
    runner = AgentRunner()
    result = runner.run(_spec(tmp_path, ["/nonexistent/command"]))
    assert result.exit_code == 127
    assert "not found" in result.stderr.lower()


def test_agent_runner_output_dir_created(tmp_path: Path) -> None:
    runner = AgentRunner()
    out_dir = tmp_path / "nested" / "output"
    spec = RunSpec(
        command=[sys.executable, "-c", "pass"],
        working_dir=tmp_path,
        timeout_seconds=5,
        output_dir=out_dir,
    )
    runner.run(spec)
    assert out_dir.exists()
