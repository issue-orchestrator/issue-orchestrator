"""Git ancestry check for stacked successors (ADR-0029, #6596).

Concrete :class:`StackBranchAncestry` that decides, from real git refs, whether
a stacked successor's working copy still contains the *current* head of its
predecessor's branch. This is the authoritative staleness signal the dependency
gate report's ``contained_in_successor`` fact consumes for the publish gate:
labels can lag, but ``git merge-base --is-ancestor`` cannot.

Procedure (in the successor worktree):

1. ``git fetch origin <predecessor_branch>`` to bring the predecessor's current
   tip into ``FETCH_HEAD`` (the predecessor may have advanced since the
   successor branched from it).
2. ``git merge-base --is-ancestor FETCH_HEAD HEAD`` — exit ``0`` means the
   successor ``HEAD`` descends from (or equals) the predecessor tip, i.e. it is
   still contained; exit ``1`` means the predecessor advanced past the successor
   (stale); any other exit is a git error.

Every path is **fail-safe**: a failed fetch, a non-ancestor exit, or any other
non-zero result yields ``False`` so a possibly-stale successor is rebuilt rather
than published on an unverified base.
"""

import logging
from pathlib import Path

from ..control.isolation import build_runtime_tool_env
from ..ports.command_runner import CommandRunner

logger = logging.getLogger(__name__)


class GitStackBranchAncestry:
    """Answers successor↔predecessor ancestry via git, through a CommandRunner."""

    def __init__(self, command_runner: CommandRunner, remote: str = "origin") -> None:
        self._runner = command_runner
        self._remote = remote

    def successor_contains_predecessor(
        self, worktree: Path, predecessor_branch: str
    ) -> bool:
        if not predecessor_branch:
            return False
        try:
            fetch = self._runner.run(
                ["git", "fetch", self._remote, predecessor_branch],
                cwd=worktree,
                env=build_runtime_tool_env(worktree),
            )
            if fetch.returncode != 0:
                logger.warning(
                    "Stack ancestry: could not fetch %s/%s into %s: %s",
                    self._remote,
                    predecessor_branch,
                    worktree,
                    (fetch.stderr or fetch.stdout or "").strip()[:200],
                )
                return False
            ancestry = self._runner.run(
                ["git", "merge-base", "--is-ancestor", "FETCH_HEAD", "HEAD"],
                cwd=worktree,
            )
        except Exception as exc:  # fail-safe: never raise into the gate
            logger.warning(
                "Stack ancestry check for %s failed in %s: %s",
                predecessor_branch,
                worktree,
                exc,
            )
            return False
        # Exit 0: successor HEAD contains the predecessor tip. Exit 1: stale.
        # Any other code (e.g. 128 for a bad object) is treated as stale too.
        return ancestry.returncode == 0
