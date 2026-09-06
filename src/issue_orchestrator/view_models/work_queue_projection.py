"""Keep evidence ledgers out of work columns without changing stored facts."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import TypeVar

from ..domain.issue_work_classification import IssueWorkClassification, classify_issue_work
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
    classifications = {
        entry.issue_number: classify_issue_work(entry.issue_labels)
        for entry in history if entry.issue_labels
    }
    classifications.update({
        issue.number: classify_issue_work(issue.labels) for issue in (*active_issues, *queue_issues, *scope_issues)
    })
    classifications.update(retained_classifications or {})
    evidence = frozenset(number for number, kind in classifications.items()
                         if kind is IssueWorkClassification.EVIDENCE)
    return WorkQueueProjection(evidence_numbers=evidence)
