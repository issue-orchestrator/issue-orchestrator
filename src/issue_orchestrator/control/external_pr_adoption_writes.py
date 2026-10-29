"""The applier boundary of external-PR adoption (#9499): its only writes.

Runs ONLY inside the ``ADVANCE_INTEGRATION`` applier handler
(:func:`~.integration_branch.apply_integration_step`), after
:func:`~.external_pr_adoption.external_step_refusal` re-judged the step on
fresh reads - the same applier IO boundary as ``action_applier.py``'s own label
and comment writes. Which labels to write is decided in the domain
(:func:`~..domain.external_pr_adoption.adoption_label_writes`,
:func:`~..domain.external_pr_adoption.return_to_review_label_writes`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..domain.external_pr_adoption import (
    AdoptExternalPullRequest,
    PullRequestLabelWrites,
    ReportRefusedExternalPullRequest,
    ReturnExternalPullRequestToReview,
    adoption_label_writes,
    refusal_marker,
    render_refusal_comment,
    return_to_review_label_writes,
)

if TYPE_CHECKING:
    from ..ports.repository_host import RepositoryHost
    from .external_pr_adoption import ExternalStep
    from .label_manager import LabelManager


def write_external_step(step: "ExternalStep", host: "RepositoryHost", labels: "LabelManager") -> None:
    """Adopt the PR (review lane first, origin last), tell it why it was
    refused, or send it back to io's review (approvals off, review lane on)."""
    if isinstance(step, ReportRefusedExternalPullRequest):
        if not host.issue_comment_marker_present(step.pr_number, refusal_marker(step)):
            host.add_comment(step.pr_number, render_refusal_comment(step))
        return
    current = host.get_issue_labels_fresh(step.pr_number)
    if isinstance(step, AdoptExternalPullRequest):
        writes = adoption_label_writes(
            current, review_label=step.review_label, approvals=(labels.code_reviewed, labels.tech_lead_reviewed),
        )
    else:
        assert isinstance(step, ReturnExternalPullRequestToReview)
        writes = return_to_review_label_writes(
            current, review_label=step.review_label, reviewed_label=labels.code_reviewed,
            other_approvals=(labels.tech_lead_reviewed,),
        )
    _write_labels(step.pr_number, writes, host)


def _write_labels(pr_number: int, writes: PullRequestLabelWrites, host: "RepositoryHost") -> None:
    """In the step's order: a failure leaves a prefix the owner plans the step from again."""
    for write in writes.writes:
        if write.add:
            host.add_label(pr_number, write.label)
        else:
            host.remove_label(pr_number, write.label)


__all__ = ["write_external_step"]
