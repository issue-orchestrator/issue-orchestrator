"""The applier boundary of external-PR adoption (#9499): its only writes.

Runs ONLY inside the ``ADVANCE_INTEGRATION`` applier handler
(:func:`~.integration_branch.apply_integration_step`), after
:func:`~.external_pr_adoption.adoption_step_refusal` re-checked the step on
fresh reads - the same applier IO boundary as ``action_applier.py``'s own label
and comment writes. Which labels to write is decided in the domain
(:func:`~..domain.external_pr_adoption.adoption_label_writes`).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ..domain.external_pr_adoption import (
    AdoptExternalPullRequest,
    ReportRefusedExternalPullRequest,
    adoption_label_writes,
    refusal_marker,
    render_refusal_comment,
)

if TYPE_CHECKING:
    from ..ports.repository_host import RepositoryHost
    from .label_manager import LabelManager


def write_adoption_step(
    step: "AdoptExternalPullRequest | ReportRefusedExternalPullRequest",
    host: "RepositoryHost",
    labels: "LabelManager",
) -> None:
    """Adopt the PR (review lane first, origin last), or tell it why it was refused."""
    if isinstance(step, ReportRefusedExternalPullRequest):
        if not host.issue_comment_marker_present(step.pr_number, refusal_marker(step.refusal)):
            host.add_comment(step.pr_number, render_refusal_comment(step))
        return
    writes = adoption_label_writes(
        host.get_issue_labels_fresh(step.pr_number), review_label=step.review_label,
        reviewed_label=labels.code_reviewed,
    )
    for name in writes.remove:
        host.remove_label(step.pr_number, name)
    for name in writes.add:
        host.add_label(step.pr_number, name)


__all__ = ["write_adoption_step"]
