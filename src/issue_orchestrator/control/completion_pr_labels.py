"""What an agent may ask to have labelled on its own PR (#6999 F2, #7592, #7678).

``pr_labels`` is the one label set that arrives from OUTSIDE the orchestrator's
own planning: an agent writes it into its completion record and the processor
applies it. That makes it untrusted input, and the shared ``needs-human`` block
is not among the things it may write directly. Applied by name it would create
a block with no cause recorded against it, which a later typed release then
takes away from whoever DID record one - the exact loss the shared-block owner
exists to prevent.

Naming the block there is still a request for a person, about the PR: the
agent published its work and asks for a decision before it merges. So the rule
is enforced twice, on purpose, and neither is redundant:

* at the DOOR, by :func:`route_reserved_pr_labels`. Every path that turns a
  completion record into publication work - the live completion, a manual
  retry, and recovery of retained validated work - passes the record through
  it first. The reserved labels leave ``pr_labels`` and become the typed
  ``HOLD_MERGE_FOR_HUMAN`` request, which :func:`apply_pr_labels` gives the
  shared-block owner as a MERGE-scoped hold on the PR once its number is known
  (:mod:`.human_gates`): review and rework proceed, only the merge waits.
  #7595 sent it to the ISSUE instead, which turned a question about the merge
  into a block on the work (porchpin#364, PR #379);
* at the WRITE, by the governed label capability, which refuses the value.

They consult different objects (the block owner, then the label capability), so
a composition can wire one without the other. When that happens the write-side
refusal FAILS the completion rather than skipping the entry: a request for a
human that is silently dropped is one nothing downstream can see.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Protocol

from ..domain.models import RequestedAction
from .completion_types import ERROR_PREFIX_GOVERNED_LABEL
from .governed_label_set import GovernedLabelError
from .human_gates import merge_decision_request

if TYPE_CHECKING:
    from ..domain.human_block import BlockOutcome
    from ..domain.models import CompletionRecord
    from .needs_human_block import SharedNeedsHumanBlock

logger = logging.getLogger(__name__)

#: PR numbers the E2E dry run invents. They exist nowhere, so labelling them
#: would be a call against a PR GitHub has never heard of.
_DRY_RUN_PR_NUMBERS = range(90000, 100000)


class _LabelWriter(Protocol):
    def add_label(self, issue_number: int, label: str) -> None: ...


def route_reserved_pr_labels(
    record: "CompletionRecord", block: "SharedNeedsHumanBlock"
) -> "CompletionRecord":
    """The door: this record with its human-block request typed as a merge hold.

    Asks the OWNER whether it governs each label rather than comparing against
    a hard-coded name, so a repo that configures a different shared block is
    governed just the same. A record naming no governed label is returned as
    is.
    """
    reserved = [label for label in (record.pr_labels or ()) if block.owns(label)]
    if not reserved:
        return record
    kept = [label for label in (record.pr_labels or ()) if not block.owns(label)]
    actions = list(record.requested_actions)
    if RequestedAction.HOLD_MERGE_FOR_HUMAN not in actions:
        actions.append(RequestedAction.HOLD_MERGE_FOR_HUMAN)
    logger.warning(
        "[COMPLETION] pr_labels names the reserved shared block %s: routing it "
        "to the block owner as a merge hold on the PR (#7678).",
        reserved,
    )
    return replace(record, pr_labels=kept or None, requested_actions=actions)


def requests_human_block(record: "CompletionRecord") -> bool:
    """Whether the record asks for the shared block on its issue."""
    return RequestedAction.ADD_NEEDS_HUMAN_LABEL in record.requested_actions


def requests_merge_hold(record: "CompletionRecord") -> bool:
    """Whether the record asks a person to decide before its PR merges (#7678)."""
    return RequestedAction.HOLD_MERGE_FOR_HUMAN in record.requested_actions


def acquire_merge_hold(block: "SharedNeedsHumanBlock", pr_number: int) -> "BlockOutcome":
    """The agent's merge hold on its PR, through the block owner (idempotent)."""
    return block.acquire(merge_decision_request(
        pr_number, "agent asked a person to decide before this PR merges"
    ))


def apply_pr_labels(
    *,
    pr_number: int,
    record: "CompletionRecord",
    labels: _LabelWriter,
    block: "SharedNeedsHumanBlock",
    actions_taken: list[str],
    errors: list[str],
) -> bool:
    """Apply the record's extra PR labels and its merge hold. False when one
    was REFUSED, or the hold did not commit.

    A refusal appends a ``governed_label`` error, which forces the completion
    to fail with no "but the push worked" escape - see
    :mod:`.completion_result_artifacts`.
    """
    if pr_number in _DRY_RUN_PR_NUMBERS:
        logger.info(
            "[E2E_DRY_RUN] Skipping PR label addition for fake PR #%d", pr_number
        )
        return True
    if not _hold_merge(pr_number, record, block, actions_taken, errors):
        return False
    if not record.pr_labels:
        return True
    applied: list[str] = []
    refused: str | None = None
    for label in record.pr_labels:
        try:
            labels.add_label(pr_number, label)
        except GovernedLabelError:
            refused = (
                f"{ERROR_PREFIX_GOVERNED_LABEL}: pr_labels entry {label!r} is the "
                f"shared needs-human block, which is not the agent's to apply on "
                f"PR #{pr_number}; use the needs_human completion outcome"
            )
            logger.error("[COMPLETION] %s", refused)
            errors.append(refused)
            break
        applied.append(label)
        logger.info("Added label '%s' to PR #%d", label, pr_number)
    if applied:
        actions_taken.append(f"Added labels to PR: {applied}")
    return refused is None


def _hold_merge(
    pr_number: int,
    record: "CompletionRecord",
    block: "SharedNeedsHumanBlock",
    actions_taken: list[str],
    errors: list[str],
) -> bool:
    """The agent's ``HOLD_MERGE_FOR_HUMAN``, as a merge-scoped hold on the PR."""
    if not requests_merge_hold(record):
        return True
    outcome = acquire_merge_hold(block, pr_number)
    if outcome.committed:
        actions_taken.append(f"Held PR #{pr_number}'s merge for a person")
        return True
    error = f"merge hold for a person on PR #{pr_number} did not commit ({outcome.value})"
    logger.error("[COMPLETION] %s", error)
    errors.append(error)
    return False


__all__ = [
    "acquire_merge_hold",
    "apply_pr_labels",
    "requests_human_block",
    "requests_merge_hold",
    "route_reserved_pr_labels",
]
