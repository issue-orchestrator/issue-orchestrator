"""Keep evidence ledgers out of work columns without changing stored facts."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TypeVar

from ..domain.issue_work_classification import IssueWorkClassification, resolve_work_classifications
from ..domain.models import SessionHistoryEntry
from ..ports.issue import Issue


Item = TypeVar("Item")


@dataclass(frozen=True)
class WorkQueueProjection:
    """Evidence classification shared by historical cards and current work counts."""

    evidence_numbers: frozenset[int]

    def work_items(
        self, items: Sequence[Item], *, issue_number: Callable[[Item], int | None],
    ) -> list[Item]:
        return [item for item in items if issue_number(item) not in self.evidence_numbers]


def project_work_queue(
    *, queue_issues: Sequence[Issue], scope_issues: Sequence[Issue],
    history: Sequence[SessionHistoryEntry] = (),
    active_issues: Sequence[Issue] = (),
    retained_classifications: Mapping[int, IssueWorkClassification] | None = None,
) -> WorkQueueProjection:
    """Use retained identity and historical labels across queue refreshes.

    The queue owner records current observations before projecting them. Its
    retained identity outranks restored session/cache/history labels; absence from a
    fetched scope does not erase identity. Source history remains intact.
    """
    classifications = resolve_work_classifications(
        historical_labels=((entry.issue_number, entry.issue_labels) for entry in history),
        active_labels=((issue.number, issue.labels) for issue in active_issues),
        cached_labels=((issue.number, issue.labels) for issue in (*queue_issues, *scope_issues)),
        retained=retained_classifications or {},
    )
    evidence = frozenset(number for number, kind in classifications.items()
                         if kind is IssueWorkClassification.EVIDENCE)
    return WorkQueueProjection(evidence_numbers=evidence)
