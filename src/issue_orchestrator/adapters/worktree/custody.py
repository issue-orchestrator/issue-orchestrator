"""Custody records kept in the repository's git metadata (#7274).

Stored beside the repository rather than inside the checkout, for two reasons:
the record has to outlive the deletion it exists to prevent, and an operator
asking "what is being held?" needs one place to look rather than a walk over
every worktree that might still be there.

**What custody can and cannot promise.** Every removal the orchestrator makes
goes through :func:`..removal.remove_checkout_path`, and that one asks. Nothing
can stop a process outside this repository -- or a person with ``rm -rf`` --
from deleting a directory, and a guardrail that tried to find every
``shutil.rmtree`` in every script would be a search with no end. So custody
PREVENTS inside the owner and DETECTS everywhere else:
:meth:`GitMetadataWorktreeCustody.breached` reports grants whose checkout is no
longer there, and the operator surface prints them. A promise that stops at the
edge of this codebase is worth making; one that pretends to go further is not.

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
import stat
import threading
from collections.abc import Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from ...ports.worktree_custody import (
    CustodyGrant,
    CustodyRelease,
    CustodyUnavailableError,
    WorktreeInCustodyError,
)

logger = logging.getLogger(__name__)

#: Lock depth per store, so a guard can nest inside a guard.
#:
#: ``flock`` is per open file DESCRIPTION: a second ``open`` of the same file in
#: this process gets a new one, and locking it while the first is held is a
#: self-deadlock. Removal paths legitimately nest -- a policy holds one around
#: its git attempt AND its filesystem fallback, and the seam it calls in
#: between holds its own -- so the count is kept here and the file lock is
#: taken only on the way in from zero.
_DEPTH_GUARD = threading.Lock()
_DEPTH: dict[tuple[int, str], int] = {}

#: Under the git COMMON dir, so every worktree of a repository sees one store.
CUSTODY_DIR = Path("issue-orchestrator")
CUSTODY_FILE = CUSTODY_DIR / "worktree-custody.json"
CUSTODY_LOG = CUSTODY_DIR / "worktree-custody.log.jsonl"
CUSTODY_LOCK = CUSTODY_DIR / "worktree-custody.lock"

#: Every action the trail may record. A row outside this set is damage: the
#: trail is what accounts for grants when the state file is gone, so a row it
#: cannot read is a grant it cannot account for.
_TRAIL_ACTIONS = frozenset({"take", "release", "release-intent"})

#: Every trail row says who did it and why. A row missing either is refused
#: rather than obeyed: see ``_readable_trail_row``.
_TRAIL_ATTRIBUTION = ("holder", "actor", "reason", "at")


class GitMetadataWorktreeCustody:
    """One custody store per repository, shared by all of its worktrees."""

    def __init__(self, common_dir: Path) -> None:
        self._root = Path(common_dir)

    @classmethod
    def for_path(
        cls, path: Path, repo_root: Path | None = None
    ) -> "GitMetadataWorktreeCustody | None":
        """The store shared by every worktree of ``path``'s repository.

        ``repo_root`` is the fallback when the CHECKOUT can no longer say which
        repository it belongs to -- a held worktree whose ``.git`` file has
        been deleted still has its grant in the repository's store, and
        answering "not in a repository, so not held" there would delete it
        (round 3 finding 2).

        A supplied ``repo_root`` is AUTHORITATIVE, including when it turns out
        not to name a repository at all. Falling back to the checkout there
        reproduced the fail-open the ``UNKNOWN_REPOSITORY`` sentinel used to be:
        pass a path that is not a repository, and a held checkout whose ``.git``
        file is gone answers "no store, so nothing is held" and is deleted
        (round 9 finding 1).

        None only when the caller named no repository AND the checkout cannot
        say either.
        """
        if repo_root is not None:
            named = git_common_dir(repo_root)
            if named is None:
                raise CustodyUnavailableError(
                    f"{repo_root} is not a git repository, so whether {path} "
                    "is held cannot be determined"
                )
            # AUTHORITATIVE. The checkout's own pointer is a claim it makes
            # about itself, and a corrupted one still starting with "gitdir:"
            # would send this to an empty store somewhere else -- where
            # nothing is held, and the removal proceeds (round 5 finding 2).
            from_path = git_common_dir(path)
            if from_path is not None and from_path != named:
                raise CustodyUnavailableError(
                    f"{path} points at {from_path} but its caller says "
                    f"{named}; which store holds it is unknown"
                )
            return cls(named)
        common_dir = git_common_dir(path)
        return None if common_dir is None else cls(common_dir)

    # -- reads --------------------------------------------------------------

    def held(self, worktree_path: Path) -> CustodyGrant | None:
        with self._locked():
            return self._records().get(_key(worktree_path))

    def breached(self) -> tuple[CustodyGrant, ...]:
        """Grants whose checkout is gone: something removed it anyway.

        Detection, not prevention. The orchestrator's own removals are refused,
        but nothing stops a hand or a script outside this codebase, and an
        operator who was told their branch was protected deserves to find out
        that it is not -- rather than discovering it when they go looking.
        """
        return tuple(
            grant for grant in self.list_held() if not grant.path.exists()
        )

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
            if existing is None and git_common_dir(worktree_path) is None:
                # A grant on something that is not a checkout of any repository
                # protects nothing and would later be reported as a breach the
                # moment it is tidied away. Re-holding one that IS already held
                # stays possible: that is the checkout which lost its `.git`.
                raise CustodyUnavailableError(
                    f"{worktree_path} is not a checkout of any repository, so "
                    "holding it would protect nothing"
                )
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
    ) -> Iterator["CustodySettlement"]:
        """Refuse a held checkout, and hold that answer while the caller removes.

        The lock spans the caller's body on purpose: checking and then removing
        leaves a window in which an operator takes custody, is told no removal
        path will discard the checkout, and watches it go anyway.

        The grant is dropped only when the caller calls
        :meth:`CustodySettlement.removed`. Neither "the body did not raise" nor
        "the body returned" is proof: a removal that reports failure leaves the
        checkout THERE, and dropping its grant would leave it standing and
        unprotected for the next forced cleanup (round 6 finding 2).

        The release INTENT is recorded before the body, so a process that dies
        mid-removal leaves an audited hand-off rather than a breach nobody
        asked for (round 6 finding 4).

        Any grant that OVERLAPS the target refuses unconditionally, release or
        not. The removal owner's forced fallback is a recursive delete, so
        removing a parent removes every held checkout beneath it. The reverse
        matters too: a held checkout whose ``.git`` file is gone is mistaken for
        a session container, and its child directories are handed to the removal
        owner one at a time. A release naming either path is not consent to
        discard the other (rounds 8 and 11, finding 1).
        """
        with self._locked():
            records = self._records()
            overlapping = _other_grants_overlapping(records, worktree_path)
            if overlapping:
                raise WorktreeInCustodyError(overlapping[0])
            grant = records.get(_key(worktree_path))
            if grant is None:
                yield CustodySettlement(lambda: None)
                return
            if release is None:
                raise WorktreeInCustodyError(grant)
            self._append_trail(
                "release-intent", grant, actor=release.holder, reason=release.reason
            )
            yield CustodySettlement(
                lambda: self._release_locked(worktree_path, release)
            )

    # -- storage ------------------------------------------------------------

    @contextmanager
    def _locked(self) -> Iterator[None]:
        """Serialize read-modify-write against every process on this host.

        A lock FILE rather than an in-process one: the orchestrator, a CLI
        invocation and a recovery script are separate processes reading the
        same store, and losing a grant to a lost update is losing a branch.

        Re-entrant within a thread, because removal paths nest.
        """
        _require_real_directory(
            self._root / CUSTODY_DIR,
            description="worktree custody directory",
            create=True,
        )
        path = self._root / CUSTODY_LOCK
        _require_regular_or_absent(path, description="worktree custody lock")
        # Keyed by the CANONICAL path. Two stores addressing the same file
        # through a relative and an absolute common directory would otherwise
        # get different depths, nest, and flock the same file twice -- the
        # self-deadlock the re-entrancy exists to prevent (round 3 finding 5).
        key = (threading.get_ident(), str(_canonical_lock(path)))
        with _DEPTH_GUARD:
            depth = _DEPTH.get(key, 0)
            _DEPTH[key] = depth + 1
        if depth:
            try:
                yield
            finally:
                _release_depth(key)
            return
        try:
            handle = path.open("a+")
        except OSError as exc:
            _release_depth(key)
            raise CustodyUnavailableError(
                f"cannot open the worktree custody lock at {path}: {exc}"
            ) from exc
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            handle.close()
            _release_depth(key)

    def _records(self) -> dict[str, CustodyGrant]:
        path = self._root / CUSTODY_FILE
        raw = _read_regular_text(path, description="worktree custody store")
        if raw is None:
            outstanding = self._outstanding_in_trail()
            if outstanding:
                # The trail says grants were taken and not released, so the
                # state file did not go missing because nothing was held. It
                # went missing (round 4 finding 2).
                raise CustodyUnavailableError(
                    f"the worktree custody store at {path} is gone while its "
                    f"trail still holds {len(outstanding)} grant(s): "
                    f"{sorted(outstanding)}"
                )
            return {}
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise CustodyUnavailableError(
                f"the worktree custody store at {path} is unreadable: {exc}"
            ) from exc
        if not isinstance(payload, dict):
            raise CustodyUnavailableError(
                f"the worktree custody store at {path} is not an object"
            )
        try:
            records = {key: _grant_from(key, value) for key, value in payload.items()}
        except (TypeError, ValueError) as exc:
            raise CustodyUnavailableError(
                f"the worktree custody store at {path} has an unreadable record: {exc}"
            ) from exc
        # A parseable file is not automatically a TRUTHFUL one. `{}` is valid
        # JSON, so a hostile or truncated write could contradict the
        # append-only audit and report an outstanding grant as released
        # (round 14 finding 1).
        missing = self._outstanding_in_trail().difference(records)
        if missing:
            raise CustodyUnavailableError(
                f"the worktree custody store at {path} omits grant(s) that its "
                f"audit trail still holds: {sorted(missing)}"
            )
        return records

    def _outstanding_in_trail(self) -> set[str]:
        """Paths the trail took and never released.

        The trail is append-only and fsynced, so it is the one record that
        survives the state file: if it says something is held and the state
        file is absent, the store is damaged, not empty.
        """
        path = self._root / CUSTODY_LOG
        raw = _read_regular_text(path, description="worktree custody trail")
        if raw is None:
            return set()
        held: set[str] = set()
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError as exc:
                # Skipping it could turn a recorded take into an empty store,
                # which is the failure this whole file exists to avoid.
                raise CustodyUnavailableError(
                    f"the worktree custody trail at {path} has an unreadable "
                    f"row: {exc}"
                ) from exc
            action, target = _readable_trail_row(entry, path)
            if action == "take":
                held.add(target)
            elif action == "release":
                held.discard(target)
        return held

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
        with scratch.open("w") as handle:
            handle.write(json.dumps(payload, indent=2, sort_keys=True) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        scratch.replace(path)
        # The directory entry too: without it a power loss can leave the OLD
        # state file visible beside a trail that already recorded the take,
        # and a state file that parses is trusted over the trail (round 5
        # finding 3).
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)

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


@dataclass(frozen=True)
class CustodySettlement:
    """How a guarded removal says it actually happened.

    Handed to the caller rather than inferred, because the guard cannot see the
    difference between a removal that worked and one that reported failure --
    and only the first should end a grant.
    """

    _drop: "Callable[[], object]"

    def removed(self) -> None:
        """The checkout is gone; end the grant that was released for it."""
        self._drop()


@contextmanager
def custody_guard(
    worktree_path: Path,
    release: CustodyRelease | None = None,
    *,
    repo_root: Path | None = None,
) -> Iterator[CustodySettlement]:
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
    custody = GitMetadataWorktreeCustody.for_path(worktree_path, repo_root)
    if custody is None:
        yield CustodySettlement(lambda: None)
        return
    with custody.guard(worktree_path, release) as settlement:
        yield settlement


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
    try:
        entry_stat = git_entry.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CustodyUnavailableError(
            f"cannot inspect {git_entry}, so whether {path} is held is unknown: {exc}"
        ) from exc
    if stat.S_ISLNK(entry_stat.st_mode):
        # A readable redirect is not the repository saying where it lives; it
        # is somebody else saying so (round 14 finding 1).
        raise CustodyUnavailableError(
            f"{git_entry} is a symlink, so which repository owns {path} is unknown"
        )
    if stat.S_ISDIR(entry_stat.st_mode):
        # Absolute, like the linked-worktree branch below. A main checkout
        # addressed by a RELATIVE path used to answer with a relative
        # directory, so the same repository compared unequal to itself and
        # `for_path` refused a real grant (round 9 finding 1).
        return _validated_git_common_dir(git_entry, claimed_by=path)
    if not stat.S_ISREG(entry_stat.st_mode):
        raise CustodyUnavailableError(
            f"{git_entry} is not a regular git metadata entry, so whether "
            f"{path} is held is unknown"
        )
    content = _read_regular_text(
        git_entry, description=f"git metadata pointer for {path}"
    )
    if content is None:
        raise CustodyUnavailableError(
            f"{git_entry} vanished while resolving custody for {path}"
        )
    content = content.strip()
    if not content.startswith("gitdir:"):
        raise CustodyUnavailableError(
            f"{git_entry} is not a gitdir pointer, so whether {path} is held "
            "is unknown"
        )
    target = Path(content.split(":", 1)[1].strip())
    # Relative to the .git FILE, which is what git means by it -- not to
    # whatever directory this process happens to be running in.
    git_dir = target if target.is_absolute() else (git_entry.parent / target)
    _require_real_directory(
        git_dir, description=f"git directory named by {git_entry}", create=False
    )
    git_dir = git_dir.resolve()
    common_dir = (
        git_dir.parent.parent if git_dir.parent.name == "worktrees" else git_dir
    )
    return _validated_git_common_dir(common_dir, claimed_by=path)


def _is_text(value: object) -> bool:
    """A present, non-blank string -- the only shape a trail field may take."""
    return isinstance(value, str) and bool(value.strip())


def _readable_trail_row(entry: object, path: Path) -> tuple[str, str]:
    """The action and path of one trail row, or refuse the whole trail.

    A row this build cannot read is a grant it cannot account for, and
    accounting for none is how a held checkout gets deleted (round 6 finding 3).

    Attribution is part of readable. Every row this build WRITES carries it:
    ``CustodyGrant`` refuses an empty holder or reason, and ``_append_trail``
    stamps the actor. A ``release`` row without them was not written by a
    release anyone performed, so obeying it would clear a real grant on the
    strength of a line nobody can be held to -- neither fail-closed nor
    auditable (round 7 finding 3).
    """
    if not isinstance(entry, dict):
        raise CustodyUnavailableError(
            f"the worktree custody trail at {path} has a row that is not an object"
        )
    action, target = entry.get("action"), entry.get("path")
    if action not in _TRAIL_ACTIONS or not _is_text(target):
        raise CustodyUnavailableError(
            f"the worktree custody trail at {path} has a row this build "
            f"cannot read: action={action!r} path={target!r}"
        )
    missing = [name for name in _TRAIL_ATTRIBUTION if not _is_text(entry.get(name))]
    if missing:
        raise CustodyUnavailableError(
            f"the worktree custody trail at {path} has a {action} row for "
            f"{target} with no {', '.join(missing)}"
        )
    return str(action), str(target)


def _canonical_lock(path: Path) -> Path:
    """One spelling per lock file, even before it exists."""
    return path.parent.resolve() / path.name


def _require_real_directory(path: Path, *, description: str, create: bool) -> None:
    """Require a REAL directory, never a readable redirect to one."""
    if create:
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise CustodyUnavailableError(
                f"cannot create {description} at {path}: {exc}"
            ) from exc
    try:
        path_stat = path.lstat()
    except OSError as exc:
        raise CustodyUnavailableError(
            f"cannot inspect {description} at {path}: {exc}"
        ) from exc
    if not stat.S_ISDIR(path_stat.st_mode):
        raise CustodyUnavailableError(
            f"{description} at {path} is not a real directory"
        )


def _require_regular_or_absent(path: Path, *, description: str) -> None:
    """Reject symlinks and special files.

    NOT hard links. Round 14 refused ``st_nlink != 1`` on the reasoning that two
    processes would flock different inodes -- which is backwards: every hard link
    names the SAME inode, so they all flock the same file. The check bought no
    integrity and broke legitimate hard-link snapshots (round 15 finding 3).
    """
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return
    except OSError as exc:
        raise CustodyUnavailableError(
            f"cannot inspect {description} at {path}: {exc}"
        ) from exc
    if not stat.S_ISREG(path_stat.st_mode):
        raise CustodyUnavailableError(
            f"{description} at {path} is not a private regular file"
        )


def _read_regular_text(path: Path, *, description: str) -> str | None:
    """Read one custody file without treating INDIRECTION as absence.

    ``None`` only when nothing is there at all. Anything present that is not a
    regular file raises: a readable symlink pointed at an empty store used to
    read as "nothing is held", which deletes the checkout it was protecting.
    Several hard-link names still refer to this one regular-file inode.
    """
    try:
        path_stat = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise CustodyUnavailableError(
            f"cannot inspect {description} at {path}: {exc}"
        ) from exc
    if not stat.S_ISREG(path_stat.st_mode):
        raise CustodyUnavailableError(
            f"{description} at {path} cannot be read although something is there"
        )
    try:
        return path.read_text()
    except (OSError, UnicodeError) as exc:
        raise CustodyUnavailableError(
            f"{description} at {path} cannot be read although something is "
            f"there: {exc}"
        ) from exc


def _validated_git_common_dir(path: Path, *, claimed_by: Path) -> Path:
    """Reject a directory without git's mandatory HEAD metadata.

    Swapping `.git` for an empty directory produced a NEW, empty custody store
    -- nothing held, remove away. HEAD is what a valid repository must have; the
    object database may legitimately be relocated with ``GIT_OBJECT_DIRECTORY``,
    so requiring the default ``objects`` path refused real repositories (round
    15 finding 3).
    """
    common_dir = path.resolve()
    _require_real_directory(
        common_dir,
        description=f"git common directory claimed by {claimed_by}",
        create=False,
    )
    head = common_dir / "HEAD"
    try:
        head_stat = head.lstat()
    except OSError as exc:
        raise CustodyUnavailableError(
            f"{common_dir} is not readable git metadata for {claimed_by}: {exc}"
        ) from exc
    if not stat.S_ISREG(head_stat.st_mode):
        raise CustodyUnavailableError(
            f"{common_dir} is not git metadata for {claimed_by}: "
            "HEAD has the wrong type"
        )
    return common_dir


def _release_depth(key: tuple[int, str]) -> None:
    with _DEPTH_GUARD:
        remaining = _DEPTH[key] - 1
        if remaining:
            _DEPTH[key] = remaining
        else:
            del _DEPTH[key]


def _key(worktree_path: Path) -> str:
    """One spelling per checkout, so a relative path cannot hide a grant."""
    return str(Path(worktree_path).resolve())


def _other_grants_overlapping(
    records: "dict[str, CustodyGrant]", worktree_path: Path
) -> tuple[CustodyGrant, ...]:
    """Held checkouts above or beneath a removal target.

    The DESCENDANT case is the per-session layout
    ``<worktree_base>/<session>/<checkout>``: the sweep hands the session
    directory to the removal owner, no grant names it, git declines to remove
    something that is not a worktree, and the forced fallback deletes the whole
    subtree (round 8 finding 1).

    The ANCESTOR case is its mirror: a held checkout whose ``.git`` file is gone
    looks like a session container, so the sweep hands each of ITS children to
    the owner instead. Nothing names those, and an investigation's uncommitted
    work is deleted while the checkout root and its grant survive, looking
    untouched (round 11 finding 1).

    Canonical containment alone is not enough when the TARGET is itself a
    symlink. Resolving ``<held>/evidence-link`` moves the target outside the
    held checkout and erases the lexical ancestor the filesystem fallback is
    about to modify, so each lexical ancestor is resolved separately and
    crossing a held root stays visible (round 12 finding 2).

    Either way a grant is discarded without being named. Exact equality is left
    to :meth:`guard`, where an explicit release may legitimately settle it -- but
    a final-component symlink is NOT exact equality: unlinking it removes the
    alias, not the checkout the grant is about.
    """
    target = Path(_key(worktree_path))
    lexical = Path(os.path.abspath(worktree_path))
    resolved_ancestors = {
        candidate.resolve() for candidate in (lexical, *lexical.parents)
    }
    target_is_symlink = Path(worktree_path).is_symlink()
    overlapping = [
        grant
        for key, grant in records.items()
        if not (Path(key) == target and not target_is_symlink)
        and (
            target in Path(key).parents
            or Path(key) in target.parents
            or Path(key) in resolved_ancestors
        )
    ]
    return tuple(sorted(overlapping, key=lambda grant: grant.taken_at))


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
