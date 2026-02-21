from __future__ import annotations

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


def test_agent_runner_captures_stdout_and_stderr(tmp_path: Path) -> None:
    runner = AgentRunner()
    command = [
        sys.executable,
        "-c",
        "import sys; print('hello-out'); print('hello-err', file=sys.stderr)",
    ]
    result = runner.run(_spec(tmp_path, command))

    assert result.exit_code == 0
    assert result.timed_out is False
    assert "hello-out" in result.stdout
    assert "hello-err" in result.stderr
    assert result.stdout_path.read_text(errors="replace").strip().endswith("hello-out")
    assert result.stderr_path.read_text(errors="replace").strip().endswith("hello-err")


def test_agent_runner_preserves_partial_output_on_timeout(tmp_path: Path) -> None:
    runner = AgentRunner()
    command = [
        sys.executable,
        "-c",
        (
            "import sys,time; "
            "sys.stdout.write('partial-out\\n'); sys.stdout.flush(); "
            "sys.stderr.write('partial-err\\n'); sys.stderr.flush(); "
            "time.sleep(5)"
        ),
    ]
    result = runner.run(_spec(tmp_path, command, timeout_seconds=1))

    assert result.timed_out is True
    assert "partial-out" in result.stdout
    assert "partial-err" in result.stderr
    assert "partial-out" in result.stdout_path.read_text(errors="replace")
    assert "partial-err" in result.stderr_path.read_text(errors="replace")
