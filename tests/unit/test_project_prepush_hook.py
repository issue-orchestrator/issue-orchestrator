"""Tests for the repo's project pre-push hook."""

import subprocess
from pathlib import Path


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)


class TestProjectPrepushHook:
    def test_delegates_to_verify_pr_script(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()

        hook_src = Path(__file__).parent.parent.parent / "hooks" / "pre-push"
        hook_dest = repo / "hooks" / "pre-push"
        hook_dest.parent.mkdir(parents=True, exist_ok=True)
        hook_dest.write_text(hook_src.read_text())
        hook_dest.chmod(0o755)

        log_path = repo / "hook.log"
        _write_executable(
            repo / "scripts" / "verify-pr.sh",
            f"""#!/usr/bin/env bash
echo "verify:$PWD" >> "{log_path}"
exit 0
""",
        )

        result = subprocess.run(
            [str(hook_dest)],
            cwd=repo,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        assert log_path.read_text().splitlines() == [f"verify:{repo}"]

    def test_fails_when_verify_pr_script_fails(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()

        hook_src = Path(__file__).parent.parent.parent / "hooks" / "pre-push"
        hook_dest = repo / "hooks" / "pre-push"
        hook_dest.parent.mkdir(parents=True, exist_ok=True)
        hook_dest.write_text(hook_src.read_text())
        hook_dest.chmod(0o755)

        _write_executable(
            repo / "scripts" / "verify-pr.sh",
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
