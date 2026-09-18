"""Custody records kept in the repository's git metadata (#7274).

Stored beside the repository rather than inside the checkout, for two reasons:
the record has to outlive the deletion it exists to prevent, and an operator
asking "what is being held?" needs one place to look rather than a walk over
every worktree that might still be there.

Three properties this module is built around, each because losing them loses
somebody's only copy of a branch:

* **It fails closed.** A ``.git`` file it cannot read, or a custody store it
  cannot parse, raises. "Nothing is held" is never an answer derived from a
  read that did not work.
* **The check and the removal are one.** :func:`custody_guard` holds an
  exclusive lock for the whole removal, so an operator cannot take custody in
  the window between a remover looking and deleting -- and be told, truthfully
  at the time, that no removal path will discard it.
* **The audit survives the state.** A take writes the grant before its trail
  entry and a release writes the trail before dropping the grant, so a crash in
  the middle always lands on the side where the checkout is still protected.
"""

from __future__ import annotations

import fcntl
import json
import logging
import os
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Iterator

from ...ports.worktree_custody import (
    CustodyGrant,
    CustodyRelease,
    CustodyUnavailableError,
    WorktreeInCustodyError,
)

logger = logging.getLogger(__name__)

#: Under the git COMMON dir, so every worktree of a repository sees one store.
CUSTODY_DIR = Path("issue-orchestrator")
CUSTODY_FILE = CUSTODY_DIR / "worktree-custody.json"
CUSTODY_LOG = CUSTODY_DIR / "worktree-custody.log.jsonl"
CUSTODY_LOCK = CUSTODY_DIR / "worktree-custody.lock"


class GitMetadataWorktreeCustody:
    """One custody store per repository, shared by all of its worktrees."""

    def __init__(self, common_dir: Path) -> None:
        self._root = Path(common_dir)

    @classmethod
    def for_path(cls, path: Path) -> "GitMetadataWorktreeCustody | None":
        """The store shared by every worktree of ``path``'s repository.

        None ONLY when ``path`` is not in a repository at all -- there is
        nothing to hold there, because taking custody resolves the same way. A
        repository whose metadata cannot be read raises instead.
        """
        common_dir = git_common_dir(path)
        return None if common_dir is None else cls(common_dir)

    # -- reads --------------------------------------------------------------

    def held(self, worktree_path: Path) -> CustodyGrant | None:
        with self._locked():
            return self._records().get(_key(worktree_path))

    def list_held(self) -> tuple[CustodyGrant, ...]:
        with self._locked():
            return tuple(
                sorted(self._records().values(), key=lambda grant: grant.taken_at)
            )

    # -- writes -------------------------------------------------------------

    def take(
        self, worktree_path: Path, *, branch: str | None, holder: str, reason: str
    ) -> CustodyGrant:
        with self._locked():
            # Checked INSIDE the lock, which is the whole point: outside it, a
            # take that looked while a removal was already underway would
            # record a grant on a path deleted before it wrote. A grant on
            # nothing tells an operator their branch is protected when there is
            # nothing left to protect.
            if not Path(worktree_path).exists():
                raise CustodyUnavailableError(
                    f"{worktree_path} does not exist, so it cannot be held"
                )
            records = self._records()
            key = _key(worktree_path)
            existing = records.get(key)
            if existing is not None:
                # The first holder keeps it. A second caller learns who that is
                # instead of quietly taking over a checkout someone else is
                # using.
                return existing
            grant = CustodyGrant(
                path=Path(key),
                branch=branch,
                holder=holder,
                reason=reason,
                taken_at=datetime.now(timezone.utc),
            )
            records[key] = grant
            # State first, then the trail: a crash between them leaves the
            # checkout PROTECTED but unaudited, which is the survivable half.
            self._write(records)
            self._append_trail("take", grant, actor=holder, reason=reason)
        logger.info(
            "Worktree placed in custody: path=%s holder=%s reason=%s",
            worktree_path,
            holder,
            reason,
        )
        return grant

    def release(
        self, worktree_path: Path, release: CustodyRelease
    ) -> CustodyGrant | None:
        with self._locked():
            grant = self._release_locked(worktree_path, release)
        if grant is not None:
            logger.info(
                "Worktree custody released: path=%s holder=%s by=%s reason=%s",
                grant.path,
                grant.holder,
                release.holder,
                release.reason,
            )
        return grant

    def _release_locked(
        self, worktree_path: Path, release: CustodyRelease
    ) -> CustodyGrant | None:
        records = self._records()
        grant = records.get(_key(worktree_path))
        if grant is None:
            return None
        # The trail FIRST, then the state: a crash between them leaves the
        # checkout protected and the attempt on record, rather than
        # unprotected with nothing saying who let it go.
        self._append_trail("release", grant, actor=release.holder, reason=release.reason)
        del records[_key(worktree_path)]
        self._write(records)
        return grant

    @contextmanager
    def guard(
        self, worktree_path: Path, release: CustodyRelease | None = None
    ) -> Iterator[None]:
        """Refuse a held checkout, and hold that answer while the caller removes.

        The lock spans the caller's body on purpose: checking and then removing
        leaves a window in which an operator takes custody, is told no removal
        path will discard the checkout, and watches it go anyway.
        """
        with self._locked():
            grant = self._records().get(_key(worktree_path))
            if grant is not None:
                if release is None:
                    raise WorktreeInCustodyError(grant)
                self._release_locked(worktree_path, release)
            yield

    # -- storage ------------------------------------------------------------

    @contextmanager
    def _locked(self) -> Iterator[IO[str]]:
        """Serialize read-modify-write against every process on this host.

        A lock FILE rather than an in-process one: the orchestrator, a CLI
        invocation and a recovery script are separate processes reading the
        same store, and losing a grant to a lost update is losing a branch.
        """
        path = self._root / CUSTODY_LOCK
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            handle = path.open("a+")
        except OSError as exc:
            raise CustodyUnavailableError(
                f"cannot open the worktree custody lock at {path}: {exc}"
            ) from exc
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield handle
        finally:
            handle.close()

    def _records(self) -> dict[str, CustodyGrant]:
        path = self._root / CUSTODY_FILE
        try:
            payload = json.loads(path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            raise CustodyUnavailableError(
                f"the worktree custody store at {path} is unreadable: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise CustodyUnavailableError(
                f"the worktree custody store at {path} is not an object"
            )
        try:
            return {key: _grant_from(key, value) for key, value in payload.items()}
        except (TypeError, ValueError) as exc:
            raise CustodyUnavailableError(
                f"the worktree custody store at {path} has an unreadable record: {exc}"
            ) from exc

    def _write(self, records: dict[str, CustodyGrant]) -> None:
        path = self._root / CUSTODY_FILE
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            key: {
                "branch": grant.branch,
                "holder": grant.holder,
                "reason": grant.reason,
                "taken_at": grant.taken_at.isoformat(),
            }
            for key, grant in records.items()
        }
        # Written whole and renamed into place: a torn custody file read back
        # as "nothing is held" is the failure this store exists to prevent.
        scratch = path.with_suffix(f".{os.getpid()}.tmp")
        scratch.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")
        scratch.replace(path)

    def _append_trail(
        self, action: str, grant: CustodyGrant, *, actor: str, reason: str
    ) -> None:
        path = self._root / CUSTODY_LOG
        path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "action": action,
            "path": str(grant.path),
            "branch": grant.branch,
            "holder": grant.holder,
            "actor": actor,
            "reason": reason,
            "at": datetime.now(timezone.utc).isoformat(),
        }
        with path.open("a") as trail:
            trail.write(json.dumps(entry, sort_keys=True) + "\n")
            trail.flush()
            os.fsync(trail.fileno())


@contextmanager
def custody_guard(
    worktree_path: Path, release: CustodyRelease | None = None
) -> Iterator[None]:
    """Refuse to remove a held checkout, and hold that answer while you remove.

    THE check. Every path in this repository that removes a worktree wraps the
    removal in it, and ``tests/unit/test_worktree_custody.py`` fails when a
    function starts removing worktrees without one -- because custody enforced
    at four call sites is custody a fifth one skips, which is the shape of the
    bug #7274 exists to close.

    A free function rather than an injected collaborator for the same reason: a
    removal path that forgets a dependency still compiles, and this one has to
    be impossible to forget.
    """
    custody = GitMetadataWorktreeCustody.for_path(worktree_path)
    if custody is None:
        yield
        return
    with custody.guard(worktree_path, release):
        yield


def git_common_dir(path: Path) -> Path | None:
    """The one metadata directory every worktree of a repository shares.

    Read off the filesystem rather than through ``git rev-parse``: this runs on
    the removal path of every checkout, and a subprocess per removal buys
    nothing over the two shapes git actually writes -- a ``.git`` DIRECTORY in
    the main checkout, and a ``.git`` FILE pointing at
    ``<common>/worktrees/<name>`` in a linked one.

    None means "not in a repository". A ``.git`` that exists and cannot be read
    RAISES: reporting no metadata there would report no custody, and delete a
    checkout on the strength of a read that failed.
    """
    git_entry = Path(path) / ".git"
    if git_entry.is_dir():
        return git_entry
    if not git_entry.is_file():
        return None
    try:
        content = git_entry.read_text().strip()
    except OSError as exc:
        raise CustodyUnavailableError(
            f"cannot read {git_entry}, so whether {path} is held is unknown: {exc}"
        ) from exc
    if not content.startswith("gitdir:"):
        raise CustodyUnavailableError(
            f"{git_entry} is not a gitdir pointer, so whether {path} is held "
            "is unknown"
        )
    target = Path(content.split(":", 1)[1].strip())
    # Relative to the .git FILE, which is what git means by it -- not to
    # whatever directory this process happens to be running in.
    git_dir = target if target.is_absolute() else (git_entry.parent / target)
    git_dir = git_dir.resolve()
    if git_dir.parent.name == "worktrees":
        return git_dir.parent.parent
    return git_dir


def _key(worktree_path: Path) -> str:
    """One spelling per checkout, so a relative path cannot hide a grant."""
    return str(Path(worktree_path).resolve())


def _grant_from(key: str, value: object) -> CustodyGrant:
    if not isinstance(value, dict):
        raise ValueError(f"custody record for {key} is not an object")
    branch = value.get("branch")
    return CustodyGrant(
        path=Path(key),
        branch=branch if isinstance(branch, str) else None,
        holder=str(value.get("holder") or ""),
        reason=str(value.get("reason") or ""),
        taken_at=datetime.fromisoformat(str(value.get("taken_at"))),
    )
