"""Who owns a checkout the orchestrator would otherwise delete (#7274).

A tech-lead failure investigation runs in a **disposable** worktree on a branch
that is never pushed. From the moment its session ends that checkout is
inactive, and its branch exists in exactly one place: on disk, in that checkout.
Four separate paths remove it -- action application, tech-lead termination,
stale cleanup, startup reconciliation -- and three of them ask for FORCED
removal, which falls back to ``shutil.rmtree`` when git refuses. ``git worktree
lock`` is metadata: ``git worktree prune`` honours it, a filesystem delete does
not.

So before this there was no way to say "a human owns this checkout now", and any
code that told an operator their commits were safe was making a promise the
system could not keep -- which is exactly what PR #7271 tried to ship and its
review took apart three different ways.

Custody is recorded OUTSIDE the checkout, in the repository's git metadata, so
it survives both a restart and the deletion it exists to prevent. Every removal
consults it at one seam; a caller cannot opt out by passing ``force``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol


class CustodyError(RuntimeError):
    """Custody could not be honoured. NEVER catch this to keep removing.

    The whole value of custody is that a removal stops. Every caller that wraps
    a removal in ``except Exception`` has to let this through, or the refusal
    becomes a log line and the checkout goes anyway -- which is how three of
    this repository's removal paths behaved before #7274.
    """


class CustodyUnavailableError(CustodyError):
    """Whether the checkout is held could not be determined.

    Fails CLOSED. An unreadable ``.git`` file or a damaged custody store is not
    "nothing is held": answering that way deletes the one copy of a branch on
    the strength of a read that did not work.
    """


class WorktreeInCustodyError(CustodyError):
    """A removal was refused because a human owns the checkout.

    Carries the grant so a caller can tell the operator who holds it and why,
    rather than reporting a generic failure.
    """

    def __init__(self, grant: "CustodyGrant") -> None:
        super().__init__(
            f"{grant.path} is in the custody of {grant.holder} "
            f"since {grant.taken_at.isoformat()}: {grant.reason}"
        )
        self.grant = grant


@dataclass(frozen=True)
class CustodyGrant:
    """One checkout held for a person, with why and since when."""

    path: Path
    branch: str | None
    holder: str
    reason: str
    taken_at: datetime

    def __post_init__(self) -> None:
        for name in ("holder", "reason"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"a custody grant requires {name}")


@dataclass(frozen=True)
class CustodyRelease:
    """The explicit intent to end a grant, as part of removing the checkout.

    A separate value rather than a boolean, and never implied by ``force``: the
    whole point of custody is that discarding someone's only copy of a branch
    has to be something a caller SAID, with a reason that outlives it in the
    audit trail.
    """

    holder: str
    reason: str

    def __post_init__(self) -> None:
        for name in ("holder", "reason"):
            if not str(getattr(self, name)).strip():
                raise ValueError(f"a custody release requires {name}")


class WorktreeCustody(Protocol):
    """The single owner of "this checkout is not ours to delete".

    Deliberately NOT injected into the removal paths. Enforcement is
    ``adapters.worktree.custody.custody_guard``, a module-level function every
    removal wraps itself in, because the property that matters is that removing
    a worktree WITHOUT consulting custody is impossible -- and a collaborator
    passed in can be not passed in. A new removal path that forgets an injected
    dependency compiles; one that forgets the guard fails
    ``tests/unit/test_worktree_custody.py``.

    This protocol is the read/write surface an owner offers a caller that wants
    to place or end a grant -- the CLI, and the launch paths that will hand a
    checkout to a person (#7263). ``GitMetadataWorktreeCustody`` satisfies it,
    and a test pins that it still does.
    """

    def take(
        self, worktree_path: Path, *, branch: str | None, holder: str, reason: str
    ) -> CustodyGrant:
        """Place a checkout in custody and return the grant.

        Taking custody of a checkout already held returns the EXISTING grant
        unchanged: the first holder keeps it, and a second caller learns who
        that is rather than silently taking it over.
        """
        ...

    def release(self, worktree_path: Path, release: CustodyRelease) -> CustodyGrant | None:
        """End a grant. Returns the grant that ended, or None if not held."""
        ...

    def held(self, worktree_path: Path) -> CustodyGrant | None:
        """The grant on this checkout, or None."""
        ...

    def list_held(self) -> tuple[CustodyGrant, ...]:
        """Every checkout in custody, oldest first."""
        ...
