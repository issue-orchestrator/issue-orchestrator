"""Keep evidence ledgers out of work columns without changing stored facts."""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import TypeVar

from ..domain.tech_lead_session import is_tech_lead_observation_label
from ..ports.issue import Issue


Item = TypeVar("Item")


@dataclass(frozen=True)
class WorkQueueProjection:
    """Snapshot-owned evidence classification shared by cards and work counts."""

    evidence_numbers: frozenset[int]
    queued_evidence_count: int

    def work_items(
        self, items: Sequence[Item], *, issue_number: Callable[[Item], int | None],
    ) -> list[Item]:
        return [item for item in items if issue_number(item) not in self.evidence_numbers]

    def work_total(self, queue_total: int) -> int:
        # Startup deliberately reports zero before restored queue facts are ready.
        return max(0, queue_total - self.queued_evidence_count)


def project_work_queue(
    *, queue_issues: Sequence[Issue], scope_issues: Sequence[Issue],
) -> WorkQueueProjection:
    """Use the domain marker in both current snapshots, never title heuristics.

    Classify after collecting scope/history/retry cards so older failure cards
    cannot reintroduce current evidence records. Stored facts remain intact for
    the tech-lead evidence view.
    """
    evidence = frozenset(
        issue.number for issue in (*queue_issues, *scope_issues)
        if any(is_tech_lead_observation_label(label) for label in issue.labels)
    )
    return WorkQueueProjection(
        evidence_numbers=evidence,
        queued_evidence_count=sum(issue.number in evidence for issue in queue_issues),
    )
