"""A clone whose remote-tracking refs disagree with its remote (#9550).

The tech-lead exam's engine checkout, in miniature: a ``--shared`` clone of a
local repository whose ``origin`` is then re-pointed at the run's repository.
``git clone`` turns every LOCAL branch of the source into an ``origin/*``
tracking ref of the clone, and nothing prunes them after the re-point, so the
clone claims branches its remote never had. Exam cases B and I failed when one
of them shared the seeded issue's number and sorted after the seeded branch.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from issue_orchestrator.adapters.git.git_cli import GitCLI
from issue_orchestrator.execution.command_runner import LocalCommandRunner


def git(repo: Path, *args: str) -> str:
    return GitCLI(runner=LocalCommandRunner()).run(repo, list(args)).stdout.strip()


def _commit_repo(path: Path) -> Path:
    path.mkdir()
    git(path, "init", "-q", "-b", "main")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Test User")
    git(path, "config", "commit.gpgsign", "false")
    git(path, "commit", "-q", "--allow-empty", "-m", "base")
    return path


@dataclass(frozen=True)
class EngineClone:
    root: Path
    remote: Path


def clone_with_foreign_tracking_refs(
    tmp_path: Path, *, foreign_branch: str, remote_branch: str, fetched: bool
) -> EngineClone:
    """A clone tracking ``origin/<foreign_branch>``, which its remote lacks.

    The remote holds ``main`` and ``remote_branch`` (the issue's real branch,
    pushed after the clone was made). ``fetched`` is whether the clone has
    fetched since: without it, the clone's tracking refs do not know
    ``remote_branch`` at all.
    """
    source = _commit_repo(tmp_path / "source")
    git(source, "branch", foreign_branch)
    remote = tmp_path / "remote.git"
    git(tmp_path, "clone", "-q", "--bare", str(source), str(remote))
    git(remote, "branch", "-D", foreign_branch)
    root = tmp_path / "engine"
    git(tmp_path, "clone", "-q", "--shared", str(source), str(root))
    git(root, "remote", "set-url", "origin", str(remote))
    git(remote, "branch", remote_branch, "main")
    if fetched:
        git(root, "fetch", "-q", "origin")
    tracked = git(root, "branch", "-r", "--list", "origin/*")
    assert f"origin/{foreign_branch}" in tracked, "the clone must track the foreign branch"
    assert (f"origin/{remote_branch}" in tracked) is fetched
    return EngineClone(root=root, remote=remote)
