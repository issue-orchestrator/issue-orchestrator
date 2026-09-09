"""A scoped view of the shared block, retaining its existing policy and store.

Fence EACH boundary read/write, including the steps inside acquire/release;
authenticating only the outer call would allow a lost claim during a read to
reach the subsequent label write.
"""

from contextlib import AbstractContextManager
from dataclasses import dataclass, replace
from ..domain.issue_disposition_gate import IssueDispositionGateStatus
from typing import TYPE_CHECKING

from ..ports.pending_work_claim_store import NeedsHumanCauseStore
from ..ports.synchronous_effects import SynchronousEffectScope

if TYPE_CHECKING:
    from .needs_human_block import BlockLabelWriter, NeedsHumanBlock


@dataclass(frozen=True, slots=True)
class _ScopedCauses:
    causes: NeedsHumanCauseStore
    scope: SynchronousEffectScope

    def mutate_needs_human(
        self, issue_number: int
    ) -> AbstractContextManager[IssueDispositionGateStatus]:
        return self.causes.mutate_needs_human(issue_number)

    def begin_needs_human_removal(self, issue_number: int) -> bool:
        return self.scope.perform(
            lambda: self.causes.begin_needs_human_removal(issue_number)
        )

    def record_needs_human_cause(
        self, issue_number: int, cause: str, *, reason: str
    ) -> None:
        self.scope.perform(
            lambda: self.causes.record_needs_human_cause(
                issue_number, cause, reason=reason
            )
        )

    def restart_needs_human_causes(
        self, issue_number: int, cause: str, *, reason: str
    ) -> None:
        self.scope.perform(
            lambda: self.causes.restart_needs_human_causes(
                issue_number, cause, reason=reason
            )
        )

    def needs_human_causes(self, issue_number: int) -> frozenset[str]:
        return self.scope.perform(lambda: self.causes.needs_human_causes(issue_number))

    def withdraw_needs_human_cause(self, issue_number: int, cause: str) -> None:
        self.scope.perform(
            lambda: self.causes.withdraw_needs_human_cause(issue_number, cause)
        )

    def clear_needs_human_causes(self, issue_number: int) -> None:
        self.scope.perform(lambda: self.causes.clear_needs_human_causes(issue_number))


def scoped_human_block(
    owner: "NeedsHumanBlock", scope: SynchronousEffectScope, labels: "BlockLabelWriter"
) -> "NeedsHumanBlock":
    return replace(
        owner,
        labels=labels,
        read_labels=lambda number: scope.perform(lambda: owner.read_labels(number)),
        quarantined_issue_numbers=lambda: scope.perform(
            owner.quarantined_issue_numbers
        ),
        causes=_ScopedCauses(owner.causes, scope),
    )
