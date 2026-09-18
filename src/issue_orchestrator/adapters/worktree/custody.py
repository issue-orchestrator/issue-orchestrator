"""Custody records kept in the repository's git metadata (#7274).

Stored beside the repository rather than inside the checkout, for two reasons:
the record has to outlive the deletion it exists to prevent, and an operator
asking "what is being held?" needs one place to look rather than a walk over
every worktree that might still be there.

The store is a JSON object keyed by resolved path, plus an append-only JSONL
trail of every take and release. The trail is the audit: a grant that ended
still says who ended it and why, which is the only way to answer "who threw away
my branch" after the fact.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path

from ...ports.worktree_custody import (
    CustodyGrant,
    CustodyRelease,
    WorktreeInCustodyError,
)

logger = logging.getLogger(__name__)

#: Under the git COMMON dir, so every worktree of a repository sees one store.
CUSTODY_DIR = Path("issue-orchestrator")
CUSTODY_FILE = CUSTODY_DIR / "worktree-custody.json"
CUSTODY_LOG = CUSTODY_DIR / "worktree-custody.log.jsonl"


class GitMetadataWorktreeCustody:
    """One custody store per repository, shared by all of its worktrees."""

    def __init__(self, common_dir: Path) -> None:
        self._root = Path(common_dir)

    @classmethod
    def for_path(cls, path: Path) -> "GitMetadataWorktreeCustody | None":
        """The store shared by every worktree of ``path``'s repository.

        None when ``path`` is not inside a repository -- an orphaned directory
        git no longer knows about, where nothing can be held, because taking
        custody resolves the same way.
        """
        common_dir = git_common_dir(path)
        return None if common_dir is None else cls(common_dir)

    # -- reads --------------------------------------------------------------

    def held(self, worktree_path: Path) -> CustodyGrant | None:
        return self._records().get(_key(worktree_path))

    def list_held(self) -> tuple[CustodyGrant, ...]:
        return tuple(
            sorted(self._records().values(), key=lambda grant: grant.taken_at)
        )

    # -- writes -------------------------------------------------------------

    def take(
        self, worktree_path: Path, *, branch: str | None, holder: str, reason: str
    ) -> CustodyGrant:
        records = self._records()
        key = _key(worktree_path)
        existing = records.get(key)
        if existing is not None:
            # The first holder keeps it. A second caller learns who that is
            # instead of quietly taking over a checkout someone else is using.
            return existing
        grant = CustodyGrant(
            path=Path(key),
            branch=branch,
            holder=holder,
            reason=reason,
            taken_at=datetime.now(timezone.utc),
        )
        records[key] = grant
        self._write(records)
        self._append_trail("take", grant, actor=holder, reason=reason)
        logger.info(
            "Worktree placed in custody: path=%s holder=%s reason=%s",
            grant.path,
            holder,
            reason,
        )
        return grant

    def release(
        self, worktree_path: Path, release: CustodyRelease
    ) -> CustodyGrant | None:
        records = self._records()
        grant = records.pop(_key(worktree_path), None)
        if grant is None:
            return None
        self._write(records)
        self._append_trail(
            "release", grant, actor=release.holder, reason=release.reason
        )
        logger.info(
            "Worktree custody released: path=%s holder=%s by=%s reason=%s",
            grant.path,
            grant.holder,
            release.holder,
            release.reason,
        )
        return grant

    # -- storage ------------------------------------------------------------

    def _records(self) -> dict[str, CustodyGrant]:
        path = self._root / CUSTODY_FILE
        try:
            payload = json.loads(path.read_text())
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            # Never "no custody": an unreadable store would report every held
            # checkout as free, which is the one answer that loses work.
            raise ValueError(f"the worktree custody store at {path} is unreadable: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"the worktree custody store at {path} is not an object")
        return {key: _grant_from(key, value) for key, value in payload.items()}

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


def require_no_custody(
    worktree_path: Path, release: CustodyRelease | None = None
) -> None:
    """Refuse to remove a held checkout, or record the release that lets it go.

    THE check. Every path in this repository that removes a worktree calls it
    first, and ``tests/unit/test_worktree_custody.py`` fails when a module
    starts removing worktrees without it -- because custody enforced at four
    call sites is custody a fifth one silently skips, which is the shape of the
    bug #7274 exists to close.
    """
    custody = GitMetadataWorktreeCustody.for_path(worktree_path)
    if custody is None:
        return
    grant = custody.held(worktree_path)
    if grant is None:
        return
    if release is None:
        raise WorktreeInCustodyError(grant)
    custody.release(worktree_path, release)


def git_common_dir(path: Path) -> Path | None:
    """The one metadata directory every worktree of a repository shares.

    Read off the filesystem rather than through ``git rev-parse``: this runs on
    the removal path of every checkout, and a subprocess per removal buys
    nothing over the two shapes git actually writes -- a ``.git`` DIRECTORY in
    the main checkout, and a ``.git`` FILE pointing at
    ``<common>/worktrees/<name>`` in a linked one.

    Getting this wrong would give each worktree a private store, which the
    removal paths of every other worktree would not see -- so it resolves to the
    COMMON directory in both shapes, never to a per-worktree one.
    """
    git_entry = Path(path) / ".git"
    if git_entry.is_dir():
        return git_entry
    if not git_entry.is_file():
        return None
    try:
        content = git_entry.read_text().strip()
    except OSError:
        return None
    if not content.startswith("gitdir:"):
        return None
    git_dir = Path(content.split(":", 1)[1].strip())
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
