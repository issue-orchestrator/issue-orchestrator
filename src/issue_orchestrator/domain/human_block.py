"""Typed provenance and outcomes for the shared human-attention block."""

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from enum import Enum



class NeedsHumanCause(Enum):
    """One durable, independently recorded cause of the shared block.

    Enumerated rather than passed as free text: every owner that may remove the
    label has to be able to name the ones it is NOT, and a cause added without
    an entry here would simply be invisible to the others.
    """

    #: A tech-lead investigation that exhausted its bounded launch budget.
    #: Recorded as the tech-lead marker label on the issue.
    TECH_LEAD_ESCALATION = "tech_lead_escalation"
    #: A run whose pending-work claim could not be read or rebuilt. Recorded as
    #: a non-releasing row in the quarantine ledger.
    CLAIM_QUARANTINE = "claim_quarantine"
    #: An AGENT asked for it, through ``coding-done needs_human`` /
    #: ``reviewer-done``. Its own cause because it is the one assertion that
    #: arrives from outside the orchestrator's own planning, on the completion
    #: path rather than through an action (#6999 F2 round 3).
    AGENT_COMPLETION = "agent_completion"
    #: A PR escalated out of the merge/awaiting-merge lifecycle. Its own cause
    #: because it is the one SESSION-side assertion that is targetedly
    #: released again - by the post-publish "now reworkable" clear - so sharing
    #: a token with anything else would let that clear erase another block.
    MERGE_ESCALATION = "merge_escalation"
    #: Every other orchestrator escalation the planners raise: a session that
    #: ended without a completion record, publish failures past their bound, an
    #: invalid completion record, a stuck sweep, a failed rework worktree, a
    #: retrospective review, an uncommitted mandated reset. They share ONE token
    #: because none of them is ever released on its own terms: each ends when a
    #: human clears the label, or when an operator/terminal force-clear ends
    #: every cause at once. A lifecycle that gains a targeted release must take
    #: its own cause rather than joining this one. A tech lead's
    #: ``resolve_block`` (#7658) is not such a lifecycle: it decides the whole
    #: token in the human's stead, as the operator's own clear would.
    SESSION_LIFECYCLE = "session_lifecycle"
    #: Independent failed validated-work records, projected by disposition.
    VALIDATED_WORK_DISPOSITION = "validated_work_disposition"
    #: A person must decide before a PR MERGES (#7678): an agent asked for a
    #: human beside its published work (``pr_labels: [needs-human]``), so the
    #: question is about the PR, not the work. Recorded against the PR. It is
    #: the one MERGE-scoped cause: review, rework and conflict rework proceed,
    #: and only the merge waits. Released by a person, never by a lifecycle -
    #: so the post-publish "now reworkable" clear cannot take it off.
    MERGE_DECISION = "merge_decision"
    #: An action the liveness owner parked (#7350): it failed permanently,
    #: needs a person, or spent its retry budget with unchanged facts. Its own
    #: token because it IS released on its own terms - the parked action
    #: succeeding - and that release must not erase any other lifecycle's block.
    ACTION_LIVENESS = "action_liveness"

    @property
    def scope(self) -> "HumanHoldScope":
        """What this cause holds while it stands (#7678); see :class:`HumanHoldScope`."""
        return HumanHoldScope.MERGE if self is NeedsHumanCause.MERGE_DECISION else HumanHoldScope.WORK

    @property
    def is_row_backed(self) -> bool:
        """The block owner records this cause as a row of the block's generation.

        The other two keep their provenance in their own lifecycles: the
        tech-lead marker label and the quarantine ledger.

        A row-backed withdrawal may take the label off ONLY while its row is
        recorded on the generation standing now (#8774). A release can arrive
        long after its acquisition: a liveness debt is replayed later (#7350),
        an agent's question is answered days on. By then a person may have
        cleared the block and put a new one on by hand, which ends every cause
        of the old generation, so a release whose row is gone must leave that
        person's label alone.
        """
        return self not in {NeedsHumanCause.TECH_LEAD_ESCALATION, NeedsHumanCause.CLAIM_QUARANTINE}

    def matches_key(self, key: str) -> bool:
        if self is NeedsHumanCause.VALIDATED_WORK_DISPOSITION:
            return key.startswith(f"{self.value}:")
        return key == self.value


class HumanHoldScope(Enum):
    """What a ``needs-human`` holds on the number it is on (#7678).

    One label, two meanings, typed by the causes recorded against it:

    * ``WORK``: the item's work waits on a person (an agent's pre-work
      question, the engine giving up, an escalation). Nothing proceeds: no
      launch, review or rework, and no merge.
    * ``MERGE``: only the PR's merge waits on a person. Review, rework and
      conflict rework proceed.

    A label is MERGE-scoped only when every cause recorded against it is; a
    label with no recorded cause (put on by hand) or an unreadable record is
    WORK, the scope that holds the most.
    """

    WORK = "work"
    MERGE = "merge"


def hold_scope(causes: frozenset[NeedsHumanCause]) -> HumanHoldScope:
    """The scope of a ``needs-human`` held by ``causes`` (see :class:`HumanHoldScope`)."""
    if causes and all(cause.scope is HumanHoldScope.MERGE for cause in causes):
        return HumanHoldScope.MERGE
    return HumanHoldScope.WORK


def needs_human_hold(
    labels: Iterable[str],
    *,
    needs_human: str,
    handover_marker: str,
    causes: Callable[[], frozenset[NeedsHumanCause]],
) -> HumanHoldScope | None:
    """What the shared label in ``labels`` holds; None when it is absent.

    A tech-lead hand-over marker beside it is WORK (the hand-over holds the
    item whatever else is recorded); otherwise the recorded ``causes`` decide
    (:func:`hold_scope`), read only when the label is there.
    """
    folded = {label.casefold() for label in labels}
    if needs_human.casefold() not in folded:
        return None
    if handover_marker.casefold() in folded:
        return HumanHoldScope.WORK
    return hold_scope(causes())


@dataclass(frozen=True, slots=True)
class ValidatedWorkBlockSource:
    """The retained disposition record whose failure requires attention."""

    record_id: str

    def __post_init__(self) -> None:
        # Imported here: the decision artifact types this module's causes
        # (block_resolution), and validated_work reaches back to it via models.
        from .validated_work import require_text

        require_text(self.record_id, "record_id")


@dataclass(frozen=True, slots=True)
class NeedsHumanGeneration:
    """One generation (episode) of the shared block, as its owner records it.

    ``episode`` is ``"<opened_at>#<n>"`` (#8688): comparable for equality,
    readable by a person, and what a triage fingerprint carries. ``hand_over``
    is True only for the generation the tech lead's hand-over opened when it
    put its marker and this label on together (#8112): the block that
    hand-over placed, not one it found. A generation adopted from GitHub's
    event (the label taken off and put back, or re-dated from the marker,
    outside its owner) or opened by any other acquisition never is.
    """

    episode: str
    hand_over: bool


@dataclass(frozen=True, slots=True)
class HumanBlockRequest:
    """One lifecycle asserting or withdrawing the shared block (#6999 F2 r3).

    ``target`` is the number ACTUALLY mutated, which is not always an issue: a
    merge escalation blocks the PR. Carrying it explicitly is what stops a
    caller recording provenance against an issue while labelling a pull
    request, after which no remover can find the cause it is standing on.
    """

    target: int
    cause: NeedsHumanCause
    reason: str
    source: ValidatedWorkBlockSource | None = None
    #: The tech lead's hand-over is putting its marker and this label on
    #: together (#8112): a generation this acquisition opens is the block that
    #: hand-over placed (:attr:`NeedsHumanGeneration.hand_over`).
    hand_over: bool = False

    def __post_init__(self) -> None:
        scoped = self.cause is NeedsHumanCause.VALIDATED_WORK_DISPOSITION
        if scoped != isinstance(self.source, ValidatedWorkBlockSource):
            raise ValueError("validated-work causes require exactly one record source")
        if not scoped and self.source is not None:
            raise ValueError("ordinary causes cannot carry a validated-work source")
        if self.hand_over and self.cause is not NeedsHumanCause.TECH_LEAD_ESCALATION:
            raise ValueError("only the tech lead's escalation hands an item over")

    @property
    def cause_key(self) -> str:
        if self.source is not None:
            return f"{self.cause.value}:{self.source.record_id}"
        return self.cause.value


class BlockOutcome(Enum):
    """What a command did to the shared label."""

    #: The label is on the target and this cause is recorded against it.
    HELD = "held"
    #: This cause is withdrawn and the label came off with it.
    CLEARED = "cleared"
    #: This cause is withdrawn, but the label stays: another cause needs it.
    HELD_BY_ANOTHER_CAUSE = "held_by_another_cause"
    #: The label write did not commit. The caller retries; nothing is assumed.
    FAILED = "failed"
    #: No owner governs this label in this composition, so NOTHING happened and
    #: the caller must do its own write. Distinct from ``FAILED`` on purpose: a
    #: null owner that reported success turned real mutations into silent
    #: no-ops, which is a worse bug than the bypass it was closing.
    UNGOVERNED = "ungoverned"

    @property
    def committed(self) -> bool:
        return self in {BlockOutcome.HELD, BlockOutcome.CLEARED}


@dataclass(frozen=True, slots=True)
class ResolutionOutcome:
    """What a resolution's discharge did (#7658): its outcome, and whether
    any write was attempted. Only the owner knows the second: a FAILED outcome
    after an attempted removal may have taken the label off anyway, and the
    write-ahead record of the discharge must then stay begun."""

    outcome: BlockOutcome
    mutation_attempted: bool
