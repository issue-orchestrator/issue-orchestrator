"""Port: stack successor↔predecessor branch ancestry (ADR-0029, #6596).

A stacked successor is only publishable / merge-ready while its branch still
contains the *current* head of its predecessor's branch. When the predecessor
advances — a force-push, a reset for rework, a rebase, or any new commit — a
successor that was branched from an older head no longer descends from it and is
**stale**: publishing or merging it would base on or carry a head the
predecessor has moved past.

This port answers that single ancestry question for the dependency gate report's
``contained_in_successor`` fact. The concrete git implementation lives in
``execution/`` and is wired at the composition root; the control layer (the
dependency evaluator) depends only on this Protocol.

Implementations must be **fail-safe**: when ancestry cannot be established (the
predecessor branch cannot be fetched, a transient git error, an unknown ref),
return ``False`` so a possibly-stale successor is rebuilt rather than published
or merged on an unverified base.
"""

from pathlib import Path
from typing import Protocol, runtime_checkable


@runtime_checkable
class StackBranchAncestry(Protocol):
    """Decides whether a successor working copy contains a predecessor's head."""

    def successor_contains_predecessor(
        self, worktree: Path, predecessor_branch: str
    ) -> bool:
        """Whether the successor at *worktree* contains *predecessor_branch*'s head.

        Returns ``True`` only when the successor's ``HEAD`` is a descendant of (or
        equal to) the current tip of ``predecessor_branch``. Any inability to
        determine ancestry must return ``False`` (fail-safe), never raise.
        """
        ...
