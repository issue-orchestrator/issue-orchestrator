"""What an agent may ask to have labelled on its own PR (#6999 F2, #7592).

``pr_labels`` is the one label set that arrives from OUTSIDE the orchestrator's
own planning: an agent writes it into its completion record and the processor
applies it. That makes it untrusted input, and the shared ``needs-human`` block
is not among the things it may put on a PR. Applied there it would create a
block with no cause recorded against it, which a later typed release then takes
away from whoever DID record one - the exact loss the shared-block owner exists
to prevent.

Naming the block there is still a request for a human, and dropping it would be
a request nothing downstream can see. So the rule is enforced twice, on
purpose, and neither is redundant:

* at the DOOR, by :func:`route_reserved_pr_labels`. Every path that turns a
  completion record into publication work - the live completion, a manual
  retry, and recovery of retained validated work - passes the record through
  it first. The reserved labels leave ``pr_labels`` and become the typed
  ``ADD_NEEDS_HUMAN_LABEL`` request, which the shared-block owner applies to
  the ISSUE with the agent's cause recorded. The work itself still publishes.
  Refusing the record here instead (#6999 F2 round 5) stranded retained work
  for good: recovery has no agent to correct the record, so the same refusal
  came back on every pass (porchpin #364);
* at the WRITE, by the governed label capability, which refuses the value.

They consult different objects (the block owner, then the label capability), so
a composition can wire one without the other. When that happens the write-side
refusal FAILS the completion rather than skipping the entry: a request for a
human block that is silently dropped is one nothing downstream can see.
"""

from __future__ import annotations

import logging
from dataclasses import replace
from typing import TYPE_CHECKING, Protocol

from ..domain.models import RequestedAction
from .completion_types import ERROR_PREFIX_GOVERNED_LABEL
from .governed_label_set import GovernedLabelError

if TYPE_CHECKING:
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
    """The door: this record with its human-block request moved off the PR.

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
    if RequestedAction.ADD_NEEDS_HUMAN_LABEL not in actions:
        actions.append(RequestedAction.ADD_NEEDS_HUMAN_LABEL)
    logger.warning(
        "[COMPLETION] pr_labels names the reserved shared block %s: it is never "
        "applied to a PR. Routing the request to the issue through the block "
        "owner instead.",
        reserved,
    )
    return replace(record, pr_labels=kept or None, requested_actions=actions)


def requests_human_block(record: "CompletionRecord") -> bool:
    """Whether the record asks for the shared block on its issue."""
    return RequestedAction.ADD_NEEDS_HUMAN_LABEL in record.requested_actions


def apply_pr_labels(
    *,
    pr_number: int,
    record: "CompletionRecord",
    labels: _LabelWriter,
    actions_taken: list[str],
    errors: list[str],
) -> bool:
    """Apply the record's extra PR labels. False when one was REFUSED.

    A refusal appends a ``governed_label`` error, which forces the completion
    to fail with no "but the push worked" escape - see
    :mod:`.completion_result_artifacts`.
    """
    if not record.pr_labels:
        return True
    if pr_number in _DRY_RUN_PR_NUMBERS:
        logger.info(
            "[E2E_DRY_RUN] Skipping PR label addition for fake PR #%d", pr_number
        )
        return True

    applied: list[str] = []
    refused: str | None = None
    for label in record.pr_labels:
        try:
            labels.add_label(pr_number, label)
        except GovernedLabelError as exc:
            refused = (
                f"{ERROR_PREFIX_GOVERNED_LABEL}: pr_labels entry {label!r} is not"
                f" the agent's to apply on PR #{pr_number}; it is owned by"
                f" {exc.owner} (for a human, use the needs_human completion outcome)"
            )
            logger.error("[COMPLETION] %s", refused)
            errors.append(refused)
            break
        applied.append(label)
        logger.info("Added label '%s' to PR #%d", label, pr_number)
    if applied:
        actions_taken.append(f"Added labels to PR: {applied}")
    return refused is None


__all__ = ["apply_pr_labels", "requests_human_block", "route_reserved_pr_labels"]
