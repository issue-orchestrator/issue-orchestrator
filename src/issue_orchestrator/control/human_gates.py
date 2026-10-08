"""THE owner of what a blocking label holds: an item's work, or only a merge (#7678).

``needs-human`` is one label with two meanings. On an issue, or on a PR whose
engine gave up on it, it holds the WORK: no launch, review or rework. A person
asked to decide before a PR merges (an agent's question beside its published
work) holds only the MERGE: review, rework and conflict rework proceed. The
type lives on the causes the shared block records against the number
(:class:`~..domain.human_block.HumanHoldScope`), and every reader asks this
owner rather than reading the label set itself:

* the review lane (``review_validity``, discovery, launch, startup recovery),
* the rework scan and the published-review release,
* the merge queue (:func:`holds_merge`),
* the tech lead's ``resolve_block`` (#7658), which acts on WORK holds only.

The API below is kept stable for those callers: ``needs_human_scope``,
``work_blocking``, ``holds_work`` and ``holds_merge``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..domain.human_block import HumanHoldScope, NeedsHumanCause, needs_human_hold

if TYPE_CHECKING:
    from ..domain.human_block import HumanBlockRequest
    from .label_manager import LabelManager
    from .needs_human_block import SharedNeedsHumanBlock

logger = logging.getLogger(__name__)

#: The causes recorded against each number, as the shared block reads them.
CauseReader = Callable[[Sequence[int]], Mapping[int, frozenset[NeedsHumanCause]]]


@dataclass(frozen=True)
class HumanGates:
    """What each blocking label on a number holds (see the module docstring)."""

    labels: "LabelManager"
    causes: CauseReader

    @classmethod
    def over(cls, block: "SharedNeedsHumanBlock", labels: "LabelManager") -> "HumanGates":
        # A merge-scoped record is checked against the standing generation (#8774).
        return cls(labels=labels, causes=block.hold_causes)

    @classmethod
    def unrecorded(cls, labels: "LabelManager") -> "HumanGates":
        """Gates for a reader with no cause record (an exam observer, a
        projection): every ``needs-human`` reads as the WORK hold it would be
        with no recorded cause."""
        return cls(labels=labels, causes=lambda numbers: {})

    def issue_work_blocking(self, labels: Sequence[str]) -> tuple[str, ...]:
        """The blocking labels on an ISSUE: all of them hold its work.

        A merge-scoped hold is only ever recorded against a PR
        (:func:`merge_decision_request`), so an issue's ``needs-human`` is
        always a work hold and needs no cause read.
        """
        return tuple(self.labels.get_blocking(labels))

    def needs_human_scope(self, number: int, labels: Sequence[str]) -> HumanHoldScope | None:
        """What the shared ``needs-human`` on ``number`` holds; None when absent.

        A tech-lead hand-over marker beside it, an unreadable cause record, or
        no recorded cause at all (a label put on by hand) is WORK: the scope
        that holds the most is the one a doubt resolves to.
        """
        return needs_human_hold(
            labels,
            needs_human=self.labels.needs_human,
            handover_marker=self.labels.tech_lead_needs_human,
            causes=lambda: self._recorded(number),
        )

    def _recorded(self, number: int) -> frozenset[NeedsHumanCause]:
        """The causes recorded against ``number``; none (so WORK) when unreadable."""
        try:
            return self.causes([number]).get(number, frozenset())
        except Exception:
            logger.exception(
                "[GATES] needs-human causes of #%d unreadable; holding its work", number
            )
            return frozenset()

    def work_blocking(self, number: int, labels: Sequence[str]) -> tuple[str, ...]:
        """The blocking labels on ``number`` that hold its WORK (review, rework, launch)."""
        merge_only = self.needs_human_scope(number, labels) is HumanHoldScope.MERGE
        needs_human = self.labels.needs_human.casefold()
        return tuple(
            name for name in self.labels.get_blocking(labels)
            if not (merge_only and name.casefold() == needs_human)
        )

    def holds_work(self, number: int, labels: Sequence[str]) -> bool:
        return bool(self.work_blocking(number, labels))

    def holds_merge(self, *label_sets: Sequence[str]) -> bool:
        """See :func:`holds_merge`."""
        return holds_merge(self.labels, *label_sets)


def holds_merge(labels: "LabelManager", *label_sets: Sequence[str]) -> bool:
    """Whether a person must decide before a merge (the merge queue's rule).

    ``needs-human`` of EITHER scope, on the issue or on its PR: a work hold
    waits on a person, and a merge hold is exactly "do not merge yet". It
    needs no cause read, so the merge queue applies it to the labels it holds.
    """
    needs_human = labels.needs_human.casefold()
    return any(label.casefold() == needs_human for names in label_sets for label in names)


def merge_decision_request(pr_number: int, reason: str) -> "HumanBlockRequest":
    """The ONE way a merge-scoped hold is asked for: against a PR (#7678)."""
    from ..domain.human_block import HumanBlockRequest

    return HumanBlockRequest(target=pr_number, cause=NeedsHumanCause.MERGE_DECISION, reason=reason)


__all__ = ["CauseReader", "HumanGates", "holds_merge", "merge_decision_request"]
