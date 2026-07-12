"""Port for durable stale needs-human label-clear provenance (#6771 round 7).

When a recovered triage investigation supersedes an incomplete needs-human
escalation, the orchestrator owns the removal of the stale source-of-truth
label it applied. That removal is an external mutation and can fail; the retry
must survive a process restart.

This store is the durable provenance of the orchestrator's OWN incomplete
clear: an issue number appears here ONLY because the orchestrator initiated a
stale-label removal that has not yet committed. Restart recovery reads this
record instead of inferring ownership from "an active session still carries
needs-human" — a conjunction of two independently valid facts (a running
session AND a globally meaningful human-control label) that an operator or a
running session transitioning to needs-human also satisfies, so inferring
ownership from it would strip a legitimate stop/escalation on the next tick
(#6771 round 7 finding).

Constructed once at the composition root (``entrypoints/bootstrap.py``) and
injected into the session launcher, which owns the needs-human lifecycle. Tests
mock this protocol; the durable JSON implementation lives in
``execution/json_needs_human_clear_store.py``.
"""

from __future__ import annotations

from typing import Protocol


class NeedsHumanClearStore(Protocol):
    """Durable set of issue numbers with an orchestrator-owed needs-human clear."""

    def record(self, issue_number: int) -> None:
        """Persist that a stale needs-human removal is owed for this issue.

        Idempotent: recording an already-pending issue is a no-op.
        """
        ...

    def discard(self, issue_number: int) -> None:
        """Drop the record once the removal commits. No-op if absent."""
        ...

    def pending_issue_numbers(self) -> list[int]:
        """Return the issue numbers whose stale needs-human removal is still owed."""
        ...


class InMemoryNeedsHumanClearStore:
    """Non-durable store for tests that do not exercise restart durability."""

    def __init__(self) -> None:
        self._pending: list[int] = []

    def record(self, issue_number: int) -> None:
        if issue_number not in self._pending:
            self._pending.append(issue_number)

    def discard(self, issue_number: int) -> None:
        if issue_number in self._pending:
            self._pending.remove(issue_number)

    def pending_issue_numbers(self) -> list[int]:
        return list(self._pending)
