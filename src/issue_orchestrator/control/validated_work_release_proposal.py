"""What a ``release_validated_work`` proposal shows the approving operator (#9092).

An approval is only informed if the operator can read exactly what it
releases: every record, its validated head and branch, the evidence and
observation revision the approval is bound to, and the PR said to have
rebuilt the work. Approving runs exactly those facts; any that moved since
are refused with no write.
"""

from __future__ import annotations

from ..domain.validated_work_release import ValidatedWorkRelease


def release_proposal_section(release: ValidatedWorkRelease) -> str:
    """The table rows and consequence text a release proposal adds."""
    records = "".join(
        f"| Record | `{item.record_id}`: head `{item.validated_head_sha}` on"
        f" `{item.branch_name}`, evidence `{item.evidence_id}` revision"
        f" `{item.observation_revision}` |\n"
        for item in release.authorities
    )
    return (
        f"| Superseding PR | #{release.superseding_pr_number} (must be a merged PR"
        f" of #{release.issue_number} in `{release.repo_slug}` when approved) |\n"
        f"{records}"
        "| Predicted effects | Each record above is resolved ABANDONED by the"
        " approving operator, naming the superseding PR; the issue's"
        " recovery-pending block is reprojected. All or none: a record whose"
        " evidence or observations moved since this proposal refuses the whole"
        " release with no write. Nothing proves the work survived; approve only"
        " if the PR rebuilt it. |\n"
    )
