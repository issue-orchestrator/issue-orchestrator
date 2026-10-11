"""Test doubles for the tech-lead proposal approval model (#7763).

``FakeApprovalEvidence`` is the GitHub side of approval: the label events on
each issue and the repository roles of their actors. Tests say who applied
``approved`` (a maintainer, a bot, a contributor) and the approval owner reads
it exactly as it reads GitHub.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from issue_orchestrator.control.tech_lead_approval import TechLeadApprovals
from issue_orchestrator.domain.tech_lead_approval import (
    APPROVED_LABEL,
    AWAITING_APPROVAL_LABEL,
    GATED_PROPOSAL_LABELS,
    TECH_LEAD_PROPOSAL_LABEL,
    LabelEvent,
    StandingLabel,
)
from issue_orchestrator.ports.approval_evidence import InMemoryOperatorApprovalRecords, InMemoryProposalIssueIndex

#: What a gated proposal is filed with.
GATED: tuple[str, ...] = GATED_PROPOSAL_LABELS
#: A proposal a maintainer approved but the engine has not admitted yet.
CLAIMED: tuple[str, ...] = (TECH_LEAD_PROPOSAL_LABEL, AWAITING_APPROVAL_LABEL, APPROVED_LABEL)
#: A proposal the engine admitted (it took the waiting label off).
ADMITTED: tuple[str, ...] = (TECH_LEAD_PROPOSAL_LABEL, APPROVED_LABEL)

MAINTAINER = "octo-maintainer"
CONTRIBUTOR = "octo-contributor"
BOT = "io-bot[bot]"
#: The engine's own GitHub App: GitHub names its bot account as the actor of
#: its writes (``performed_via_github_app`` stays null, #8987).
ENGINE = "io-engine[bot]"
#: GitHub's account id for each login the fakes use; ENGINE's is the App bot's.
ACCOUNT_IDS: dict[str, int] = {MAINTAINER: 101, CONTRIBUTOR: 102, BOT: 201, ENGINE: 301493143}
ENGINE_BOT_ID = ACCOUNT_IDS[ENGINE]


@dataclass
class FakeApprovalEvidence:
    """Label events and repository roles, as GitHub would answer them.

    Each issue's label transitions are an ordered log, oldest first, read the
    way GitHub's event listing is: a removal ends a label's standing run.
    """

    roles: dict[str, str] = field(
        default_factory=lambda: {MAINTAINER: "admin", CONTRIBUTOR: "write"}
    )
    transitions: dict[int, list[tuple[str, bool, LabelEvent]]] = field(default_factory=dict)
    #: When set, every issue without a recorded ``approved`` event reads as
    #: approved by this login (tests that only care that approval holds).
    default_approver: str | None = None
    event_reads: list[tuple[int, str, bool]] = field(default_factory=list)
    role_reads: list[str] = field(default_factory=list)
    _next_id: int = 1000

    def label(self, issue_number: int, label: str = APPROVED_LABEL, *, by: str = MAINTAINER,
              removed: bool = False) -> LabelEvent:
        """Record that *by* applied (or removed) *label* on the issue, newest.

        ``by=ENGINE`` is the engine's own App write (``engine_write``)."""
        self._next_id += 1
        event = LabelEvent(
            event_id=self._next_id,
            actor_login=by,
            actor_is_bot=by.endswith("[bot]"),
            created_at=f"2026-10-03T00:00:{self._next_id % 60:02d}Z",
            actor_id=ACCOUNT_IDS.get(by, 999),
        )
        self.record(issue_number, label, event, removed=removed)
        return event

    def record(self, issue_number: int, label: str, event: LabelEvent, *, removed: bool = False) -> None:
        """Append a prepared event to the issue's transition log."""
        self.transitions.setdefault(issue_number, []).append((label.casefold(), removed, event))

    def _run(self, issue_number: int, label: str, *, removed: bool) -> tuple[LabelEvent, ...]:
        folded = label.casefold()
        run: tuple[LabelEvent, ...] = ()
        for named, was_removal, event in self.transitions.get(issue_number, []):
            if named == folded:
                run = (*run, event) if was_removal == removed else ()
        return run

    def standing_label(self, issue_number: int, label: str) -> StandingLabel | None:
        self.event_reads.append((issue_number, label, False))
        if (
            self.default_approver
            and label.casefold() == APPROVED_LABEL
            and not any(named == APPROVED_LABEL for named, _, _ in self.transitions.get(issue_number, []))
        ):
            self.label(issue_number, by=self.default_approver)
        run = self._run(issue_number, label, removed=False)
        return StandingLabel(run) if run else None

    def latest_label_removal(self, issue_number: int, label: str) -> LabelEvent | None:
        self.event_reads.append((issue_number, label, True))
        run = self._run(issue_number, label, removed=True)
        return run[-1] if run else None

    def repository_role(self, login: str) -> str | None:
        self.role_reads.append(login)
        return self.roles.get(login)

    def is_own_write(self, event: LabelEvent) -> bool:
        return event.actor_is_bot and event.actor_id == ENGINE_BOT_ID

    def engine_write(self, issue_number: int, label: str = APPROVED_LABEL) -> LabelEvent:
        """The event the engine's own App write produces."""
        return self.label(issue_number, label, by=ENGINE)


def make_approvals(evidence: FakeApprovalEvidence | None = None) -> TechLeadApprovals:
    return TechLeadApprovals(
        evidence=evidence or FakeApprovalEvidence(),
        records=InMemoryOperatorApprovalRecords(),
        index=InMemoryProposalIssueIndex(),
        ledger_numbers=lambda: (),
    )


def approving_everything() -> TechLeadApprovals:
    """An owner for which every ``approved`` label is a maintainer's."""
    return make_approvals(FakeApprovalEvidence(default_approver=MAINTAINER))


def maintainer_approved(numbers: int | tuple[int, ...] = ()) -> TechLeadApprovals:
    """An owner whose evidence says a maintainer applied ``approved`` to each issue."""
    evidence = FakeApprovalEvidence()
    for number in (numbers,) if isinstance(numbers, int) else numbers:
        evidence.label(number)
    return make_approvals(evidence)


__all__ = [
    "ADMITTED",
    "BOT",
    "CLAIMED",
    "CONTRIBUTOR",
    "ENGINE",
    "ENGINE_BOT_ID",
    "FakeApprovalEvidence",
    "GATED",
    "MAINTAINER",
    "approving_everything",
    "make_approvals",
    "maintainer_approved",
]
