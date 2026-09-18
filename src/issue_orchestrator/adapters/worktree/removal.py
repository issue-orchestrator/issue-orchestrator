"""The one place a git worktree checkout is removed (#7274).

Before this there were nine: four lifecycle paths, a reuse-cleanup fallback, a
reviewer rollback, a publication workspace, a validation lane, an E2E fixture
and a doctor repair. Each built its own ``git worktree remove``, and most had
their own ``shutil.rmtree`` fallback for when git declined -- which is why
custody had to be added in nine places, and why it kept turning out to be
missing from a tenth.

Everything routes through :func:`remove_checkout_path` now. Custody is asked
HERE, once, and the answer is held for the whole removal INCLUDING the
filesystem fallback. A removal path that does not consult custody is therefore
not something to catch in review: it is something that does not exist, because
``tests/unit/test_worktree_custody.py`` refuses a ``git worktree remove`` built
anywhere else.

Each caller passes its own way of running git rather than sharing one, because
the layers genuinely differ -- a ``Git`` port here, a ``CommandRunner`` there,
a plain subprocess in the E2E fixture. What this owns is the COMMAND and the
order of the two attempts, not the transport.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from ...domain.escrow_retention_boundary import require_disposable_path
from ...ports.worktree_custody import CustodyRelease
from .custody import custody_guard

logger = logging.getLogger(__name__)

#: Runs ``git`` with the given arguments in the caller's repository.
#: Returns ``None`` when git succeeded, or the error text when it did not.
GitRunner = Callable[[list[str]], "str | None"]


@dataclass(frozen=True)
class CheckoutRemoval:
    """What happened, so a caller decides without parsing a message."""

    removed: bool
    used_filesystem_fallback: bool
    git_error: str = ""


def remove_checkout_path(
    worktree_path: Path,
    *,
    force: bool,
    run_git: GitRunner | None,
    repo_root: Path,
    custody_release: CustodyRelease | None = None,
    prune: bool = False,
) -> CheckoutRemoval:
    """Remove one checkout, asking custody first and holding the answer.

    Args:
        worktree_path: The checkout to remove.
        force: Pass ``--force`` to git, and delete the directory when git still
            declines. It does NOT release custody: discarding someone's only
            copy of a branch has to be something a caller said, not a side
            effect of asking git to try harder.
        run_git: How to run git here, or ``None`` for a path whose own ``.git``
            file is gone -- then only the filesystem attempt is possible.
        repo_root: Which repository to ask about custody. An authoritative
            ``Path``, always. Custody lives in the repository's metadata and a
            checkout that has lost its ``.git`` file names none, so a removal
            that cannot say which store to read cannot say the checkout is
            unheld either. There used to be an ``UNKNOWN_REPOSITORY`` sentinel
            for "nobody here knows", which proceeded and left the loss to
            ``breached()`` -- a fail-OPEN hole in a contract whose whole premise
            is that an unreadable store is not an empty one (round 8 finding 2).
            A caller with no repository must not call this.
        custody_release: The explicit intent to end a grant as part of this
            removal.
        prune: Run ``git worktree prune`` after a successful removal.

    Raises:
        WorktreeInCustodyError: The checkout is held and no release was given.
        CustodyUnavailableError: Whether it is held could not be determined.
    """
    require_disposable_path(worktree_path)
    if worktree_path.resolve() == repo_root.resolve():
        # The repository's own anchor. Its custody state and append-only trail
        # live under ITS `.git`, so a forced removal deletes the audit trail it
        # just wrote -- the release becomes unauditable at the moment it is
        # exercised, and settlement reads an empty store (round 9 finding 3).
        # The orchestrator has no reason to delete its own repository, so this
        # is refused rather than made to work: supporting it would mean moving
        # the store outside the repository, which is a different change.
        raise ValueError(
            f"{worktree_path} is the repository itself, not a disposable "
            "checkout; removing it would destroy the custody trail it holds"
        )
    with custody_guard(worktree_path, custody_release, repo_root=repo_root) as settled:
        error = _remove_with_git(worktree_path, force=force, run_git=run_git)
        if error is None:
            if prune and run_git is not None:
                run_git(["worktree", "prune"])
            settled.removed()
            return CheckoutRemoval(removed=True, used_filesystem_fallback=False)
        if not force:
            # The checkout is still THERE. Ending its grant here would leave it
            # standing and unprotected for the next forced cleanup, which is
            # the opposite of what the release asked for (round 6 finding 2).
            return CheckoutRemoval(
                removed=False, used_filesystem_fallback=False, git_error=error
            )
        # The fallback runs INSIDE the guard. Released first, it would leave a
        # window in which an operator takes custody, is told the checkout is
        # protected, and watches this delete it anyway.
        logger.warning(
            "Forced removal via git failed; deleting directory: path=%s error=%s",
            worktree_path,
            error,
        )
        _delete_path(worktree_path)
        if prune and run_git is not None:
            run_git(["worktree", "prune"])
        gone = not worktree_path.exists()
        if gone:
            settled.removed()
        return CheckoutRemoval(
            removed=gone,
            used_filesystem_fallback=True,
            git_error=error,
        )


def _delete_path(worktree_path: Path) -> None:
    """Delete whatever is at the path, directory or not.

    A checkout is usually a directory, but a half-created one can be a file or
    a symlink, and a fallback that only handles directories leaves those behind
    for the next ``git worktree add`` to trip over.
    """
    if worktree_path.is_dir() and not worktree_path.is_symlink():
        shutil.rmtree(worktree_path, ignore_errors=True)
        return
    try:
        worktree_path.unlink()
    except FileNotFoundError:
        return


def _remove_with_git(
    worktree_path: Path, *, force: bool, run_git: GitRunner | None
) -> str | None:
    """Ask git to remove the checkout. None when it did."""
    if run_git is None:
        return "the repository for this path could not be resolved"
    argv = ["worktree", "remove"]
    if force:
        argv.append("--force")
    argv.append(str(worktree_path))
    return run_git(argv)
