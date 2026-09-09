"""Repair one linked-worktree backlink without Git's repository-wide repair sweep."""

import os
import uuid
from pathlib import Path

from ...infra.escrow_files import fsync_directory, read_regular, write_durable
from ...ports.git import Git


def repair_worktree_registration(git: Git, repository: Path, checkout: Path) -> None:
    """Repair only the backlink proven by this checkout's existing gitfile.

    `git worktree repair <path>` also rewrites other registered worktrees. This
    adapter instead verifies the selected common/admin directories and gitfile,
    refuses an admin directory still attached to a present different checkout,
    and atomically replaces only its own unchanged backlink.
    """
    if not checkout.is_absolute() or checkout.resolve() != checkout:
        raise ValueError("worktree registration requires a canonical absolute checkout")
    common = Path(git.run(repository, ["rev-parse", "--path-format=absolute", "--git-common-dir"]).stdout.strip())
    admin = Path(git.run(checkout, ["rev-parse", "--absolute-git-dir"]).stdout.strip())
    if common.resolve() != common or admin.resolve() != admin or admin.parent != common / "worktrees":
        raise ValueError("worktree administration directory is outside this repository")
    gitfile = checkout / ".git"
    expected_gitfile = f"gitdir: {admin}\n".encode()
    if read_regular(gitfile, len(expected_gitfile)) != expected_gitfile:
        raise ValueError("worktree gitfile does not prove this exact administration directory")
    backlink = admin / "gitdir"
    size = backlink.lstat().st_size
    if size > 1_048_576:
        raise ValueError("worktree backlink exceeds size limit")
    before = read_regular(backlink, size)
    expected = f"{gitfile}\n".encode()
    if before == expected:
        return
    previous = Path(os.fsdecode(before).removesuffix("\n"))
    if not previous.is_absolute() or previous.exists() or previous.is_symlink() or previous.parent.exists():
        raise ValueError("worktree administration is still bound to another checkout; retained")
    temporary = admin / f".io-gitdir-{uuid.uuid4().hex}"
    write_durable(temporary, expected)
    if read_regular(backlink, size) != before:
        raise ValueError("worktree backlink changed during repair; retained")
    os.replace(temporary, backlink)
    fsync_directory(admin)
