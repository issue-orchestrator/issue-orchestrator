"""io's review approval of an adopted PR binds to the head it reviewed (#9499).

The ``code-reviewed`` label survives a push, so it cannot say WHICH head io's
reviewer approved. When a review session approves an adopted (external) PR,
the completion first leaves :func:`~..domain.external_pr_adoption.reviewed_head_receipt`
for the commit the reviewer's worktree was at - the head it actually read,
even if the PR moved while the review was completing - and only then adds the
approval label. The integration owner merges the PR only at a head carrying
such a receipt written under io's own credential; any other head goes back to
io's review.

The completion processor's approval boundary, extracted for its line budget:
it runs ONLY from the ``ADD_CODE_REVIEWED_LABEL`` completion action, the same
IO boundary as the label write it precedes.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ..domain.external_pr_adoption import ORIGIN_EXTERNAL_LABEL, reviewed_head_receipt
from ..domain.models import RequestedAction
from ..ports.repository_host import RepositoryHostError

if TYPE_CHECKING:
    from .completion_ports import GitAdapter, PRAdapter

logger = logging.getLogger(__name__)

#: The completion action that writes io's approval onto a PR.
_APPROVAL = RequestedAction.ADD_CODE_REVIEWED_LABEL


def record_reviewed_head(
    action: RequestedAction, worktree: Path, *, issue_number: int, pr_number: int, pr_adapter: "PRAdapter",
    git_adapter: "GitAdapter", actions_taken: list[str], errors: list[str],
) -> bool:
    """Leave io's review receipt before an adopted PR's approval label; False halts the completion.

    Anything but the approval of a PR (not an issue) passes untouched, and so
    does io's own PR: its approval label is all its lanes read. A receipt that
    cannot be written halts the remaining actions, so the PR keeps its review
    label and is reviewed again rather than left approved with no receipt.
    """
    if action is not _APPROVAL or pr_number == issue_number:
        return True
    try:
        pr = pr_adapter.get_pr(pr_number)
        if pr is None:
            raise RepositoryHostError(f"PR #{pr_number} could not be read before its approval")
        adopted = ORIGIN_EXTERNAL_LABEL in pr.labels
        if not adopted:
            return True
        head = git_adapter.get_head_sha(worktree)
        if not head:
            raise RepositoryHostError(f"the review worktree {worktree} has no head commit")
        pr_adapter.add_comment(pr_number, reviewed_head_receipt(pr_number, head))
    except (RepositoryHostError, ValueError) as error:
        logger.error("Approval of adopted PR #%d not recorded: %s", pr_number, error)
        errors.append(f"{action.value}: io's review receipt for PR #{pr_number} not written: {error}")
        return False
    actions_taken.append(f"Recorded io's review of PR #{pr_number} at {head[:8]}")
    return True


__all__ = ["record_reviewed_head"]
