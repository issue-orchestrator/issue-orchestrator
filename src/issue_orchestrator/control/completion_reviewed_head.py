"""io's approval of a PR, as ONE convergent transition (#9499).

The ``code-reviewed`` label survives a push, so it cannot say WHICH head io's
reviewer approved. Approving an adopted (external) PR therefore first makes
sure :func:`~..domain.external_pr_adoption.reviewed_head_receipt` stands on
the PR for the commit the reviewer's worktree was at - the head it actually
read, even if the PR moved while the review was completing - and only then
adds the approval label. The integration owner merges the PR only at a head
carrying such a receipt written under io's own credential; any other head
goes back to io's review.

The transition is idempotent and ordered: an exact receipt already written
under io's credential satisfies the receipt step (a retry never posts it
twice), and ANY failure - reading the PR, the receipt, writing either - halts
the completion's remaining actions, so the review label is never retired
without the approval label in its place. The PR then stays in io's review
lane and its next review converges on the same state.

The completion processor's approval boundary, extracted for its line budget:
it runs ONLY for the ``ADD_CODE_REVIEWED_LABEL`` completion action on a PR.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ..domain.external_pr_adoption import ORIGIN_EXTERNAL_LABEL, reviewed_head_receipt
from ..domain.models import RequestedAction
from ..ports.repository_host import RepositoryHostError

if TYPE_CHECKING:
    from .completion_ports import GitAdapter, LabelAdapter, PRAdapter

logger = logging.getLogger(__name__)

#: The completion action that writes io's approval onto a PR.
_APPROVAL = RequestedAction.ADD_CODE_REVIEWED_LABEL


def approve_pull_request(
    action: RequestedAction, worktree: Path, *, issue_number: int, pr_number: int, pr_adapter: "PRAdapter",
    git_adapter: "GitAdapter", labels: "LabelAdapter", approval_label: str, actions_taken: list[str],
    errors: list[str],
) -> bool | None:
    """Approve PR *pr_number*: io's review receipt first (an adopted PR), then the approval label.

    None when *action* is not the approval of a PR (the caller's own action
    applies); True once the PR is approved; False when the completion must
    halt, the review label still on the PR.
    """
    if action is not _APPROVAL or pr_number == issue_number:
        return None
    head: str | None = None
    try:
        pr = pr_adapter.get_pr(pr_number)
        if pr is None:
            raise RepositoryHostError(f"PR #{pr_number} could not be read before its approval")
        adopted = ORIGIN_EXTERNAL_LABEL in pr.labels
        if adopted:
            head = git_adapter.get_head_sha(worktree)
            if not head:
                raise RepositoryHostError(f"the review worktree {worktree} has no head commit")
            receipt = reviewed_head_receipt(pr_number, head)
            standing = pr_adapter.find_issue_comment_receipt(pr_number, body=receipt)
            if standing is None:
                pr_adapter.add_comment(pr_number, receipt)
        labels.add_label(pr_number, approval_label)
    except Exception as error:  # any failure halts: the review lane is never retired without the approval
        logger.error("Approval of PR #%d not written: %s", pr_number, error)
        errors.append(f"{action.value}: PR #{pr_number}'s approval not written: {error}")
        return False
    if head is not None:
        actions_taken.append(f"Recorded io's review of PR #{pr_number} at {head[:8]}")
    actions_taken.append(f"Added '{approval_label}' label to #{pr_number}")
    return True


__all__ = ["approve_pull_request"]
