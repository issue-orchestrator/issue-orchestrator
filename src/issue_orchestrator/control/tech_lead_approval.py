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
    proposal_label_state,
    proposal_state,
)

if TYPE_CHECKING:
    from ..ports.approval_evidence import (
        ApprovalEvidenceReader,
        OperatorApprovalRecords,
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
    clock: Callable[[], float] = time.monotonic
    _verified: dict[int, ApprovalVerdict] = field(default_factory=dict, init=False)
    _roles: dict[str, tuple[float, str | None]] = field(default_factory=dict, init=False)
    _scope: tuple["Issue", ...] = field(default=(), init=False)
    _scope_verdicts: dict[int, ApprovalVerdict] = field(default_factory=dict, init=False)

    # -- the last observed scope (read model) --------------------------------

    def record_scope(
        self, issues: Sequence["Issue"], verdicts: dict[int, ApprovalVerdict]
    ) -> None:
        """Remember the last complete approval-scope observation, for the UI.

        In-memory only: GitHub labels stay the truth, and a restart re-observes
        the scope on its first tick.
        """
        self._scope = tuple(issues)
        self._scope_verdicts = dict(verdicts)

    def forget_from_scope(self, issue_number: int) -> None:
        """Drop one item after a decline closed it, so the read model is current."""
        self._scope = tuple(issue for issue in self._scope if issue.number != issue_number)
        self._scope_verdicts.pop(issue_number, None)

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

    def observe(self, issues: Iterable["Issue"]) -> None:
        """Drop cached approvals for items now observed without ``approved``.

        Free (no reads): every tick feeds it the issues it already holds, so a
        removed ``approved`` label revokes admission on the next tick rather
        than at the next approval scan.
        """
        for issue in issues:
            if issue.state != "open" or not proposal_label_state(issue.labels).claims_approval:
                self._verified.pop(issue.number, None)

    def confirm(self, issue: "Issue | None") -> bool:
        """Apply-time consent: open, claimed, and approved by a FRESH read."""
        if issue is None:
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
        state = proposal_state(issue.labels, issue.body)
        if state is ProposalLabelState.NOT_A_PROPOSAL:
            return True
        return state is ProposalLabelState.ADMITTED and issue.number in self._verified

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
    if not proposal_state(issue.labels, issue.body).is_proposal:
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
) -> tuple[ApprovalSettlement, ...]:
    """The approval-label transitions one tick's observations call for.

    Read-free. Op-backed proposals are never ADMITTED: their approval executes
    the stored op, which closes the proposal with its outcome. Everything else
    a verified approval admits to the work queue.
    """
    latest = {issue.number: issue for issue in issues}
    settlements: list[ApprovalSettlement] = []
    for number in sorted(latest):
        issue = latest[number]
        if issue.state != "open":
            continue
        state = proposal_state(issue.labels, issue.body)
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
