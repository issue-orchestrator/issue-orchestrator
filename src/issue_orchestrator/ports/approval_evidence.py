"""Ports: the evidence behind a tech-lead proposal approval (#7763).

Two reads and one ledger, kept apart from the fat repository host so the
approval owner depends on exactly what it uses:

* :class:`ApprovalEvidenceReader` — who applied a label that stands now, and what role a
  login holds in the repository. GitHub's issue events and collaborator
  permission endpoints back it.
* :class:`OperatorApprovalRecords` — the engine-owned record of approvals an
  operator gave in the Control Center, keyed by proposal and bound to the
  exact label event the engine's write produced.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Protocol

from ..domain.tech_lead_approval import LabelEvent, OperatorApprovalRecord, StandingLabel


class ApprovalEvidenceReader(Protocol):
    """Read-only evidence about who approved an item."""

    def standing_label(self, issue_number: int, label: str) -> StandingLabel | None:
        """Every ``labeled`` event adding *label* (case-insensitive) since it
        was last absent, oldest first — the application in force now and any
        later labeled event GitHub recorded while it stayed on (#8346).

        ``None`` means the complete event history shows the label not
        standing (never applied, or removed, or voided by a close or reopen
        since). A read that cannot prove completeness raises instead.
        """
        ...

    def latest_label_removal(self, issue_number: int, label: str) -> LabelEvent | None:
        """The newest ``unlabeled`` event taking *label* off, while it is
        still off; ``None`` when the complete history has none standing."""
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
    still finds it. A row is never deleted (#7763 review r18 F1): a closed
    proposal turns INACTIVE (out of the scope's reads) but stays known, so
    one reopened after an edit stripped it is still a proposal.
    """

    def index_proposals(self, numbers: Iterable[int]) -> None:
        """Add, or reactivate, live proposals (never un-declines one)."""
        ...

    def indexed_proposals(self) -> frozenset[int]:
        """The ACTIVE proposals the approval scope reads (not declined)."""
        ...

    def known_proposals(self) -> frozenset[int]:
        """Every proposal ever indexed: active, inactive or declined."""
        ...

    def retire_proposals(self, numbers: Iterable[int]) -> None:
        """Mark closed or gone proposals inactive; identity is kept."""
        ...

    def decline_proposals(self, numbers: Iterable[int]) -> None:
        """Mark op-backed proposals the operator declined (#7763 review r7 F2):
        a reopened one is never reinterpreted as follow-up work."""
        ...

    def declined_proposals(self) -> frozenset[int]: ...

    def is_known(self, number: int) -> bool:
        """A targeted, uncached read: is *number* any row (active, inactive
        or declined)? Admission and launch consent ask it (#7763 r25 F1)."""
        ...

    def is_declined(self, number: int) -> bool:
        """A targeted, uncached read: consent asks it every time, since
        another engine sharing the store may have declined since (#7763 r24)."""
        ...


class InMemoryProposalIssueIndex:
    """Process-local :class:`ProposalIssueIndex` for tests and fakes."""

    def __init__(self) -> None:
        self._numbers: set[int] = set()
        self._inactive: set[int] = set()
        self._declined: set[int] = set()

    def index_proposals(self, numbers: Iterable[int]) -> None:
        added = set(numbers)
        self._numbers.update(added)
        self._inactive.difference_update(added)

    def indexed_proposals(self) -> frozenset[int]:
        return frozenset(self._numbers - self._declined - self._inactive)

    def known_proposals(self) -> frozenset[int]:
        return frozenset(self._numbers | self._declined)

    def retire_proposals(self, numbers: Iterable[int]) -> None:
        self._inactive.update(set(numbers) & self._numbers)

    def decline_proposals(self, numbers: Iterable[int]) -> None:
        self._declined.update(numbers)

    def declined_proposals(self) -> frozenset[int]:
        return frozenset(self._declined)

    def is_declined(self, number: int) -> bool:
        return number in self._declined

    def is_known(self, number: int) -> bool:
        return number in self._numbers or number in self._declined


__all__ = [
    "ApprovalEvidenceReader",
    "InMemoryOperatorApprovalRecords",
    "InMemoryProposalIssueIndex",
    "OperatorApprovalRecords",
    "ProposalIssueIndex",
]
