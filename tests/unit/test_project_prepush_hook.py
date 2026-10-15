"""Tests for the repo's project pre-push hook."""

import subprocess
from pathlib import Path

from tests.git_push_authorization import authorized_local_fixture_git_env


def _write_executable(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    path.chmod(0o755)


def _write_post_verify_hook(repo: Path, content: str) -> None:
    _write_executable(repo / "repo-specific" / "hooks" / "post-verify", content)


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

    def test_runs_post_verify_hook_after_successful_verify(
        self, tmp_path: Path
    ) -> None:
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
echo verify >> "{log_path}"
exit 0
""",
        )
        _write_post_verify_hook(
            repo,
            f"""#!/usr/bin/env bash
echo "post-verify:$PWD" >> "{log_path}"
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
        lines = log_path.read_text().splitlines()
        assert lines[0] == "verify"
        assert lines[1] == f"post-verify:{repo}"

    def test_fails_when_post_verify_hook_fails(self, tmp_path: Path) -> None:
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
echo verify >> "{log_path}"
exit 0
""",
        )
        _write_post_verify_hook(
            repo,
            f"""#!/usr/bin/env bash
echo post-verify >> "{log_path}"
exit 99
""",
        )

        result = subprocess.run(
            [str(hook_dest)],
            cwd=repo,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 99
        assert log_path.read_text().splitlines() == ["verify", "post-verify"]

    def test_forwards_prepush_args_to_post_verify_hook(
        self, tmp_path: Path
    ) -> None:
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
            """#!/usr/bin/env bash
exit 0
""",
        )
        _write_post_verify_hook(
            repo,
            f"""#!/usr/bin/env bash
printf '%s\\n' "$*" >> "{log_path}"
exit 0
""",
        )

        result = subprocess.run(
            [str(hook_dest), "origin", "git@github.com:owner/repo.git"],
            cwd=repo,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0
        assert log_path.read_text().strip() == "origin git@github.com:owner/repo.git"

    def test_skips_post_verify_hook_when_verify_fails(self, tmp_path: Path) -> None:
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
echo verify >> "{log_path}"
exit 42
""",
        )
        _write_post_verify_hook(
            repo,
            f"""#!/usr/bin/env bash
echo post-verify >> "{log_path}"
exit 99
""",
        )

        result = subprocess.run(
            [str(hook_dest)],
            cwd=repo,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 42
        assert log_path.read_text().splitlines() == ["verify"]


_HOOK_SRC = Path(__file__).parent.parent.parent / "hooks" / "pre-push"


def test_embeds_the_shared_pre_push_ref_functions_verbatim() -> None:
    """hooks/pre-push is a tracked file, so it carries a copy of the shared
    delete-only decision. The copy must not drift from its owner."""
    from issue_orchestrator.infra.hooks.pre_push_refs import pre_push_refs_shell

    assert pre_push_refs_shell() in _HOOK_SRC.read_text()


def _git(cwd: Path, *args: str) -> str:
    # Pushes go to a bare remote under tmp_path; inside an orchestrator session
    # the agent's PATH git wrapper refuses them without the fixture authorization.
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=authorized_local_fixture_git_env(),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, f"git {' '.join(args)}:\n{result.stderr}"
    return result.stdout


def _repo_using_hooks_dir(tmp_path: Path) -> tuple[Path, Path]:
    """The documented ``git config core.hooksPath hooks`` install, with a bare
    remote and a verify-pr stand-in that records each run and its stdin."""
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    _write_executable(repo / "hooks" / "pre-push", _HOOK_SRC.read_text())
    log_path = tmp_path / "verify.log"
    _write_executable(
        repo / "scripts" / "verify-pr.sh",
        f'#!/usr/bin/env bash\necho "=== verify" >> "{log_path}"\ncat >> "{log_path}"\n',
    )
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "seed")
    _git(repo, "config", "core.hooksPath", "hooks")
    _git(repo, "remote", "add", "origin", str(remote))
    _git(repo, "branch", "stale")
    _git(repo, "push", "--no-verify", "origin", "main", "stale")
    return repo, log_path


def test_real_delete_push_skips_verify_pr(tmp_path: Path) -> None:
    repo, log_path = _repo_using_hooks_dir(tmp_path)

    _git(repo, "push", "origin", "--delete", "stale")

    assert not log_path.exists()
    assert "stale" not in _git(repo, "ls-remote", "--heads", "origin")


def test_real_update_push_runs_verify_pr_with_the_ref_lines(tmp_path: Path) -> None:
    repo, log_path = _repo_using_hooks_dir(tmp_path)
    (repo / "file").write_text("change\n")
    _git(repo, "add", "file")
    _git(repo, "commit", "-m", "change")
    head = _git(repo, "rev-parse", "HEAD").strip()

    _git(repo, "push", "origin", "main")

    lines = log_path.read_text().splitlines()
    assert lines[0] == "=== verify"
    assert len(lines) == 2 and lines[1].startswith(f"refs/heads/main {head} ")
