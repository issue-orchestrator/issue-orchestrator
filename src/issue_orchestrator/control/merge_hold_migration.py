"""Move an agent's question about a PR from its issue to the PR's merge hold (#7678).

Before #7678 an agent's ``pr_labels: [needs-human]`` landed on the ISSUE as an
``agent_completion`` work block (#7595), so a question about the PR's merge
held the whole item: porchpin#364, whose published PR #379 waits on the
maintainer's call before its rebase. New requests are typed at the door
(``completion_pr_labels``); this moves one that already landed.

The recorded reason cannot tell that request from an agent's pre-work
question (porchpin#262 records the same one), so the move is an operator's
explicit, named command - never inferred - and it refuses anything else:

* the issue's ``needs-human`` must be held by the agent's own question alone
  (no other recorded cause, no tech-lead hand-over);
* the PR must be open and be the issue's own.

The hold goes on the PR first, then the issue's cause is released, so a
failure between the two leaves the question held twice, never dropped.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from functools import cache
from typing import TYPE_CHECKING

from ..domain.human_block import BlockOutcome, HumanBlockRequest, NeedsHumanCause
from .human_gates import merge_decision_request
from .needs_human_standing_generation import StandingGenerationUnknown

if TYPE_CHECKING:
    from ..ports.issue import Issue
    from ..ports.pull_request_tracker import PRInfo
    from .label_manager import LabelManager
    from .needs_human_block import SharedNeedsHumanBlock


class MergeHoldMoveStatus(StrEnum):
    MOVED = "moved"
    #: Dry run: every precondition holds; nothing was written.
    WOULD_MOVE = "would_move"
    REFUSED = "refused"
    #: A write did not commit; what landed is named in the detail.
    FAILED = "failed"


@dataclass(frozen=True)
class MergeHoldMove:
    status: MergeHoldMoveStatus
    detail: str


@dataclass(frozen=True)
class MergeHoldMigration:
    block: "SharedNeedsHumanBlock"
    labels: "LabelManager"
    read_issue: Callable[[int], "Issue | None"]
    read_pr: Callable[[int], "PRInfo | None"]
    #: The issue a PR belongs to, as review scoping reads it.
    pr_issue_number: Callable[["PRInfo"], int | None]

    def move(self, issue_number: int, pr_number: int, *, apply: bool) -> MergeHoldMove:
        refusal = self._refusal(issue_number, pr_number)
        if refusal is not None:
            return MergeHoldMove(MergeHoldMoveStatus.REFUSED, refusal)
        if not apply:
            return MergeHoldMove(
                MergeHoldMoveStatus.WOULD_MOVE,
                f"#{issue_number}'s question would move to PR #{pr_number}'s merge hold",
            )
        held = self.block.acquire(merge_decision_request(
            pr_number, f"moved from issue #{issue_number}: the agent's question is about this PR's merge"
        ))
        if not held.committed:
            return MergeHoldMove(
                MergeHoldMoveStatus.FAILED, f"PR #{pr_number}'s merge hold did not commit ({held.value})"
            )
        released = self.block.release(HumanBlockRequest(
            target=issue_number, cause=NeedsHumanCause.AGENT_COMPLETION,
            reason=f"moved to PR #{pr_number}'s merge hold (#7678)",
        ))
        if released is BlockOutcome.FAILED:
            return MergeHoldMove(
                MergeHoldMoveStatus.FAILED,
                f"PR #{pr_number} holds the merge, but #{issue_number}'s block did not come off; rerun",
            )
        return MergeHoldMove(
            MergeHoldMoveStatus.MOVED,
            f"#{issue_number}'s question now holds PR #{pr_number}'s merge ({released.value} on the issue)",
        )

    def _refusal(self, issue_number: int, pr_number: int) -> str | None:
        """The first precondition that fails, as the operator should read it."""
        issue = self.read_issue(issue_number)
        pr = self.read_pr(pr_number)
        needs_human = self.labels.needs_human
        causes = cache(lambda: self._causes(issue_number))  # one events read
        checks: tuple[tuple[Callable[[], bool], Callable[[], str]], ...] = (
            (lambda: issue is not None and issue.state == "open",
             lambda: f"issue #{issue_number} is not open"),
            (lambda: _carries(issue, needs_human),
             lambda: f"issue #{issue_number} carries no {needs_human}"),
            (lambda: not _carries(issue, self.labels.tech_lead_needs_human),
             lambda: f"issue #{issue_number} is handed over by the tech lead, not held by an agent's question"),
            (lambda: causes() == _AGENT_QUESTION_ONLY,
             lambda: f"issue #{issue_number}'s {needs_human} is held by"
                     f" {_named(causes())}, not only an agent's question"),
            (lambda: pr is not None and pr.state.lower() == "open",
             lambda: f"PR #{pr_number} is not open"),
            (lambda: pr is not None and self.pr_issue_number(pr) == issue_number,
             lambda: f"PR #{pr_number} is not issue #{issue_number}'s"),
        )
        return next((why() for holds, why in checks if not holds()), None)

    def _causes(self, issue_number: int) -> frozenset[NeedsHumanCause] | None:
        """The causes on the generation standing: a person who put the label
        back by hand ended the agent's question (#8774). None: unreadable."""
        try:
            return self.block.standing_causes(issue_number)
        except StandingGenerationUnknown:
            return None


_AGENT_QUESTION_ONLY = frozenset({NeedsHumanCause.AGENT_COMPLETION})


def _carries(issue: "Issue | None", name: str) -> bool:
    return issue is not None and any(label.casefold() == name.casefold() for label in issue.labels)


def _named(causes: frozenset[NeedsHumanCause] | None) -> str:
    if causes is None:
        return "causes that cannot be verified against GitHub's label events"
    return ", ".join(sorted(cause.value for cause in causes)) or "no recorded cause"


__all__ = ["MergeHoldMigration", "MergeHoldMove", "MergeHoldMoveStatus"]
