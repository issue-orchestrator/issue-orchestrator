"""The one owner of tech-lead proposal approval (#7763).

The label vocabulary lives in :mod:`~..domain.tech_lead_approval`; this module
owns the POLICY that turns labels plus evidence into decisions, and every
approval-label write the engine makes:

* **Verification** — :meth:`TechLeadApprovals.verify` answers "did a
  maintainer approve this?" from the latest ``approved`` labeled event: a
  Control Center approval recorded against that exact event, or a non-bot
  actor whose repository role is a maintainer role. It reads events only for
  items that carry ``approved``, and caches positive verdicts for the engine's
  lifetime (a restart re-verifies once). The cache is dropped for any item
  observed WITHOUT ``approved``.
* **Admission** — :meth:`TechLeadApprovals.admits` is the scheduler's rule: a
  proposal joins the work queue only in the ``ADMITTED`` label state AND with
  a verified approval held in this process. Label truth alone can never admit,
  so a stripped ``awaiting-approval`` plus a bot's ``approved`` stays out.
* **Settlement planning** — :func:`plan_approval_settlements` decides,
  read-free, which label transitions a tick's verdicts call for.
* **Settlement** — :func:`apply_settle_proposal_approval` is the single
  mutating boundary for those transitions; it re-verifies FRESH before an
  admission writes anything.

Execution of act-level ops is unchanged: reconciliation now treats an op as
approved only when its verdict approves, and the apply-time consent re-check
(:mod:`~.tech_lead_proposal_execution`) asks :meth:`TechLeadApprovals.confirm`
with a fresh read, so a stray strip or a bot label never executes anything.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable, Collection, Iterable, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..domain.tech_lead_approval import (
    APPROVED_LABEL,
    GATED_PROPOSAL_LABELS,
    MAINTAINER_ROLES,
    ApprovalSettlement,
    ApprovalTransition,
    ApprovalVerdict,
    ApprovalVerdictKind,
    ProposalLabelState,
    known_proposal_state,
    proposal_label_state,
    proposal_state,
)

if TYPE_CHECKING:
    from ..ports.approval_evidence import (
        ApprovalEvidenceReader,
        OperatorApprovalRecords,
        ProposalIssueIndex,
    )
    from ..domain.tech_lead_approval import LabelEvent
    from ..ports import RepositoryHost
    from ..ports.issue import Issue

logger = logging.getLogger(__name__)

#: How long a repository role answer is reused. Roles change rarely and an
#: approval re-reads the label event every time it is not cached, so this only
#: bounds collaborator-permission reads when several approvals arrive at once.
ROLE_CACHE_SECONDS = 600.0


@dataclass
class TechLeadApprovals:
    """Verification, admission and the verified-approval cache."""

    evidence: "ApprovalEvidenceReader"
    records: "OperatorApprovalRecords"
    index: "ProposalIssueIndex"
    #: The durable op ledger's proposal issue numbers: an op-backed proposal
    #: is known even before (or without) an index row (#7763 review r19 F1).
    op_numbers: Callable[[], Iterable[int]]
    clock: Callable[[], float] = time.monotonic
    _verified: dict[int, ApprovalVerdict] = field(default_factory=dict, init=False)
    _roles: dict[str, tuple[float, str | None]] = field(default_factory=dict, init=False)
    _scope: tuple["Issue", ...] = field(default=(), init=False)
    _scope_verdicts: dict[int, ApprovalVerdict] = field(default_factory=dict, init=False)
    _declined: frozenset[int] | None = field(default=None, init=False)
    _scope_observed: bool = field(default=False, init=False)
    _known: frozenset[int] | None = field(default=None, init=False)
    _active: frozenset[int] | None = field(default=None, init=False)

    # -- the last observed scope (read model) --------------------------------

    def record_scope(
        self,
        issues: Sequence["Issue"],
        verdicts: dict[int, ApprovalVerdict],
        *,
        retired: Iterable[int] = (),
    ) -> None:
        """Remember the last complete approval-scope observation.

        The read model for the UI is in-memory only: GitHub labels and body
        markers stay the truth, and a restart re-observes the scope on its
        first tick. Every observed proposal also joins the durable proposal
        index, so a later strip of all its gate labels cannot hide it; the
        ones the scope read found closed, gone or no longer a proposal leave
        it (#7763 review r6 F2).
        """
        declined = self.declined_numbers()  # the page model; consent reads it fresh
        self._scope = tuple(issue for issue in issues if issue.number not in declined)
        self._scope_verdicts = dict(verdicts)
        self._scope_observed = True
        self.index.index_proposals(issue.number for issue in issues)
        self.index.retire_proposals(retired)
        self._known = self._active = None

    def decline(self, issue_number: int) -> None:
        """An operator declines *issue_number*: final, for every proposal kind.

        The durable declined disposition is the FIRST write (#7763 review r13
        F1, r14 F1), so from that moment no consent check, settlement or
        admission accepts the proposal, whatever a crash leaves undone; the
        caller then closes it and startup's ``finish_interrupted_declines``
        completes an interrupted one. Its verified approval and Control Center
        record go, and it leaves the read model. A reopened declined proposal
        stays declined: re-proposing is a new proposal.
        """
        self.index.decline_proposals([issue_number])
        self._declined = None
        self._known = self._active = None
        self._verified.pop(issue_number, None)
        self.records.discard_operator_approval(issue_number)
        self.forget_from_scope(issue_number)

    def is_declined(self, issue_number: int) -> bool:
        """Read fresh from the durable store, never this process's cache: a
        second engine sharing the store may have declined it (#7763 r24 F1)."""
        return self.index.is_declined(issue_number)

    def declined_numbers(self) -> frozenset[int]:
        if self._declined is None:
            self._declined = self.index.declined_proposals()
        return self._declined

    def remember_proposals(self, numbers: Iterable[int]) -> None:
        """Index (or reactivate) proposals outside a scope observation."""
        added = list(numbers)
        if not added:
            return  # nothing changed: keep the cached reads
        self.index.index_proposals(added)
        self._known = self._active = None

    def known_proposals(self) -> frozenset[int]:
        """Every issue the engine knows is a proposal: indexed or declined.

        Proposal identity belongs to this owner (#7763 review r15 F1), not to
        what an issue's labels and body say today.
        """
        if self._known is None:
            self._known = self.index.known_proposals() | frozenset(self.op_numbers())
        return self._known

    def proposal_state_of(self, issue: "Issue") -> ProposalLabelState:
        """The issue's approval state, its known proposal identity included."""
        return known_proposal_state(
            issue.labels, issue.body, known=issue.number in self.known_proposals()
        )

    def indexed_proposals(self) -> frozenset[int]:
        """Every ACTIVE proposal the approval scope must keep finding."""
        if self._active is None:
            self._active = self.index.indexed_proposals()
        return self._active

    def forget_from_scope(self, issue_number: int) -> None:
        """Drop one item after a decline closed it, so the read model is current."""
        self._scope = tuple(issue for issue in self._scope if issue.number != issue_number)
        self._scope_verdicts.pop(issue_number, None)

    def mark_scope_unavailable(self) -> None:
        """A scope refresh is starting: until :meth:`record_scope` completes
        it, the last read model is not a current answer (#7763 r16 F2)."""
        self._scope_observed = False

    @property
    def scope_observed(self) -> bool:
        """Whether a complete approval-scope observation has succeeded yet.

        Until then ``observed_scope()`` is empty because nothing was looked
        at, not because nothing waits (#7763 review r10 F1).
        """
        return self._scope_observed

    def observed_scope(self) -> tuple[tuple["Issue", ApprovalVerdict | None], ...]:
        """Every open proposal last observed, with its verdict when it claims one."""
        return tuple((issue, self._scope_verdicts.get(issue.number)) for issue in self._scope)

    # -- verification ------------------------------------------------------

    def verify(self, issue: "Issue", *, fresh: bool = False) -> ApprovalVerdict:
        """Whether a maintainer approved *issue*, from its labels and events.

        ``fresh`` bypasses the cache: the apply-time consent re-check and the
        admission write use it so the approval they act on is this moment's.
        """
        number = issue.number
        if issue.state != "open":
            self._verified.pop(number, None)
            return ApprovalVerdict(number, ApprovalVerdictKind.CLOSED)
        if self.is_declined(number):  # final, reopened or not (#7763 r14 F1)
            self._verified.pop(number, None)
            return ApprovalVerdict(number, ApprovalVerdictKind.DECLINED)
        if not proposal_label_state(issue.labels).claims_approval:
            self._verified.pop(number, None)
            return ApprovalVerdict(number, ApprovalVerdictKind.NOT_CLAIMED)
        cached = None if fresh else self._verified.get(number)
        if cached is not None:
            return cached
        verdict = self._read_verdict(number, fresh=fresh)
        if verdict.approved:
            self._verified[number] = verdict
        else:
            self._verified.pop(number, None)
        return verdict

    def judge_latest_approval(self, number: int) -> ApprovalVerdict:
        """The verdict on the latest ``approved`` event alone, read fresh.

        For a caller that just wrote the label itself and so knows it is
        present without trusting a possibly-cached issue read.
        """
        return self._read_verdict(number, fresh=True)

    def _read_verdict(self, number: int, *, fresh: bool) -> ApprovalVerdict:
        event = self.evidence.latest_label_event(number, APPROVED_LABEL)
        if event is None:
            return ApprovalVerdict(number, ApprovalVerdictKind.NO_LABEL_EVENT)
        record = self.records.load_operator_approval(number)
        # Both halves: the recorded event, AND an event this engine's own
        # credential produced — a record can never vouch for someone else's
        # label, however it came to hold that id (#7763 review F3).
        if (
            record is not None
            and record.label_event_id == event.event_id
            and self.evidence.is_own_write(event)
        ):
            return ApprovalVerdict(
                number,
                ApprovalVerdictKind.CONTROL_CENTER,
                actor=event.actor_login,
                event_id=event.event_id,
            )
        if event.actor_is_bot:
            return ApprovalVerdict(
                number, ApprovalVerdictKind.BOT_ACTOR, event.actor_login, event.event_id
            )
        if self._role(event.actor_login, fresh=fresh) not in MAINTAINER_ROLES:
            return ApprovalVerdict(
                number,
                ApprovalVerdictKind.NOT_A_MAINTAINER,
                event.actor_login,
                event.event_id,
            )
        return ApprovalVerdict(
            number, ApprovalVerdictKind.MAINTAINER, event.actor_login, event.event_id
        )

    def is_maintainer(self, event: "LabelEvent") -> bool:
        """Whether *event* was a maintainer's act: a person, in a maintainer role."""
        return not event.actor_is_bot and self._role(event.actor_login, fresh=True) in MAINTAINER_ROLES

    def _role(self, login: str, *, fresh: bool) -> str | None:
        """A login's role; ``fresh`` reads it now (a just-demoted maintainer
        must not keep approving on a cached answer, #7763 review F2)."""
        now = self.clock()
        cached = self._roles.get(login)
        if not fresh and cached is not None and now - cached[0] < ROLE_CACHE_SECONDS:
            return cached[1]
        role = self.evidence.repository_role(login)
        self._roles[login] = (now, role)
        return role

    def verify_claims(self, issues: Iterable["Issue"]) -> dict[int, ApprovalVerdict]:
        """Verdicts for every observed item that claims approval.

        Items without ``approved`` cost nothing and are left out; observing
        them still drops any cached verdict, which is how removing
        ``approved`` revokes an approval the engine had verified.
        """
        latest = {issue.number: issue for issue in issues}
        verdicts: dict[int, ApprovalVerdict] = {}
        for number, issue in latest.items():
            verdict = self.verify(issue)
            if verdict.kind not in (
                ApprovalVerdictKind.NOT_CLAIMED,
                ApprovalVerdictKind.CLOSED,
            ):
                verdicts[number] = verdict
        return verdicts

    def supersede_verdicts(
        self, earlier: dict[int, ApprovalVerdict], issues: Sequence["Issue"]
    ) -> dict[int, ApprovalVerdict]:
        """*earlier* verdicts, replaced for every issue a newer read saw: one
        whose ``approved`` was removed in between has no verdict left, rather
        than the older approving one (#7763 review r20 F1)."""
        seen = {issue.number for issue in issues}
        kept = {number: verdict for number, verdict in earlier.items() if number not in seen}
        return kept | self.verify_claims(issues)

    def observe(self, issues: Iterable["Issue"]) -> None:
        """Drop cached approvals for items now observed without ``approved``.

        Free (cached index reads only): every tick feeds it the issues it
        already holds, so a removed ``approved`` label revokes admission on
        the next tick rather than at the next approval scan.
        """
        reopened: list[int] = []
        inactive = self.known_proposals() - self.indexed_proposals() - self.declined_numbers()
        for issue in issues:
            if issue.state != "open" or not proposal_label_state(issue.labels).claims_approval:
                self._verified.pop(issue.number, None)
            if issue.state == "open" and issue.number in inactive:
                reopened.append(issue.number)
        # A closed proposal seen open again is back in the scope's reads, so
        # its gate is restored however it was edited (#7763 review r18 F1).
        self.remember_proposals(reopened)

    def confirm(self, issue: "Issue | None") -> bool:
        """Apply-time consent: open, claimed, approved by a FRESH read, and
        never declined."""
        if issue is None or self.is_declined(issue.number):
            return False
        return self.verify(issue, fresh=True).approved

    # -- admission ---------------------------------------------------------

    def admits(self, issue: "Issue") -> bool:
        """The scheduler's approval rule for one item.

        Ordinary work is unaffected. A proposal is admitted only in the
        ``ADMITTED`` label state (the engine took ``awaiting-approval`` off)
        AND while this process holds a verified approval for it: labels alone
        never admit.
        """
        state = self.proposal_state_of(issue)
        if state is ProposalLabelState.NOT_A_PROPOSAL:
            return True
        return (
            state is ProposalLabelState.ADMITTED
            and issue.number in self._verified
            and not self.is_declined(issue.number)
        )

    def verified_numbers(self) -> frozenset[int]:
        return frozenset(self._verified)


def admits_without_evidence(issue: "Issue") -> bool:
    """Admission where no approval owner is wired (CLI audits, dry runs).

    Fails closed: with nothing able to verify an approval, no proposal is
    admitted, whatever its labels say.
    """
    return not proposal_state(issue.labels, issue.body).is_proposal


def unapproved_proposal_launch(
    issue_number: int,
    repository: "RepositoryHost",
    approvals: "TechLeadApprovals | None",
) -> str | None:
    """Why a session must not launch on *issue_number*, or None (#7763 review F2).

    The ONE launch-boundary consent check, asked by
    ``SessionLauncher.launch_issue_session`` for every issue session — planned
    launches and startup's resumption of partial work alike. The scheduler
    admits on the verified CACHE, which is only as fresh as the last tick's
    observation: an ``approved`` removed and re-applied by a bot between two
    ticks still looks verified there. So a launch re-reads the issue and, for
    a proposal, its latest ``approved`` event and the actor's role, all fresh.
    Ordinary issues cost one (ETag-cached) issue read. No owner: fail closed.
    """
    issue = repository.get_issue(issue_number)
    if issue is None:
        # Fail closed (#7763 review r5 F2): with no fresh read there is
        # nothing to judge a proposal's approval by.
        return f"#{issue_number} could not be read fresh at launch; not launched"
    state = (
        approvals.proposal_state_of(issue) if approvals is not None else proposal_state(issue.labels, issue.body)
    )
    if not state.is_proposal:
        return None
    if approvals is not None and approvals.confirm(issue):
        return None
    return (
        f"#{issue_number} is a tech-lead proposal without a maintainer's approval"
        " standing at launch; not launched"
    )


def plan_approval_settlements(
    issues: Sequence["Issue"],
    verdicts: dict[int, ApprovalVerdict],
    *,
    op_backed: Collection[int],
    declined: Collection[int] = frozenset(),
    known: Collection[int] = frozenset(),
) -> tuple[ApprovalSettlement, ...]:
    """The approval-label transitions one tick's observations call for.

    Read-free. Op-backed proposals are never ADMITTED: their approval executes
    the stored op, which closes the proposal with its outcome. Nor is a
    *declined* one reopened later (its op is gone; #7763 review r7 F2).
    Everything else a verified approval admits to the work queue.
    """
    latest = {issue.number: issue for issue in issues}
    settlements: list[ApprovalSettlement] = []
    for number in sorted(latest):
        issue = latest[number]
        if issue.state != "open":
            continue
        state = known_proposal_state(issue.labels, issue.body, known=number in known)
        if not state.is_proposal:
            continue
        verdict = verdicts.get(number)
        if verdict is not None and verdict.rejected_claim:
            settlements.append(
                ApprovalSettlement(number, ApprovalTransition.REJECT_CLAIM, verdict)
            )
            continue
        if state is ProposalLabelState.AWAITING and not _carries_gate(issue.labels):
            settlements.append(
                ApprovalSettlement(
                    number,
                    ApprovalTransition.RESTORE_WAITING,
                    ApprovalVerdict(number, ApprovalVerdictKind.NOT_CLAIMED),
                )
            )
            continue
        if (
            verdict is not None
            and verdict.approved
            and state is ProposalLabelState.APPROVAL_CLAIMED
            and number not in op_backed
            and number not in declined
        ):
            settlements.append(
                ApprovalSettlement(number, ApprovalTransition.ADMIT, verdict)
            )
    return tuple(settlements)


def _carries_gate(labels: Collection[str]) -> bool:
    """Both the provenance and the waiting label are present."""
    folded = {str(label).casefold() for label in labels}
    return all(label.casefold() in folded for label in GATED_PROPOSAL_LABELS)


__all__ = [
    "ROLE_CACHE_SECONDS",
    "TechLeadApprovals",
    "admits_without_evidence",
    "plan_approval_settlements",
    "unapproved_proposal_launch",
]
