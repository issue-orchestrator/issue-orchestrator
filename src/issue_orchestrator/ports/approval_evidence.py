"""Ports: the evidence behind a tech-lead proposal approval (#7763).

Two reads and one ledger, kept apart from the fat repository host so the
approval owner depends on exactly what it uses:

* :class:`ApprovalEvidenceReader` — who last applied a label, and what role a
  login holds in the repository. GitHub's issue events and collaborator
  permission endpoints back it.
* :class:`OperatorApprovalRecords` — the engine-owned record of approvals an
  operator gave in the Control Center, keyed by proposal and bound to the
  exact label event the engine's write produced.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from ..domain.tech_lead_approval import LabelEvent, OperatorApprovalRecord


class ApprovalEvidenceReader(Protocol):
    """Read-only evidence about who approved an item."""

    def latest_label_event(
        self, issue_number: int, label: str, *, removed: bool = False
    ) -> LabelEvent | None:
        """The newest ``labeled`` event adding *label* (case-insensitive), or
        with ``removed`` the newest ``unlabeled`` event taking it off.

        ``None`` means the complete event history has no such event. A read
        that cannot prove completeness raises instead of answering ``None``.
        """
        ...

    def repository_role(self, login: str) -> str | None:
        """*login*'s role in the repository (``admin``, ``maintain``,
        ``write``, ``triage``, ``read``), or ``None`` for a non-collaborator."""
        ...

    def is_own_write(self, event: LabelEvent) -> bool:
        """Whether *event* was produced by THIS engine's own credential.

        Decidable only for a GitHub App identity (the event's
        ``performed_via_github_app`` names the app); a personal-token engine
        answers ``False``, so its writes are judged by the actor check alone.
        """
        ...


class OperatorApprovalRecords(Protocol):
    """Control Center approvals, owned by the engine and nothing else."""

    def record_operator_approval(self, record: OperatorApprovalRecord) -> None: ...

    def load_operator_approval(self, issue_number: int) -> OperatorApprovalRecord | None: ...

    def discard_operator_approval(self, issue_number: int) -> None: ...


class InMemoryOperatorApprovalRecords:
    """Process-local :class:`OperatorApprovalRecords` for tests and fakes."""

    def __init__(self) -> None:
        self._records: dict[int, OperatorApprovalRecord] = {}

    def record_operator_approval(self, record: OperatorApprovalRecord) -> None:
        self._records[record.issue_number] = record

    def load_operator_approval(self, issue_number: int) -> OperatorApprovalRecord | None:
        return self._records.get(issue_number)

    def discard_operator_approval(self, issue_number: int) -> None:
        self._records.pop(issue_number, None)


class ProposalIssueIndex(Protocol):
    """Every proposal this engine knows it filed or migrated (#7763 r6 F2).

    Label queries find a proposal only while it carries a gate label; a bulk
    edit can strip all of them, leaving a proposal whose body marker still
    blocks it but that no label names. The index is how the approval scope
    still finds it: numbers in, retired once the item is closed or gone.
    """

    def index_proposals(self, numbers: Iterable[int]) -> None: ...

    def indexed_proposals(self) -> frozenset[int]: ...

    def retire_proposals(self, numbers: Iterable[int]) -> None: ...


class InMemoryProposalIssueIndex:
    """Process-local :class:`ProposalIssueIndex` for tests and fakes."""

    def __init__(self) -> None:
        self._numbers: set[int] = set()

    def index_proposals(self, numbers: Iterable[int]) -> None:
        self._numbers.update(numbers)

    def indexed_proposals(self) -> frozenset[int]:
        return frozenset(self._numbers)

    def retire_proposals(self, numbers: Iterable[int]) -> None:
        self._numbers.difference_update(numbers)


__all__ = [
    "ApprovalEvidenceReader",
    "InMemoryOperatorApprovalRecords",
    "InMemoryProposalIssueIndex",
    "OperatorApprovalRecords",
    "ProposalIssueIndex",
]
