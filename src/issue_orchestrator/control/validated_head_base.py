"""The base a validated head must be ahead of to be work (#7347).

Termination captures a validated completion only when its head has commits
beyond the base its PR targets: a head that base already contains has nothing
to publish, and a PR of it is refused by the host. For an ordinary issue that
base is the default branch; for a stack successor it is its predecessor's
branch, which the stack base gate owns (ADR-0029). A successor whose head
equals its predecessor's is ahead of the default branch yet has nothing of its
own (#7347 PR 2 review r1).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .stack_publish_gate import StackBaseGate

#: The remote the orchestrator publishes to.
_REMOTE = "origin"


@dataclass(frozen=True, slots=True)
class PullRequestBaseRef:
    """``(issue, worktree) -> remote ref`` of the base the issue's PR targets.

    ``None`` when the base cannot be established (the stack gate could not read
    or confirm the issue): an unknown base proves nothing, so the caller
    preserves the head.
    """

    default_branch: Callable[[], str]
    #: ``None`` only for compositions without stack policy (never production).
    stack_gate: "StackBaseGate | None"

    def __call__(self, issue_number: int, worktree: Path) -> str | None:
        if self.stack_gate is None:
            return f"{_REMOTE}/{self.default_branch()}"
        decision = self.stack_gate.decide_work(issue_number)
        if not decision.allowed:
            return None
        if decision.is_stack and decision.base_branch:
            return f"{_REMOTE}/{decision.base_branch}"
        return f"{_REMOTE}/{self.default_branch()}"


__all__ = ["PullRequestBaseRef"]
