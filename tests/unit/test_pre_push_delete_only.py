"""A push that only deletes remote refs skips io's validation gate (#7765).

The decision is ``push_is_delete_only`` in ``infra/hooks/pre_push_refs.py``,
rendered into the managed repo wrapper, the worktree chained wrapper and the
bundled orchestrator hook. These tests drive it three ways:

- the shared shell function directly, against hand-written ref lines;
- the real generated repo wrapper, on real ``git push`` runs to a bare remote;
- the real worktree hooks, on real ``git push`` runs from a linked worktree.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from issue_orchestrator.adapters.worktree._worktree_hooks import install_hooks
from issue_orchestrator.control.pre_publish_gate import PrePublishGate
from issue_orchestrator.domain.models import AgentConfig
from issue_orchestrator.execution.command_runner import LocalCommandRunner
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.hooks.pre_push_refs import pre_push_refs_shell
from issue_orchestrator.infra.repo_guardrails import setup_repo_guardrails
from tests.git_push_authorization import authorized_local_fixture_git_env

ZERO_SHA1 = "0" * 40
ZERO_SHA256 = "0" * 64
SHA_A = "a" * 40
SHA_B = "b" * 40
GIT_TIMEOUT_S = 60


def _delete_line(branch: str, remote_sha: str = SHA_A) -> str:
    return f"(delete) {ZERO_SHA1} refs/heads/{branch} {remote_sha}\n"


def _update_line(branch: str, local_sha: str = SHA_B, remote_sha: str = SHA_A) -> str:
    return f"refs/heads/{branch} {local_sha} refs/heads/{branch} {remote_sha}\n"


def _decide(tmp_path: Path, ref_lines: str) -> bool:
    """Run the shared shell decision on *ref_lines*; True means delete-only."""
    refs_file = tmp_path / "refs"
    refs_file.write_text(ref_lines)
    script = (
        "set -euo pipefail\n"
        + pre_push_refs_shell()
        + 'if push_is_delete_only "$1"; then echo SKIP; else echo GATE; fi\n'
    )
    result = subprocess.run(
        ["bash", "-c", script, "decide", str(refs_file)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_S,
        check=True,
    )
    verdict = result.stdout.strip()
    assert verdict in {"SKIP", "GATE"}, result
    return verdict == "SKIP"


# --- the decision ---------------------------------------------------------


@pytest.mark.parametrize(
    "ref_lines",
    [
        pytest.param(_delete_line("stale"), id="one-delete"),
        pytest.param(_delete_line("a") + _delete_line("b"), id="two-deletes"),
        pytest.param(_delete_line("stale").rstrip("\n"), id="no-trailing-newline"),
        pytest.param(
            f"(delete) {ZERO_SHA256} refs/heads/stale {'c' * 64}\n", id="sha256"
        ),
    ],
)
def test_delete_only_ref_lines_skip_the_gate(tmp_path: Path, ref_lines: str) -> None:
    assert _decide(tmp_path, ref_lines) is True


@pytest.mark.parametrize(
    "ref_lines",
    [
        pytest.param("", id="empty-stdin"),
        pytest.param("\n", id="blank-line"),
        pytest.param(_update_line("main"), id="update-only"),
        pytest.param(_delete_line("stale") + _update_line("main"), id="delete-then-update"),
        pytest.param(_update_line("main") + _delete_line("stale"), id="update-then-delete"),
        pytest.param(_delete_line("stale") + "\n", id="delete-then-blank"),
        pytest.param(
            f"refs/heads/new {SHA_B} refs/heads/new {ZERO_SHA1}\n", id="new-branch"
        ),
        pytest.param(
            f"(delete) {ZERO_SHA1} refs/heads/stale {SHA_A} extra\n", id="extra-field"
        ),
        pytest.param(f"(delete) {ZERO_SHA1} refs/heads/stale\n", id="missing-field"),
        pytest.param(
            f"(delete) {'0' * 39} refs/heads/stale {SHA_A}\n", id="short-zero-sha"
        ),
        pytest.param(
            f"refs/heads/x {ZERO_SHA1} refs/heads/x {SHA_A}\n", id="zero-sha-not-delete"
        ),
        pytest.param(f"(delete) {ZERO_SHA1} stale {SHA_A}\n", id="remote-not-a-ref"),
        pytest.param("not a ref line at all\n", id="garbage"),
    ],
)
def test_anything_but_delete_only_runs_the_gate(tmp_path: Path, ref_lines: str) -> None:
    assert _decide(tmp_path, ref_lines) is False


# --- real pushes through the generated repo wrapper -------------------------


def _git(cwd: Path, *args: str, env: dict[str, str] | None = None) -> str:
    """Run git in a throwaway fixture repo, whose pushes go to a local bare
    remote and so carry the fixture push authorization: inside an agent
    session the ``git`` on PATH is the wrapper that refuses ``git push``."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        env=authorized_local_fixture_git_env(env),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_S,
    )
    assert result.returncode == 0, f"git {' '.join(args)} failed:\n{result.stderr}"
    return result.stdout


def _init_repo_with_remote(tmp_path: Path) -> tuple[Path, Path]:
    remote = tmp_path / "remote.git"
    _git(tmp_path, "init", "--bare", "-b", "main", str(remote))
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "T")
    (repo / "file").write_text("seed\n")
    _git(repo, "add", "file")
    _git(repo, "commit", "-m", "seed")
    _git(repo, "remote", "add", "origin", str(remote))
    return repo, remote


def _recorder(path: Path, name: str, stdin_copy: Path) -> None:
    """Write a hook stand-in that appends its stdin and a run marker."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "#!/usr/bin/env bash\n"
        "set -euo pipefail\n"
        f'printf "=== {name}\\n" >> {stdin_copy}\n'
        f"cat >> {stdin_copy}\n"
    )
    path.chmod(0o755)


@pytest.fixture
def guarded_repo(tmp_path: Path) -> tuple[Path, Path]:
    """A repo with real setup-guardrails output and a bare remote.

    verify-pr.sh is swapped for a recorder after install: the wrapper is the
    unit under test, and the real verify-pr would run the full gate.
    """
    repo, _remote = _init_repo_with_remote(tmp_path)
    config = Config(repo_root=repo)
    config.validation.publish.cmd = "make validate-pr"
    config.agents = {
        "agent:dev": AgentConfig(prompt_path=repo / "prompt.md", command="claude --print")
    }
    result = setup_repo_guardrails(config)
    stdin_log = tmp_path / "stdin.log"
    _recorder(result.verify_script, "verify-pr", stdin_log)
    _recorder(result.hooks_dir / "pre-push.project", "project", stdin_log)
    # The repo's own feature branches, pushed once so they can be deleted.
    _git(repo, "branch", "stale-a")
    _git(repo, "branch", "stale-b")
    _git(repo, "push", "--no-verify", "origin", "main", "stale-a", "stale-b")
    return repo, stdin_log


def _sections(stdin_log: Path) -> dict[str, list[str]]:
    """Group the recorded stdin by consumer, in run order."""
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    if not stdin_log.exists():
        return sections
    for line in stdin_log.read_text().splitlines():
        if line.startswith("=== "):
            current = sections.setdefault(line.removeprefix("=== "), [])
            continue
        assert current is not None, line
        current.append(line)
    return sections


def _hook_log(repo: Path) -> str:
    return (repo / ".githooks" / "pre-push.log").read_text()


def test_real_delete_push_skips_verify_pr(guarded_repo: tuple[Path, Path]) -> None:
    repo, stdin_log = guarded_repo

    _git(repo, "push", "origin", "--delete", "stale-a")

    sections = _sections(stdin_log)
    assert "verify-pr" not in sections
    # The project hook still runs, and still gets git's ref line.
    assert len(sections["project"]) == 1
    assert sections["project"][0].startswith(f"(delete) {ZERO_SHA1} refs/heads/stale-a ")
    log = _hook_log(repo)
    assert "verify-pr-skipped reason=delete-only" in log
    assert "verify-pr-starting" not in log
    assert "repo-pre-push-completed" in log
    assert "stale-a" not in _git(repo, "ls-remote", "--heads", "origin")


def test_real_update_push_runs_verify_pr_with_the_ref_lines(
    guarded_repo: tuple[Path, Path],
) -> None:
    repo, stdin_log = guarded_repo
    (repo / "file").write_text("change\n")
    _git(repo, "commit", "-am", "change")
    head = _git(repo, "rev-parse", "HEAD").strip()

    _git(repo, "push", "origin", "main")

    sections = _sections(stdin_log)
    project_lines = sections["project"]
    assert len(project_lines) == 1
    assert project_lines[0].startswith(f"refs/heads/main {head} ")
    # verify-pr gets the same ref lines the project hook already consumed.
    assert sections["verify-pr"] == project_lines
    log = _hook_log(repo)
    assert "verify-pr-starting" in log
    assert "verify-pr-skipped" not in log


def test_real_mixed_push_runs_verify_pr(guarded_repo: tuple[Path, Path]) -> None:
    repo, stdin_log = guarded_repo
    (repo / "file").write_text("change\n")
    _git(repo, "commit", "-am", "change")

    _git(repo, "push", "origin", "main", ":refs/heads/stale-b")

    sections = _sections(stdin_log)
    assert len(sections["verify-pr"]) == 2
    assert any(line.startswith("(delete) ") for line in sections["verify-pr"])
    assert "verify-pr-skipped" not in _hook_log(repo)


def test_wrapper_with_no_ref_lines_runs_verify_pr(
    guarded_repo: tuple[Path, Path],
) -> None:
    repo, stdin_log = guarded_repo

    result = subprocess.run(
        [str(repo / ".githooks" / "pre-push"), "origin", "unused-url"],
        cwd=repo,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_S,
    )

    assert result.returncode == 0, result.stderr
    assert _sections(stdin_log)["verify-pr"] == []
    assert "verify-pr-starting" in _hook_log(repo)


# --- real pushes from a linked worktree -------------------------------------


@pytest.fixture
def worktree_with_hooks(tmp_path: Path) -> tuple[Path, Path, dict[str, str]]:
    """A linked worktree whose hooks come from the real installer.

    The main repo carries a project hook in ``.githooks/pre-push``, so the
    worktree gets the chained wrapper around ``pre-push.project`` and the
    bundled ``pre-push.orchestrator``. The interpreter the bundled hook runs
    for its dirty-tree guard is a recorder named by ISSUE_ORCHESTRATOR_PYTHON.
    """
    repo, _remote = _init_repo_with_remote(tmp_path)
    stdin_log = tmp_path / "stdin.log"
    _recorder(repo / ".githooks" / "pre-push", "project", stdin_log)
    _git(repo, "config", "--local", "core.hooksPath", ".githooks")
    _git(repo, "branch", "stale")
    _git(repo, "push", "--no-verify", "origin", "main", "stale")
    worktree = tmp_path / "wt"
    _git(repo, "worktree", "add", str(worktree), "-b", "feature")
    assert install_hooks(worktree) is True

    guard_python = tmp_path / "guard-python"
    guard_python.write_text(
        "#!/usr/bin/env bash\n"
        f'printf "=== dirty-guard %s\\n" "$*" >> {stdin_log}\n'
    )
    guard_python.chmod(0o755)
    env = {**os.environ, "ISSUE_ORCHESTRATOR_PYTHON": str(guard_python)}
    return worktree, stdin_log, env


def test_worktree_delete_push_skips_the_orchestrator_guard(
    worktree_with_hooks: tuple[Path, Path, dict[str, str]],
) -> None:
    worktree, stdin_log, env = worktree_with_hooks

    _git(worktree, "push", "origin", "--delete", "stale", env=env)

    sections = _sections(stdin_log)
    assert not any(name.startswith("dirty-guard") for name in sections)
    assert sections["project"][0].startswith(f"(delete) {ZERO_SHA1} refs/heads/stale ")


def test_worktree_update_push_runs_the_orchestrator_guard(
    worktree_with_hooks: tuple[Path, Path, dict[str, str]],
) -> None:
    worktree, stdin_log, env = worktree_with_hooks

    _git(worktree, "push", "origin", "feature", env=env)

    sections = _sections(stdin_log)
    assert sections["project"][0].startswith("refs/heads/feature ")
    assert any(name.startswith("dirty-guard") for name in sections)


# --- the pre-publish rehearsal ----------------------------------------------


def test_pre_publish_gate_gives_the_hook_no_ref_lines(tmp_path: Path) -> None:
    """The rehearsal must not hand the hook the engine's own stdin.

    The generated hooks read stdin to EOF. Here the engine's stdin is a pipe
    with a delete line on it: inherited, it would make the rehearsal look
    like a delete-only push and skip the gate it exists to run.
    """
    repo, _remote = _init_repo_with_remote(tmp_path)
    hooks_dir = repo / ".git" / "hooks"
    seen = tmp_path / "seen"
    (hooks_dir / "pre-push").write_text(
        f"#!/usr/bin/env bash\ncat > {seen}\n"
    )
    (hooks_dir / "pre-push").chmod(0o755)

    read_end, write_end = os.pipe()
    os.write(write_end, _delete_line("stale").encode())
    os.close(write_end)
    saved_stdin = os.dup(0)
    try:
        os.dup2(read_end, 0)
        result = PrePublishGate(LocalCommandRunner()).check(repo)
    finally:
        os.dup2(saved_stdin, 0)
        os.close(saved_stdin)
        os.close(read_end)

    assert result.allowed, result.reason
    assert seen.read_text() == ""
