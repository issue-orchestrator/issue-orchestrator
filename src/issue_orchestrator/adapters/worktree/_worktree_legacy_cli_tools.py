"""Owner for retiring the ``cli_tools`` drop older io versions left in worktrees.

Until #7566, worktree runtime setup copied io's own ``entrypoints/cli_tools``
package into every agent worktree and hid it from ``git status``:

- **Foreign target repo:** the copies were untracked, hidden by one exact
  ``info/exclude`` line per file in the repository's *common* git dir. The
  target's own validators still walked the tree (porchpin's architecture audit
  failed on them).
- **io's own repo:** the copies overwrote tracked source and carried
  ``skip-worktree``, so git reported a clean tree while disk differed from the
  index. Files the engine had but the branch lacked landed untracked, hidden
  by the same exclude lines.

io no longer places anything there; completion commands resolve from the
session environment. Reused worktrees outlive the code that provisioned them,
so this owner undoes the drop when a worktree is set up again.

Ownership is proved, never assumed:

- a tracked file under the drop directory carrying ``skip-worktree`` (agents
  are blocked from setting the bit), or
- an untracked file whose exact path is one of io's exclude lines.

Nothing is deleted. Bytes that are not the committed content are moved to a
quarantine directory in the repository's common git dir — outside every
worktree, so no target tooling sees them, and recoverable if an agent had
edited one. Anything else under the drop directory is left exactly where it
is.

The exclude lines are shared by every worktree of the repository. Removing
them while another worktree still holds a drop would surface that drop as
untracked mid-session and fail its completion intake, so they are removed only
once no worktree of the repository holds a drop. Until then they keep hiding
exactly the paths they always hid.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from ._worktree_git import _git_run
from ._worktree_git_exclude import (
    common_dir_of,
    exclude_lock,
    remove_exclude_lines,
    worktree_git_dir,
)

logger = logging.getLogger(__name__)

LEGACY_CLI_TOOLS_DROP_DIR = Path("src/issue_orchestrator/entrypoints/cli_tools")
_DROP_PREFIX = f"{LEGACY_CLI_TOOLS_DROP_DIR.as_posix()}/"
LEGACY_DROP_QUARANTINE_DIR = Path("issue-orchestrator") / "retired-cli-tools-drop"

__all__ = [
    "LEGACY_CLI_TOOLS_DROP_DIR",
    "LEGACY_DROP_QUARANTINE_DIR",
    "LegacyDropRetirement",
    "retire_legacy_cli_tools_drop",
]


@dataclass(frozen=True)
class LegacyDropRetirement:
    """What retiring a worktree's legacy drop actually did.

    Args:
        restored: Tracked paths whose ``skip-worktree`` bit was cleared and
            whose committed content was restored.
        quarantined: Worktree-relative paths whose bytes were moved out of the
            worktree into ``quarantine_dir``.
        quarantine_dir: Where those bytes now live, or ``None`` when nothing
            needed preserving.
        exclude_lines_removed: Whether io's legacy exclude lines were removed
            from the repository, because no worktree holds a drop any more.
    """

    restored: tuple[Path, ...] = ()
    quarantined: tuple[Path, ...] = ()
    quarantine_dir: Path | None = None
    exclude_lines_removed: bool = False


def retire_legacy_cli_tools_drop(worktree_path: Path) -> LegacyDropRetirement:
    """Undo a pre-#7566 ``cli_tools`` drop in ``worktree_path``.

    Raises:
        GitError: If git cannot report or restore the drop.
        OSError: If a file cannot be quarantined or an exclude file rewritten.
            Callers translate both; a half-retired drop must fail setup rather
            than leave the target's validators failing on io's files.
    """
    git_dir, common_dir = _git_dirs(worktree_path)
    if common_dir is None:
        return LegacyDropRetirement()
    common_exclude = common_dir / "info" / "exclude"
    has_drop_dir = _real_drop_dir(worktree_path)
    if not _legacy_exclude_lines(common_exclude) and not has_drop_dir:
        return LegacyDropRetirement()

    # One lock for the whole retirement: the ownership proof (the shared
    # exclude lines), the moves it authorises, and the decision to drop the
    # lines must not interleave with another worktree's setup.
    with exclude_lock(common_dir):
        legacy_lines = _legacy_exclude_lines(common_exclude)
        quarantine = _Quarantine(worktree_path, common_dir / LEGACY_DROP_QUARANTINE_DIR)
        restored: tuple[Path, ...] = ()
        restored_quarantine: tuple[Path, ...] = ()
        planted: tuple[Path, ...] = ()
        if has_drop_dir:
            restored, restored_quarantine = _restore_skip_worktree_files(
                worktree_path, quarantine
            )
            planted = _quarantine_planted_untracked(
                worktree_path, legacy_lines, quarantine
            )
            _prune_empty_drop_dirs(worktree_path)

        lines_removed = bool(legacy_lines) and not _any_worktree_holds_drop(
            worktree_path, legacy_lines
        )
        if lines_removed:
            for exclude in {common_exclude, git_dir / "info" / "exclude"}:
                remove_exclude_lines(exclude, _is_legacy_line)

    quarantined = (*restored_quarantine, *planted)
    result = LegacyDropRetirement(
        restored=restored,
        quarantined=quarantined,
        quarantine_dir=quarantine.directory,
        exclude_lines_removed=lines_removed,
    )
    if restored or quarantined or lines_removed:
        logger.info(
            "Retired legacy cli_tools drop: path=%s restored=%d quarantined=%d "
            "quarantine_dir=%s exclude_lines_removed=%s",
            worktree_path,
            len(restored),
            len(quarantined),
            result.quarantine_dir,
            lines_removed,
        )
    return result


def _present(path: Path) -> bool:
    """Whether anything sits at ``path``, a dangling symlink included."""
    return path.is_symlink() or path.exists()


def _real_drop_dir(checkout: Path) -> bool:
    """Whether the drop directory exists with no symlink on the way to it.

    io's drop only ever created real directories. A symlink anywhere on the
    path is the target's own layout; following it could move or prune files
    outside the worktree, so such a checkout holds no drop.
    """
    current = checkout
    for part in LEGACY_CLI_TOOLS_DROP_DIR.parts:
        current = current / part
        if current.is_symlink() or not current.is_dir():
            return False
    return True


def _git_dirs(worktree_path: Path) -> tuple[Path, Path | None]:
    """Return the checkout's git dir and the repository's common git dir.

    Read from the ``.git`` link rather than asked of git, so a checkout git
    cannot open yields "no repository" (nothing io could have planted there)
    instead of failing setup.
    """
    dot_git = worktree_path / ".git"
    if dot_git.is_dir():
        return dot_git, dot_git
    git_dir = worktree_git_dir(worktree_path)
    if git_dir is None:
        return dot_git, None
    return git_dir, common_dir_of(git_dir)


def _git_z(worktree_path: Path, argv: list[str]) -> list[str]:
    result = _git_run(
        worktree_path, [*argv, "-z", "--", LEGACY_CLI_TOOLS_DROP_DIR.as_posix()], check=True
    )
    return [entry for entry in result.stdout.split("\0") if entry]


def _legacy_exclude_lines(exclude: Path) -> frozenset[str]:
    if not exclude.exists():
        return frozenset()
    return frozenset(
        line.strip()
        for line in exclude.read_text(encoding="utf-8").splitlines()
        if _is_legacy_line(line.strip())
    )


def _is_legacy_line(line: str) -> bool:
    return line.startswith(_DROP_PREFIX)


def _skip_worktree_paths(worktree_path: Path) -> list[str]:
    """Tracked paths under the drop dir that io hid with ``skip-worktree``."""
    tagged = _git_z(worktree_path, ["ls-files", "-v"])
    return _planted_skip_worktree(worktree_path, tagged)


def _planted_skip_worktree(checkout: Path, tagged: list[str]) -> list[str]:
    """``S``-tagged entries whose file is on disk.

    Git sets the same bit on paths a sparse checkout omits, and removes those
    files from disk. io's drop always wrote the file, so a bit on a missing
    file is git's sparse bookkeeping and is left alone.
    """
    return [
        entry[2:]
        for entry in tagged
        if entry.startswith("S ") and (checkout / entry[2:]).is_file()
    ]


class _Quarantine:
    """One retirement's private directory for bytes moved out of the tree.

    Created on first use with ``mkdtemp``, so two retirements never share a
    directory — not two worktrees with the same basename, not two runs in the
    same second — and a destination is never overwritten.
    """

    def __init__(self, worktree_path: Path, root: Path) -> None:
        self._worktree_path = worktree_path
        self._root = root
        self.directory: Path | None = None

    def move(self, relative: str) -> Path:
        if self.directory is None:
            self._root.mkdir(parents=True, exist_ok=True)
            self.directory = Path(
                tempfile.mkdtemp(
                    prefix=f"{self._worktree_path.name}-{time.strftime('%Y%m%dT%H%M%S')}-",
                    dir=self._root,
                )
            )
        destination = self.directory / relative
        if destination.exists():
            raise FileExistsError(f"quarantine destination already exists: {destination}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(self._worktree_path / relative, destination)
        return Path(relative)


def _restore_skip_worktree_files(
    worktree_path: Path, quarantine: _Quarantine
) -> tuple[tuple[Path, ...], tuple[Path, ...]]:
    """Clear io's ``skip-worktree`` bits; restore the committed bytes.

    The bit proves io hid the file, not who wrote its current bytes, so any
    bytes that differ from the index are quarantined first rather than lost.
    """
    skipped = _skip_worktree_paths(worktree_path)
    if not skipped:
        return (), ()
    differing = [
        relative
        for relative in skipped
        if _blob_of_index(worktree_path, relative)
        != _blob_of_file(worktree_path, relative)
    ]
    quarantined = tuple(quarantine.move(relative) for relative in differing)
    _git_run(worktree_path, ["update-index", "--no-skip-worktree", "--", *skipped], check=True)
    _git_run(worktree_path, ["checkout", "--", *skipped], check=True)
    return tuple(Path(relative) for relative in skipped), quarantined


def _blob_of_index(worktree_path: Path, relative: str) -> str:
    staged = _git_run(worktree_path, ["ls-files", "-s", "--", relative], check=True)
    return staged.stdout.split()[1]


def _blob_of_file(worktree_path: Path, relative: str) -> str:
    return _git_run(worktree_path, ["hash-object", "--", relative], check=True).stdout.strip()


def _quarantine_planted_untracked(
    worktree_path: Path, legacy_lines: frozenset[str], quarantine: _Quarantine
) -> tuple[Path, ...]:
    """Move out untracked files io's exact exclude lines name."""
    if not legacy_lines:
        return ()
    untracked = _git_z(worktree_path, ["ls-files", "--others"])
    return tuple(
        quarantine.move(relative)
        for relative in untracked
        if relative in legacy_lines
    )


def _prune_empty_drop_dirs(worktree_path: Path) -> None:
    """Remove directories the drop left empty, deepest first, up to the root.

    Only called for a real drop directory (see ``_real_drop_dir``).
    """
    drop_dir = worktree_path / LEGACY_CLI_TOOLS_DROP_DIR
    for directory in sorted(
        (path for path in drop_dir.rglob("*") if path.is_dir() and not path.is_symlink()),
        key=lambda path: len(path.parts),
        reverse=True,
    ):
        if not any(directory.iterdir()):
            directory.rmdir()
    current = drop_dir
    while current != worktree_path and current.is_dir() and not any(current.iterdir()):
        current.rmdir()
        current = current.parent


def _any_worktree_holds_drop(worktree_path: Path, legacy_lines: frozenset[str]) -> bool:
    """Whether any checkout of the repository still holds a legacy drop."""
    listing = _git_run(worktree_path, ["worktree", "list", "--porcelain", "-z"], check=True)
    checkouts = [
        Path(field.removeprefix("worktree "))
        for field in listing.stdout.split("\0")
        if field.startswith("worktree ")
    ]
    return any(_holds_drop(checkout, legacy_lines) for checkout in checkouts)


def _holds_drop(checkout: Path, legacy_lines: frozenset[str]) -> bool:
    """Whether ``checkout`` still holds files io's exclude lines must hide.

    A checkout git cannot answer for counts as holding one: keeping the lines
    one more round is harmless, removing them under a live drop is not.
    """
    if not _real_drop_dir(checkout):
        return False
    listing = _git_run(
        checkout,
        ["ls-files", "-v", "-z", "--", LEGACY_CLI_TOOLS_DROP_DIR.as_posix()],
        check=False,
    )
    if listing.returncode != 0:
        logger.warning(
            "Keeping legacy cli_tools exclude lines: cannot inspect %s: %s",
            checkout,
            listing.stderr.strip(),
        )
        return True
    tagged = [entry for entry in listing.stdout.split("\0") if entry]
    if _planted_skip_worktree(checkout, tagged):
        return True
    tracked = {entry[2:] for entry in tagged}
    return any(
        _present(checkout / line) and line not in tracked for line in legacy_lines
    )
