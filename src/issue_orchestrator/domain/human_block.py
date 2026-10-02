"""Typed provenance and outcomes for the shared human-attention block."""

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
    #: An action the liveness owner parked (#7350): it failed permanently,
    #: needs a person, or spent its retry budget with unchanged facts. Its own
    #: token because it IS released on its own terms - the parked action
    #: succeeding - and that release must not erase any other lifecycle's block.
    ACTION_LIVENESS = "action_liveness"

    @property
    def releases_only_its_recorded_block(self) -> bool:
        """A withdrawal of this cause may take the label off ONLY while its row is recorded.

        The liveness owner's releases are durable debts replayed later (#7350).
        By then a force-clear may have ended the block, and a person may have
        put a new one on: a replayed withdrawal whose cause is no longer
        recorded must leave that label alone.
        """
        return self is NeedsHumanCause.ACTION_LIVENESS

    def matches_key(self, key: str) -> bool:
        if self is NeedsHumanCause.VALIDATED_WORK_DISPOSITION:
            return key.startswith(f"{self.value}:")
        return key == self.value


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

    def __post_init__(self) -> None:
        scoped = self.cause is NeedsHumanCause.VALIDATED_WORK_DISPOSITION
        if scoped != isinstance(self.source, ValidatedWorkBlockSource):
            raise ValueError("validated-work causes require exactly one record source")
        if not scoped and self.source is not None:
            raise ValueError("ordinary causes cannot carry a validated-work source")

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
