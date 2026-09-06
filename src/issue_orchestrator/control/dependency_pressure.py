"""Project work-blocking dependency reports into scheduling pressure.

Resolved gate reports remain the authority: prose mentions, satisfied stack
edges, unresolved references and invalid edges never acquire scheduling weight.
The projection counts unique reachable dependents, not paths through a diamond.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from ..domain.dependencies import Dependency, DependencyState
from ..domain.dependency_gates import DependencyGateReport


@dataclass(frozen=True)
class DependencyPressure:
    """Per-snapshot transitive dependent counts, keyed by local issue number."""

    dependent_counts: Mapping[int, int]

    def __post_init__(self) -> None:
        object.__setattr__(self, "dependent_counts", MappingProxyType(dict(self.dependent_counts)))

    def count_for(self, issue_number: int) -> int:
        return self.dependent_counts.get(issue_number, 0)


def project_dependency_pressure(
    reports: Mapping[int, DependencyGateReport],
    *,
    open_issue_numbers: frozenset[int],
    repository: str | None,
) -> DependencyPressure:
    """Count only open, local successors whose work gate is actually blocked."""
    successors: dict[int, set[int]] = {}
    for number, report in reports.items():
        if number not in open_issue_numbers:
            continue
        for dependency in local_work_blockers(report, repository=repository):
            assert dependency.issue_number is not None
            if dependency.issue_number in open_issue_numbers:
                successors.setdefault(dependency.issue_number, set()).add(number)

    counts: dict[int, int] = {}
    for root in successors:
        seen = {root}
        pending = list(successors[root])
        while pending:
            successor = pending.pop()
            if successor in seen:
                continue
            seen.add(successor)
            pending.extend(successors.get(successor, ()))
        counts[root] = len(seen) - 1
    return DependencyPressure(counts)


def local_work_blockers(
    report: DependencyGateReport, *, repository: str | None
) -> tuple[Dependency, ...]:
    """Select resolved local open edges that currently prevent work.

    The gate owner decides whether an edge blocks. This shared projection keeps
    scheduling pressure and outside-scope diagnostics on the same definition.
    """
    blocking_refs = {block.dependency_ref for block in report.work.blocks}
    return tuple(
        dependency for dependency in report.dependencies
        if dependency.issue_number is not None
        and (dependency.repository is None or (
            repository is not None
            and dependency.repository.casefold() == repository.casefold()
        ))
        and dependency.problem is None
        and dependency.state is DependencyState.UNSATISFIED
        and dependency.display_ref in blocking_refs
    )
