"""Whether an issue's open question to a human withholds its PR's review (#7593).

The ONE owner of that policy, consulted by every path that decides whether a
pending review may run (discovery, startup recovery, and launch).

``needs-human`` used to withhold review whatever put it there. When the only
thing holding it is the coding agent's OWN question (``coding-done
needs_human``, or a reserved ``needs-human`` pr_label routed to the issue,
#7592), the work is already published on its PR and the question is about what
to do with it. Withholding the review then helps nobody: porchpin#364's PR #379
was dropped from the review queue every loop (``QUEUED → SKIP (stale pending
review: issue_blocked)``) while the maintainer was being asked to decide about
it, and a reviewed PR is a better basis for that decision than an unreviewed
one.

So such a block is *admitted* for review: it no longer withholds the review,
and it still holds everything else. Rework stays withheld (rework lanes do not
consult this owner), the coding agent is not relaunched (the scheduler still
treats the label as blocking), and the merge queue never enqueues a PR whose
issue carries ``needs-human`` (``MergeQueueCoordinator``), so an approval
cannot land the work before the human has answered.

Any other cause keeps the old rule. In particular a tech-lead escalation is not
admitted: its marker's reconciler clears an escalation once a session is
active on the issue, so a review launched over it would silently withdraw the
hand-over. A ``needs-human`` with no recorded cause is the operator's own
intent and is never second-guessed.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from ..domain.human_block import NeedsHumanCause

if TYPE_CHECKING:
    from .label_manager import LabelManager
    from .needs_human_block import SharedNeedsHumanBlock

logger = logging.getLogger(__name__)

#: The causes whose block is a question about already-published work.
_REVIEW_ADMITTED_CAUSES = frozenset({NeedsHumanCause.AGENT_COMPLETION})


class NumberedLabelledIssue(Protocol):
    @property
    def number(self) -> int: ...

    @property
    def labels(self) -> Sequence[str]: ...


class ReviewQuestionHolds(Protocol):
    """Which of an issue's blocking labels do not withhold its PR's review."""

    def review_admitted_blocks(self, issue: NumberedLabelledIssue | None) -> frozenset[str]:
        """Casefolded blocking labels the review may run over."""
        ...


class _NoAdmittedBlocks:
    def review_admitted_blocks(self, issue: NumberedLabelledIssue | None) -> frozenset[str]:
        del issue
        return frozenset()


#: Every block withholds review (no cause store wired).
NO_REVIEW_ADMITTED_BLOCKS: ReviewQuestionHolds = _NoAdmittedBlocks()


@dataclass(frozen=True)
class AgentQuestionReviewHolds:
    """An agent's own question does not withhold the review of its PR."""

    block: "SharedNeedsHumanBlock"
    label_manager: "LabelManager"

    def review_admitted_blocks(self, issue: NumberedLabelledIssue | None) -> frozenset[str]:
        if issue is None:
            return frozenset()
        needs_human = self.label_manager.needs_human.casefold()
        folded = {name.casefold() for name in issue.labels}
        if needs_human not in folded:
            return frozenset()
        if self.label_manager.tech_lead_needs_human.casefold() in folded:
            return frozenset()  # a tech-lead hand-over is never admitted
        try:
            causes = self.block.recorded_causes((issue.number,)).get(issue.number, frozenset())
        except Exception:
            # The cause store could not be read: keep the review withheld,
            # exactly as before this owner existed, and say so.
            logger.exception(
                "[review] needs-human causes of issue #%d unreadable; the block"
                " keeps withholding its PR's review",
                issue.number,
            )
            return frozenset()
        if causes and causes <= _REVIEW_ADMITTED_CAUSES:
            return frozenset({needs_human})
        return frozenset()
