"""Keep repository observations distinct from a merged queue projection."""

from dataclasses import dataclass

from ..ports.issue import Issue


@dataclass(frozen=True, slots=True)
class IssueRefreshBatch:
    """A scoped projection can omit closed observations without losing them."""

    issues: tuple[Issue, ...]
    observed_issues: tuple[Issue, ...]
    watermark: str | None

    @property
    def refreshed_numbers(self) -> set[int]:
        return {issue.number for issue in self.observed_issues}
