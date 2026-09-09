"""Serialize sibling admission with the aggregate recovery-block projection."""

from contextlib import AbstractContextManager
from typing import Protocol

from ..domain.issue_disposition_gate import IssueDispositionGateStatus


class IssueDispositionMutationGate(Protocol):
    def try_acquire(
        self, repo_slug: str, issue_number: int
    ) -> AbstractContextManager[IssueDispositionGateStatus]:
        """Try once on context entry; BUSY authorizes no reads or mutations.

        Hold through the fresh sibling read and label write. Acquire execution
        and any required record claim BEFORE entering this gate, never inside
        it. Do not run validation or publication subprocesses while holding it.
        Exit releases the gate even on failure; acquisition/teardown faults
        raise. The same-host lease has no timeout takeover or child inheritance.
        """
        ...
