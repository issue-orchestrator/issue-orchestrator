"""Tests for the repo's project pre-push hook."""

import os
import subprocess
from pathlib import Path


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)


class TestProjectPrepushHook:
    def test_runs_required_pr_gate(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()

        hook_src = Path(__file__).parent.parent.parent / "hooks" / "pre-push"
        hook_dest = repo / "pre-push"
        hook_dest.write_text(hook_src.read_text())
        hook_dest.chmod(0o755)

        log_path = repo / "hook.log"
        _write_executable(
            repo / ".venv" / "bin" / "python",
            f"""#!/usr/bin/env bash
echo "python:$*" >> "{log_path}"
exit 0
""",
        )
        _write_executable(
            repo / "bin" / "make",
            f"""#!/usr/bin/env bash
echo "make:$*" >> "{log_path}"
exit 0
""",
        )

        result = subprocess.run(
            [str(hook_dest)],
            cwd=repo,
            capture_output=True,
            text=True,
            env={**os.environ, "PATH": f'{repo / "bin"}:{os.environ.get("PATH", "")}'},
        )

        assert result.returncode == 0
        assert log_path.read_text().splitlines() == [
            "python:-m issue_orchestrator.entrypoints.cli_tools.prepush_check -v",
        ]

    def test_fails_when_required_pr_gate_fails(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()

        hook_src = Path(__file__).parent.parent.parent / "hooks" / "pre-push"
        hook_dest = repo / "pre-push"
        hook_dest.write_text(hook_src.read_text())
        hook_dest.chmod(0o755)

        _write_executable(
            repo / ".venv" / "bin" / "python",
            """#!/usr/bin/env bash
exit 42
""",
        )

        result = subprocess.run(
            [str(hook_dest)],
            cwd=repo,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 42
