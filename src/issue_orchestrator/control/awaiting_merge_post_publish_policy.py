"""Post-publish merge-readiness policy for awaiting-merge PRs."""

from __future__ import annotations

from typing import TYPE_CHECKING, Literal

from ..domain.models import (
    DiscoveredAwaitingMergeEscalation,
    PostPublishEscalationKind,
)

if TYPE_CHECKING:
    from ..ports.pull_request_tracker import PRInfo
    from .label_manager import LabelManager


POST_PUBLISH_VALIDATION_COMMENT_MARKER = "<!-- io:post-publish-validation -->"

# Discovered-rework source tag for post-publish (and merge-queue-failure)
# reworks. Shared by the awaiting-merge reconciler and the merge queue
# coordinator so the rework launcher treats both the same way.
POST_PUBLISH_VALIDATION_SOURCE = "post_publish_validation"

# Result of classifying GitHub merge readiness after reviewer approval.
# `mergeable_state` says merge readiness; `status_check_rollup` says check
# truth. Combining them disambiguates "checks still running" (transient; wait)
# from "a check actually failed" (real; rework).
PostApprovalAction = Literal[
    "READY",
    "WAIT_FOR_CHECKS",
    "REWORK_CONFLICT",
    "REWORK_BEHIND",
    "REWORK_CHECK_FAILED",
    "BLOCKED_TERMINAL",
    "UNKNOWN",
]

# Actions that require sending the PR back to a coder agent.
REWORK_ACTIONS: frozenset[PostApprovalAction] = frozenset(
    {"REWORK_CONFLICT", "REWORK_BEHIND", "REWORK_CHECK_FAILED"}
)


def normalized_state(state: str | None) -> str:
    return (state or "").strip().lower()


def classify_post_approval_state(pr: PRInfo) -> PostApprovalAction:
    """Decide what to do with a reviewer-approved PR based on GitHub state.

    Pure function: no I/O, no clock, so callers can unit-test the dispatch
    matrix exhaustively.
    """
    state = normalized_state(pr.mergeable_state)
    rollup = pr.status_check_rollup
    if state == "clean":
        return "READY"
    if state == "dirty":
        return "REWORK_CONFLICT"
    if state == "behind":
        return "REWORK_BEHIND"
    if state in ("unstable", "blocked"):
        if rollup in ("FAILURE", "ERROR"):
            return "REWORK_CHECK_FAILED"
        if rollup == "SUCCESS":
            if state == "blocked":
                return "BLOCKED_TERMINAL"
            return "WAIT_FOR_CHECKS"
        return "WAIT_FOR_CHECKS"
    return "UNKNOWN"


def next_rework_cycle(labels: list[str], label_manager: LabelManager) -> int:
    cycle = label_manager.extract_rework_cycle(labels)
    if cycle is not None:
        return cycle + 1
    return 1


_REWORK_HEADERS: dict[PostApprovalAction, tuple[str, str, str]] = {
    "REWORK_CONFLICT": (
        "Merge conflict against base branch",
        "GitHub reports merge conflicts against the base branch.",
        "Rebase or merge the base branch and resolve the conflicts, "
        "then push so the PR is mergeable again.",
    ),
    "REWORK_BEHIND": (
        "Branch is behind base branch",
        "GitHub reports the branch is behind the base branch and "
        "branch protection requires it to be up-to-date before merge.",
        "Rebase (or merge) the base branch into this branch and push, "
        "so the PR is mergeable again.",
    ),
    "REWORK_CHECK_FAILED": (
        "Required check failed on this PR",
        "A required status check has FAILED or ERRORED on this PR's "
        "head commit. The reviewer already approved, but a CI/check "
        "regression is now blocking merge.",
        "Open the PR's checks tab to identify the failing check, "
        "reproduce locally, fix the underlying problem, and push "
        "so the checks turn green.",
    ),
}


def build_rework_feedback(pr: PRInfo, action: PostApprovalAction) -> str:
    # Caller only invokes this for actions in REWORK_ACTIONS, and the header
    # table covers exactly those. A KeyError means dispatch drifted.
    title, detail, guidance = _REWORK_HEADERS[action]
    state = normalized_state(pr.mergeable_state) or "unknown"
    rollup = pr.status_check_rollup or "n/a"
    lines = [
        f"{title} (cycle handled by post-publish gate, not the reviewer):",
        "",
        f"PR #{pr.number} was approved by the reviewer but is no longer "
        "ready to merge.",
        f"- URL: {pr.url}",
        f"- Branch: {pr.branch}",
        f"- Mergeability: {state}",
        f"- Status checks: {rollup}",
        f"- Diagnosis: {detail}",
        "",
        guidance,
    ]
    return "\n".join(lines)


def build_post_publish_validation_comment(feedback: str) -> str:
    return f"{POST_PUBLISH_VALIDATION_COMMENT_MARKER}\n{feedback}"


def build_escalation(
    *,
    pr: PRInfo,
    issue_number: int,
    issue_key: str,
    pr_number: int,
    label_manager: LabelManager,
    kind: PostPublishEscalationKind,
    reason: str,
) -> DiscoveredAwaitingMergeEscalation:
    return DiscoveredAwaitingMergeEscalation(
        issue_number=issue_number,
        pr_number=pr_number,
        pr_url=pr.url,
        issue_key=issue_key,
        rework_cycle=next_rework_cycle(pr.labels, label_manager),
        kind=kind,
        reason=reason,
    )
