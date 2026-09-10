"""Fresh target eligibility shared by approval and worker admission."""

from __future__ import annotations
from ..domain.scoped_rework import ReworkRequest
from ..ports.issue import Issue
from ..ports.pull_request_tracker import PRInfo
from .review_scope import extract_issue_number_from_pr, pr_fields_reference_issue


def rework_target_stale_reason(
    request: ReworkRequest, pr: PRInfo | None, issue: Issue | None
) -> str | None:
    target = request.target
    if pr is None or issue is None:
        return "target PR or linked issue no longer exists"
    if issue.key.scope() != target.repository:
        return "target repository no longer matches the approved repository"
    if extract_issue_number_from_pr(
        pr
    ) != target.issue_number or not pr_fields_reference_issue(
        branch=pr.branch,
        title="",
        body=pr.body,
        issue_numbers=[target.issue_number],
    ):
        return "PR no longer links the approved issue"
    if pr.state == "closed":
        return "PR was closed without merging"
    if pr.head_sha != target.head_sha:
        return "PR head changed; a fresh review is required"
    if pr.branch != target.branch:
        return "PR branch lineage changed"
    if pr.state not in {"open", "merged"}:
        return "PR state cannot be verified"
    if pr.state == "open" and issue.state != "open":
        return "linked issue is closed"
    return None
