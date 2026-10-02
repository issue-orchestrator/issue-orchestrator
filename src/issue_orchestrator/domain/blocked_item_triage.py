"""Blocked-item triage: every blocked item gets one class and one applied action (#7593).

On porchpin the tech lead diagnosed every blocked item and acted on none: an
agent's split question waited for days as a dangling question, a stale
dependency label went unexplained, and a maintainer decision was answered with
advice on the health-review anchor. A diagnosis, a case file or advice is not a
disposition. This module is the vocabulary that makes the disposition explicit.

Each health review is granted the blocked items that still need triage (the
:class:`TriageGrant`, recorded in its launch authority), and its decision must
cover every granted item with exactly ONE action carrying a ``triage_class``:

====================  =====================================  ====================
Class                 What the operator gets                 Action types
====================  =====================================  ====================
``operator_decision``  a concrete proposal to approve or      ``propose_decision``
                       decline (a split, a scope call)
``human_hand_over``    an explicit hand-over of human work    ``escalate_to_human``
``explained``          an explanation on the item (what it    ``post_comment``
                       waits on, an engine defect and its
                       tracking issue)
``remedy``             the engine acts on the item            an act-level action
====================  =====================================  ====================

The class is the agent's claim; the orchestrator checks it against the action
type here, the grant at completion, and records it on the charter decision with
the item's :func:`block_fingerprint` at launch. That record is the watermark: an
item whose blocking state is unchanged since a triage that took effect (or is
awaiting the operator) is not triaged again.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, cast

from .tech_lead_artifacts import TriageClass


#: The charter-record effects that mean a triage is in force: it took effect,
#: or the operator already answered it. A triage that failed, was refused,
#: withheld or parked, or has not linked a result, did not dispose of the item,
#: so the item is triaged again. ``awaiting_approval`` is in force only once its
#: proposal issue is filed (:attr:`PriorTriage.in_force`).
TRIAGE_IN_FORCE_EFFECTS = frozenset({"applied", "approved_applied", "declined"})

#: At most this many items are granted to one health review, oldest first: a
#: decision carries at most 20 actions, and a backlog drains over a few runs.
MAX_TRIAGE_ITEMS_PER_RUN = 8


def block_fingerprint(
    blocking_labels: Iterable[str], *, tech_lead_marker: bool, needs_human_label: str
) -> str:
    """What is blocking an item, as a stable comparable string.

    The casefolded, sorted blocking labels. The shared ``needs-human`` label is
    left out while the tech-lead hand-over marker is on the item: that block is
    the tech lead's own hand-over, so placing it is not a change that calls for
    another triage.
    """
    folded = {label.casefold() for label in blocking_labels}
    if tech_lead_marker:
        folded.discard(needs_human_label.casefold())
    return ",".join(sorted(folded))


@dataclass(frozen=True)
class TriageGrant:
    """One blocked item a health review must triage, as observed at launch."""

    issue_number: int
    fingerprint: str

    def __post_init__(self) -> None:
        number = cast(object, self.issue_number)
        if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
            raise ValueError(f"triage grant issue_number must be a positive int, got {number!r}")
        fingerprint = cast(object, self.fingerprint)
        if not isinstance(fingerprint, str):
            raise ValueError("triage grant fingerprint must be a string")

    def to_dict(self) -> dict[str, Any]:
        return {"issue_number": self.issue_number, "fingerprint": self.fingerprint}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "TriageGrant":
        return cls(issue_number=data["issue_number"], fingerprint=data["fingerprint"])


@dataclass(frozen=True)
class PriorTriage:
    """The latest triage recorded for an item, as the agenda shows it."""

    triage_class: TriageClass
    action_kind: str
    effect: str
    decided_at: str
    fingerprint: str
    #: The gated proposal it filed, once the issue exists.
    proposal_issue_number: int | None = None

    @property
    def in_force(self) -> bool:
        """The triage still disposes of the item: it took effect, the operator
        answered it, or its proposal is FILED and waiting on them. A record
        awaiting approval whose proposal issue never got filed did not."""
        if self.effect == "awaiting_approval":
            return self.proposal_issue_number is not None
        return self.effect in TRIAGE_IN_FORCE_EFFECTS

    def to_dict(self) -> dict[str, Any]:
        return {
            "triage_class": self.triage_class.value,
            "action_kind": self.action_kind,
            "effect": self.effect,
            "decided_at": self.decided_at,
            "fingerprint": self.fingerprint,
            "proposal_issue_number": self.proposal_issue_number,
        }


@dataclass(frozen=True)
class TriageAgendaItem:
    """One blocked item the tech lead must triage, with the facts to do it."""

    issue_number: int
    title: str
    labels: tuple[str, ...]
    blocking_labels: tuple[str, ...]
    needs_human_causes: tuple[str, ...]
    fingerprint: str
    #: The last question an agent asked about the item, when one was recorded.
    agent_question: str | None
    #: Why it is on the agenda: never triaged, or its block changed since.
    reason: str
    prior: PriorTriage | None

    @property
    def grant(self) -> TriageGrant:
        return TriageGrant(self.issue_number, self.fingerprint)

    def to_dict(self) -> dict[str, Any]:
        return {
            "issue_number": self.issue_number,
            "title": self.title,
            "labels": list(self.labels),
            "blocking_labels": list(self.blocking_labels),
            "needs_human_causes": list(self.needs_human_causes),
            "agent_question": self.agent_question,
            "reason": self.reason,
            "prior_triage": self.prior.to_dict() if self.prior is not None else None,
        }


@dataclass(frozen=True)
class TriageAgenda:
    """The blocked items one health review must triage, and what it may skip."""

    items: tuple[TriageAgendaItem, ...] = ()
    #: Blocked items whose triage is still in force (not granted this run).
    in_force: tuple[int, ...] = ()
    #: Blocked items left for a later run by the per-run cap.
    deferred: tuple[int, ...] = ()

    @property
    def grants(self) -> tuple[TriageGrant, ...]:
        return tuple(item.grant for item in self.items)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": 1,
            "rule": (
                "Every item below needs exactly ONE proposed action that targets it"
                " and carries a triage_class: operator_decision (propose_decision),"
                " human_hand_over (escalate_to_human), explained (post_comment on"
                " the item), or remedy (an act-level action on the item)."
                " A diagnosis on the anchor or a case file alone does not count."
            ),
            "items": [item.to_dict() for item in self.items],
            "triage_in_force": list(self.in_force),
            "deferred_to_a_later_run": list(self.deferred),
        }


BLOCKED_ITEM_TRIAGE_FILENAME = "blocked-item-triage.json"


def render_triage_instructions(agenda: TriageAgenda) -> str:
    """The triage duty, appended to a health review's prompt by the engine.

    Every repository runs its own tech-lead prompt, and the completion rejects
    a decision that leaves a granted item untriaged, so the engine states the
    duty itself rather than relying on each prompt file to learn about it.
    Empty when nothing is owed.
    """
    if not agenda.items:
        return ""
    lines = [
        "## Blocked-item triage (REQUIRED by the orchestrator, #7593)",
        "",
        "Each blocked item below needs exactly ONE proposed action in your",
        "tech-lead-decision.json that targets the item and carries a `triage_class`.",
        "A diagnosis on this review's anchor, a case file or a new issue alone does",
        "not dispose of an item, and a decision that leaves one untriaged is",
        "rejected. Facts per item (labels, needs-human causes, the agent's own",
        f"question, why it is owed a triage) are in `tech-lead-data/{BLOCKED_ITEM_TRIAGE_FILENAME}`.",
        "",
        "- `operator_decision` + `propose_decision`: the item waits on a decision",
        "  (an agent's question, a split, a scope call). Write the decision you",
        "  recommend as `title`, the question, your recommendation and its",
        "  consequences as `body`, and any issue the decision splits out as",
        "  `follow_up_issues` (`[{\"title\": ..., \"body\": ...}]`, at most 3). The operator",
        "  approves or declines it; approval retries the item, files the",
        "  follow-ups and posts the decision on the item for the next session.",
        "- `human_hand_over` + `escalate_to_human`: genuinely human work (a",
        "  credential, provisioning, a policy call no proposal can frame). Say",
        "  exactly what the human must do in `body`.",
        "- `explained` + `post_comment` on the item: say what it is waiting on and",
        "  who acts next. For an engine defect, name the defect and the open issue",
        "  that tracks it (file one with `create_issue` if none exists).",
        "- `remedy` + an act-level action on the item (`release_withheld_review`,",
        "  `recover_validated_work`, `kill_hung_session`, `reset_retry`), when the",
        "  engine can move it itself. A blocked item's PR is never reworked: the",
        "  engine refuses a blocked issue's rework, so `request_rework` is no remedy.",
        "",
        "Items owed a triage this run:",
    ]
    for item in agenda.items:
        blocking = ", ".join(item.blocking_labels) or "the tech-lead hand-over marker"
        lines.append(f"- #{item.issue_number} {item.title} (blocked by: {blocking}; {item.reason})")
        if item.agent_question:
            lines.append(f"  - the agent asked: {item.agent_question}")
    return "\n".join(lines) + "\n"
