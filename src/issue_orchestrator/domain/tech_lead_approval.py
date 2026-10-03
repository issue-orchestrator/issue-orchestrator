"""Positive, maintainer-applied approval of tech-lead proposals (#7763).

The label model has ONE owner, this module, and three labels with one meaning
each:

* :data:`TECH_LEAD_PROPOSAL_LABEL` — **provenance**, permanent: the tech lead
  filed this under ``propose`` authority, so it does nothing until approved.
* :data:`AWAITING_APPROVAL_LABEL` — **state**: still waiting on a maintainer.
  Blocking-class, so the scheduler never picks the item up.
* :data:`APPROVED_LABEL` — **approval**, a positive act. It counts only when a
  maintainer applied it (see :class:`ApprovalVerdict`); the engine's own
  identity, any bot, and agent sessions never approve.

Decline is closing the issue.

Approval used to be the REMOVAL of one ``proposed-tech-lead`` label, so
anything that stripped labels approved: an engine retry that clears every
blocking label, a reconcile, a bulk edit. Under this model the gate stays
closed for every item carrying the provenance label unless ``approved`` is
present, and an ``approved`` label is only evidence until the engine reads who
applied it. A strip of ``awaiting-approval`` alone therefore approves nothing.

This module is pure: label classification and the verdict vocabulary. The
reads (label events, repository roles) live behind
:mod:`~..ports.approval_evidence`, and the policy that combines them lives in
:mod:`~..control.tech_lead_approval`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Collection

TECH_LEAD_PROPOSAL_LABEL = "tech-lead-proposal"
AWAITING_APPROVAL_LABEL = "awaiting-approval"
APPROVED_LABEL = "approved"

#: What a gated filing carries: provenance plus the waiting state. Every
#: producer (act-level op proposals, ``create_issue`` follow-ups, promoted
#: findings) attaches exactly these, orchestrator-side, and nowhere else.
GATED_PROPOSAL_LABELS: tuple[str, ...] = (
    TECH_LEAD_PROPOSAL_LABEL,
    AWAITING_APPROVAL_LABEL,
)

#: Every label this model owns. No agent may propose any of them, and retry,
#: recovery shedding and bulk label clears leave all three alone: approval
#: state changes only through :mod:`~..control.tech_lead_approval`.
APPROVAL_MODEL_LABELS: tuple[str, ...] = (
    TECH_LEAD_PROPOSAL_LABEL,
    AWAITING_APPROVAL_LABEL,
    APPROVED_LABEL,
)

#: Repository roles that count as a maintainer. ``write`` does not: an
#: approval spends the operator's authority, which a contributor's push
#: access does not carry.
MAINTAINER_ROLES: frozenset[str] = frozenset({"admin", "maintain"})


#: Written into the body of every gated filing (#7763 review F1). Labels can
#: all be stripped by a bulk edit; the body survives a strip, so an issue the
#: tech lead filed under ``propose`` authority stays a proposal even with no
#: approval label left — and the engine restores its labels.
PROPOSAL_BODY_MARKER = "<!-- issue-orchestrator:tech-lead-proposal -->"


def with_proposal_marker(body: str) -> str:
    """*body* carrying the proposal marker exactly once."""
    if PROPOSAL_BODY_MARKER in body:
        return body
    return f"{body.rstrip()}\n\n{PROPOSAL_BODY_MARKER}\n"


def carries_proposal_marker(body: str | None) -> bool:
    return PROPOSAL_BODY_MARKER in (body or "")


#: The one way every proposal body and comment tells a human how to approve.
HOW_TO_APPROVE = (
    "Approve it with **Approve** in the Control Center's Approvals inbox, or"
    f" add the `{APPROVED_LABEL}` label yourself. Only a repository maintainer's"
    " approval counts; a label added by a bot, the engine or an agent session"
    " is removed. To decline, close this issue."
)


def _carries(labels: Collection[str], name: str) -> bool:
    """GitHub folds label case, so the model compares case-insensitively."""
    folded = name.casefold()
    return any(str(label).casefold() == folded for label in labels)


def is_approval_model_label(name: str) -> bool:
    """True iff *name* is one of the three labels this model owns."""
    folded = name.casefold()
    return any(folded == label.casefold() for label in APPROVAL_MODEL_LABELS)


class ProposalLabelState(StrEnum):
    """What an item's labels alone say about its approval.

    Labels are evidence, not authority: ``APPROVAL_CLAIMED`` and ``ADMITTED``
    still need a verified :class:`ApprovalVerdict` before anything executes or
    schedules. The classification is what every reader shares so none of them
    re-derives the gate from raw label names.
    """

    #: Neither provenance nor waiting state: ordinary work.
    NOT_A_PROPOSAL = "not_a_proposal"
    #: A proposal with no ``approved`` label. Inert.
    AWAITING = "awaiting_approval"
    #: ``approved`` is present but the engine has not admitted it yet
    #: (``awaiting-approval`` still on). Needs the actor check.
    APPROVAL_CLAIMED = "approval_claimed"
    #: ``approved`` present and the engine removed ``awaiting-approval``
    #: after verifying it. Schedulable only while the engine still holds a
    #: verified approval for it.
    ADMITTED = "admitted"

    @property
    def gate_closed(self) -> bool:
        """True when the labels alone already keep the item out of work."""
        return self in (ProposalLabelState.AWAITING, ProposalLabelState.APPROVAL_CLAIMED)

    @property
    def is_proposal(self) -> bool:
        return self is not ProposalLabelState.NOT_A_PROPOSAL

    @property
    def claims_approval(self) -> bool:
        """True when an ``approved`` label is present and needs verifying."""
        return self in (
            ProposalLabelState.APPROVAL_CLAIMED,
            ProposalLabelState.ADMITTED,
        )


def proposal_state(labels: Collection[str], body: str | None) -> ProposalLabelState:
    """Classify an issue: its labels, or its body marker when every label is gone.

    A filed proposal whose approval labels were ALL stripped is still a
    proposal awaiting approval (#7763 review F1): only a verified approval
    ever admits it.
    """
    state = proposal_label_state(labels)
    if state is ProposalLabelState.NOT_A_PROPOSAL and carries_proposal_marker(body):
        return ProposalLabelState.AWAITING
    return state


def proposal_label_state(labels: Collection[str]) -> ProposalLabelState:
    """Classify *labels* under the approval model.

    The ``awaiting-approval`` state label marks a proposal on its own, so a
    stripped provenance label cannot make a waiting item look like ordinary
    work; the provenance label marks one on its own, so a stripped state label
    cannot either. Only ``approved`` can move an item past ``AWAITING``.
    """
    provenance = _carries(labels, TECH_LEAD_PROPOSAL_LABEL)
    waiting = _carries(labels, AWAITING_APPROVAL_LABEL)
    if not (provenance or waiting):
        return ProposalLabelState.NOT_A_PROPOSAL
    if not _carries(labels, APPROVED_LABEL):
        return ProposalLabelState.AWAITING
    if waiting:
        return ProposalLabelState.APPROVAL_CLAIMED
    return ProposalLabelState.ADMITTED


def labels_named(labels: Collection[str], name: str) -> list[str]:
    """Every label in *labels* that is *name* (GitHub folds label case)."""
    folded = name.casefold()
    return [label for label in labels if str(label).casefold() == folded]


def missing_labels(labels: Collection[str], wanted: Collection[str]) -> list[str]:
    """The *wanted* labels *labels* lacks (case-insensitively), in order."""
    present = {str(label).casefold() for label in labels}
    return [label for label in wanted if label.casefold() not in present]


def gate_blocking_labels(labels: Collection[str]) -> list[str]:
    """The approval labels that keep an unapproved proposal blocked, when its
    gate is closed (empty otherwise): what names the block once its waiting
    label was stripped (#7763)."""
    gate_closed = proposal_label_state(labels).gate_closed
    return [label for label in labels if gate_closed and is_approval_model_label(label)]


def filed_proposal_numbers(labels: Collection[str], issue_number: int) -> list[int]:
    """``[issue_number]`` when an issue filed with *labels* is a proposal."""
    return [issue_number] if proposal_label_state(labels).is_proposal else []


def require_proposal_marker(labels: Collection[str], body: str, *, what: str) -> None:
    """A filing that carries approval labels must carry the body marker too, so
    stripping every approval label cannot turn it into ordinary work."""
    if proposal_label_state(labels).is_proposal and not carries_proposal_marker(body):
        raise ValueError(f"{what} carries approval labels but not the proposal body marker")


def require_gated_filing(labels: Collection[str], body: str, *, what: str) -> None:
    """A gated filing: provenance + waiting labels, no approval, and the marker."""
    if proposal_label_state(labels) is not ProposalLabelState.AWAITING:
        raise ValueError(
            f"{what} must carry the approval model's provenance and waiting"
            " labels, and no approval; filing it without them creates"
            " immediately schedulable work nobody approved"
        )
    if not carries_proposal_marker(body):
        raise ValueError(f"{what} must carry the proposal body marker")


def refuse_approval_labels(labels: Collection[str], *, what: str) -> None:
    """Only the tech lead's gated filings may carry the approval model's
    labels; anything else naming one is refused (#7763)."""
    if any(is_approval_model_label(str(label)) for label in labels):
        raise ValueError(f"{what} may not carry the tech-lead approval labels")


@dataclass(frozen=True, slots=True)
class LabelEvent:
    """The most recent ``labeled`` event for one label on one issue.

    ``actor_is_bot`` is GitHub's own account type (``Bot``), a ``[bot]`` login,
    or an event performed through a GitHub App: any of them is automation.
    """

    event_id: int
    actor_login: str
    actor_is_bot: bool
    created_at: str
    #: ``performed_via_github_app`` (id and client id), when an App made it.
    #: It is how the engine recognises an event its OWN write produced.
    app_id: str = ""
    app_client_id: str = ""

    def __post_init__(self) -> None:
        if type(self.event_id) is not int or self.event_id <= 0:
            raise ValueError("a label event needs GitHub's positive event id")
        if not self.actor_login:
            raise ValueError("a label event needs its actor's login")


class ApprovalVerdictKind(StrEnum):
    """Why an item is, or is not, approved."""

    #: A maintainer applied ``approved`` on GitHub.
    MAINTAINER = "maintainer"
    #: An operator approved in the Control Center; the engine applied the
    #: label and recorded that exact label event as the operator's act.
    CONTROL_CENTER = "control_center"
    #: Not a proposal, or no ``approved`` label.
    NOT_CLAIMED = "not_claimed"
    #: Closed: a closed proposal is declined or finished, never approvable.
    CLOSED = "closed"
    #: ``approved`` is on the issue but GitHub has no labeled event for it.
    NO_LABEL_EVENT = "no_label_event"
    #: The latest ``approved`` label came from a bot or GitHub App identity.
    BOT_ACTOR = "bot_actor"
    #: The latest ``approved`` label came from someone without a maintainer role.
    NOT_A_MAINTAINER = "not_a_maintainer"


_APPROVING_KINDS = frozenset({ApprovalVerdictKind.MAINTAINER, ApprovalVerdictKind.CONTROL_CENTER})

#: Verdicts that mean "somebody put ``approved`` on this, and it does not
#: count". The engine removes such a label and says why on the issue, so the
#: item reads as waiting and a later real approval is a fresh labeled event.
REJECTED_APPROVAL_KINDS = frozenset(
    {
        ApprovalVerdictKind.NO_LABEL_EVENT,
        ApprovalVerdictKind.BOT_ACTOR,
        ApprovalVerdictKind.NOT_A_MAINTAINER,
    }
)


@dataclass(frozen=True, slots=True)
class ApprovalVerdict:
    """The engine's answer to "did a maintainer approve this item?"."""

    issue_number: int
    kind: ApprovalVerdictKind
    actor: str = ""
    event_id: int = 0

    @property
    def approved(self) -> bool:
        return self.kind in _APPROVING_KINDS

    @property
    def rejected_claim(self) -> bool:
        """An ``approved`` label is present and does not count."""
        return self.kind in REJECTED_APPROVAL_KINDS

    def describe(self) -> str:
        match self.kind:
            case ApprovalVerdictKind.MAINTAINER:
                return f"approved by maintainer @{self.actor}"
            case ApprovalVerdictKind.CONTROL_CENTER:
                return "approved by the operator in the Control Center"
            case ApprovalVerdictKind.NOT_CLAIMED:
                return "no approved label"
            case ApprovalVerdictKind.CLOSED:
                return "closed"
            case ApprovalVerdictKind.NO_LABEL_EVENT:
                return "the approved label has no labeled event on record"
            case ApprovalVerdictKind.BOT_ACTOR:
                return f"the approved label was applied by automation (@{self.actor})"
            case ApprovalVerdictKind.NOT_A_MAINTAINER:
                return f"the approved label was applied by @{self.actor}, who is not a maintainer"


@dataclass(frozen=True, slots=True)
class OperatorApprovalRecord:
    """An approval given in the Control Center, bound to its label event.

    The Control Center writes ``approved`` with the engine's own (bot)
    credential, so the label event's actor cannot carry the operator's
    authority. The engine instead records the exact event id its write
    produced. Only that event counts: if the label is removed and re-added by
    anything else, the latest event is a different one and the record no
    longer applies.
    """

    issue_number: int
    label_event_id: int
    recorded_at: str

    def __post_init__(self) -> None:
        if type(self.issue_number) is not int or self.issue_number <= 0:
            raise ValueError("an operator approval needs a positive issue number")
        if type(self.label_event_id) is not int or self.label_event_id <= 0:
            raise ValueError("an operator approval needs its label event id")


class ApprovalTransition(StrEnum):
    """The label writes the approval owner makes, and only it."""

    #: A verified approval on a non-op item: remove ``awaiting-approval`` so
    #: the filed issue joins the work queue.
    ADMIT = "admit"
    #: An ``approved`` label that does not count: remove it and say why.
    REJECT_CLAIM = "reject_claim"
    #: A proposal with neither ``approved`` nor ``awaiting-approval``
    #: (something stripped the state label): put the state label back.
    RESTORE_WAITING = "restore_waiting"


@dataclass(frozen=True, slots=True)
class ApprovalSettlement:
    """One planned approval-label transition, decided read-only."""

    issue_number: int
    transition: ApprovalTransition
    verdict: ApprovalVerdict


__all__ = [
    "APPROVAL_MODEL_LABELS",
    "APPROVED_LABEL",
    "AWAITING_APPROVAL_LABEL",
    "ApprovalSettlement",
    "ApprovalTransition",
    "ApprovalVerdict",
    "ApprovalVerdictKind",
    "GATED_PROPOSAL_LABELS",
    "HOW_TO_APPROVE",
    "LabelEvent",
    "MAINTAINER_ROLES",
    "OperatorApprovalRecord",
    "PROPOSAL_BODY_MARKER",
    "ProposalLabelState",
    "REJECTED_APPROVAL_KINDS",
    "TECH_LEAD_PROPOSAL_LABEL",
    "carries_proposal_marker",
    "filed_proposal_numbers",
    "gate_blocking_labels",
    "is_approval_model_label",
    "labels_named",
    "missing_labels",
    "proposal_label_state",
    "proposal_state",
    "refuse_approval_labels",
    "require_gated_filing",
    "require_proposal_marker",
    "with_proposal_marker",
]
