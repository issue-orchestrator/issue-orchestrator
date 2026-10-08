"""Integration-branch mode: the typed vocabulary of its one owner (#8144).

In integration mode agents' PRs target a long-lived integration branch, and io
itself merges each approved PR there, serialized, once its checks are green on
a head that already contains the integration tip. The operator merges ONE
delivery PR (integration -> default branch) with a merge commit; afterwards the
integration branch continues from the default branch without diverging.

This module holds what the owner (:mod:`~..control.integration_branch`), the
planner, the applier and the Tech lead page share, and nothing that reads or
writes GitHub:

* the facts the owner discovers - each one :data:`IntegrationStep` - which the
  planner turns into one action each and the applier executes;
* the delivery PR's generated body, with the integration tip it describes in a
  marker, so the owner rewrites it only when the tip moved;
* :class:`IntegrationDeliveryView`, the delivery PR as the Tech lead page shows
  it ("waiting on you").
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeAlias

#: The marker on the delivery PR's body naming the integration tip it lists.
DELIVERY_MARKER_PREFIX = "<!-- io:integration-delivery tip="
_DELIVERY_MARKER = re.compile(r"<!-- io:integration-delivery tip=([0-9a-f]{40}) -->")
_SHA = re.compile(r"^[0-9a-f]{40}$")


def require_sha(value: str, *, what: str) -> str:
    """A full lowercase commit SHA, or a loud failure naming *what* it was."""
    if not _SHA.fullmatch(value):
        raise ValueError(f"{what} must be a full 40-character commit SHA, got {value!r}")
    return value


@dataclass(frozen=True)
class BranchComparison:
    """How *head* relates to *base* (GitHub's compare ``base...head``).

    ``ahead_by`` counts commits on head that base lacks; ``behind_by`` counts
    commits on base that head lacks. ``commit_shas`` lists the commits head is
    ahead by (GitHub returns at most 250; ``complete`` says whether that is all).
    """

    ahead_by: int
    behind_by: int
    commit_shas: tuple[str, ...] = ()
    complete: bool = True

    def __post_init__(self) -> None:
        if self.ahead_by < 0 or self.behind_by < 0:
            raise ValueError("a branch comparison counts commits; it cannot be negative")

    @property
    def contains_base(self) -> bool:
        """Whether head already contains every commit of base."""
        return self.behind_by == 0


class BranchMergeOutcome(StrEnum):
    """What a server-side merge of one branch into another did."""

    MERGED = "merged"
    UP_TO_DATE = "up_to_date"
    CONFLICT = "conflict"


@dataclass(frozen=True)
class MergedIntoBranch:
    """A PR merged into a branch, as its delivery summary lists it."""

    number: int
    title: str
    url: str
    merge_commit_sha: str


@dataclass(frozen=True)
class OpenPullRequestRef:
    """An open PR found by its head and base branches."""

    number: int
    url: str
    body: str


# -- the owner's facts (each becomes one action) -------------------------------


@dataclass(frozen=True)
class MergeIntoIntegration:
    """Merge an approved, green, up-to-date PR into the integration branch.

    ``head_sha`` is the head whose checks passed and ``integration_tip`` the
    tip it contains: the applier merges only that head (GitHub's ``sha`` guard)
    and only while the tip has not moved.
    """

    issue_number: int
    issue_key: str
    pr_number: int
    pr_url: str
    pr_title: str
    head_sha: str
    integration_branch: str
    integration_tip: str
    merge_method: str


@dataclass(frozen=True)
class UpdatePullRequestBranch:
    """Bring an approved PR's head up to the integration tip mechanically.

    GitHub merges the base into the PR branch server-side, guarded by the
    expected head: no agent session and no rework cycle. A real conflict is not
    this step's business - GitHub reports the PR ``dirty`` and the conflict
    goes to agent rework.
    """

    issue_number: int
    pr_number: int
    head_sha: str
    integration_tip: str


@dataclass(frozen=True)
class CreateIntegrationBranch:
    """Create the integration branch at the default branch's head."""

    branch: str
    from_sha: str


@dataclass(frozen=True)
class FastForwardIntegration:
    """Move the integration branch forward to the default branch's head.

    Only when integration is an ancestor of the default branch: after the
    operator merges the delivery PR, this is what "continues from main" means.
    """

    branch: str
    from_sha: str
    to_sha: str


@dataclass(frozen=True)
class SyncIntegrationFromDefault:
    """Merge the default branch into integration when both moved on.

    The default branch gained commits integration lacks (the delivery merge
    commit while new PRs landed, or a direct fix) and integration has its own:
    a server-side merge commit keeps the branches from diverging.
    """

    branch: str
    default_branch: str
    integration_tip: str
    default_tip: str


@dataclass(frozen=True)
class OpenDeliveryPullRequest:
    """Open the one delivery PR (integration -> default branch)."""

    head: str
    base: str
    title: str
    body: str
    integration_tip: str


@dataclass(frozen=True)
class RefreshDeliveryPullRequest:
    """Rewrite the delivery PR's generated body for a new integration tip."""

    pr_number: int
    body: str
    integration_tip: str


IntegrationStep: TypeAlias = (
    MergeIntoIntegration
    | UpdatePullRequestBranch
    | CreateIntegrationBranch
    | FastForwardIntegration
    | SyncIntegrationFromDefault
    | OpenDeliveryPullRequest
    | RefreshDeliveryPullRequest
)

#: Steps that act on one issue's PR (the applier verifies the issue's claim).
PULL_REQUEST_STEPS: tuple[type, ...] = (MergeIntoIntegration, UpdatePullRequestBranch)


def step_issue_number(step: IntegrationStep) -> int | None:
    """The issue whose PR *step* acts on; None for a branch-level step."""
    if isinstance(step, (MergeIntoIntegration, UpdatePullRequestBranch)):
        return step.issue_number
    return None


def step_pr_number(step: IntegrationStep) -> int | None:
    if isinstance(step, (MergeIntoIntegration, UpdatePullRequestBranch, RefreshDeliveryPullRequest)):
        return step.pr_number
    return None


def describe_step(step: IntegrationStep) -> str:
    """One line for logs, action reasons and liveness."""
    if isinstance(step, MergeIntoIntegration):
        return (f"merge PR #{step.pr_number} ({step.head_sha[:8]}) into {step.integration_branch}"
                f" at {step.integration_tip[:8]}")
    if isinstance(step, UpdatePullRequestBranch):
        return f"update PR #{step.pr_number} ({step.head_sha[:8]}) to integration tip {step.integration_tip[:8]}"
    if isinstance(step, CreateIntegrationBranch):
        return f"create {step.branch} at {step.from_sha[:8]}"
    if isinstance(step, FastForwardIntegration):
        return f"fast-forward {step.branch} {step.from_sha[:8]} -> {step.to_sha[:8]}"
    if isinstance(step, SyncIntegrationFromDefault):
        return f"merge {step.default_branch} ({step.default_tip[:8]}) into {step.branch} ({step.integration_tip[:8]})"
    if isinstance(step, OpenDeliveryPullRequest):
        return f"open the delivery PR {step.head} -> {step.base} at {step.integration_tip[:8]}"
    return f"refresh delivery PR #{step.pr_number} for tip {step.integration_tip[:8]}"


# -- the delivery PR -----------------------------------------------------------


def delivery_title(*, head: str, base: str) -> str:
    return f"Deliver {head} to {base}"


def delivery_marker(tip: str) -> str:
    return f"{DELIVERY_MARKER_PREFIX}{require_sha(tip, what='integration tip')} -->"


def delivered_tip(body: str) -> str | None:
    """The integration tip a delivery PR body describes; None when unmarked."""
    found = _DELIVERY_MARKER.search(body or "")
    return found.group(1) if found else None


def render_delivery_body(
    *,
    head: str,
    base: str,
    tip: str,
    ahead_by: int,
    merged: Sequence[MergedIntoBranch],
    complete: bool,
) -> str:
    """The delivery PR's generated body: what merging it delivers."""
    lines = [
        delivery_marker(tip),
        f"## What this delivers to `{base}`",
        "",
        f"`{head}` is {ahead_by} commit(s) ahead of `{base}` at `{tip[:12]}`.",
        "",
    ]
    if merged:
        lines.append(f"### Pull requests merged into `{head}` ({len(merged)})")
        lines.append("")
        lines.extend(f"- #{pr.number} {pr.title}" for pr in merged)
    else:
        lines.append(f"No pull request merged into `{head}` is listed: its commits came another way.")
    if not complete:
        lines.extend(["", "_The list may be incomplete: GitHub compared at most 250 commits._"])
    lines.extend([
        "",
        "### How to deliver",
        "",
        f"Merge this PR with a **merge commit** (not squash or rebase). issue-orchestrator then"
        f" fast-forwards `{head}` to `{base}`, so it continues from `{base}` without diverging."
        f" Never delete `{head}`.",
        "",
        "_issue-orchestrator maintains this PR (#8144); it rewrites this body when"
        f" `{head}` moves._",
    ])
    return "\n".join(lines)


def merged_in_delivery(
    merged: Sequence[MergedIntoBranch], delivered_commits: Sequence[str]
) -> tuple[MergedIntoBranch, ...]:
    """The merged PRs whose merge commit is among the commits being delivered."""
    commits = set(delivered_commits)
    return tuple(sorted((pr for pr in merged if pr.merge_commit_sha in commits), key=lambda pr: pr.number))


def merge_commit_message(*, pr_number: int, title: str, branch: str, head_sha: str, tip: str) -> tuple[str, str]:
    """The (title, message) of io's merge of an approved PR into integration."""
    return (
        f"Merge #{pr_number}: {title}",
        (f"Merged into {branch} by issue-orchestrator (#8144): reviewer-approved, checks"
         f" green on {head_sha[:12]}, which contains {branch} at {tip[:12]}."),
    )


@dataclass(frozen=True)
class IntegrationDeliveryView:
    """The delivery PR as the Tech lead page shows it ("waiting on you")."""

    pr_number: int
    url: str
    head: str
    base: str
    integration_tip: str
    ahead_by: int
    merged_pr_numbers: tuple[int, ...]
    observed_at: str


__all__ = [
    "BranchComparison",
    "BranchMergeOutcome",
    "CreateIntegrationBranch",
    "DELIVERY_MARKER_PREFIX",
    "FastForwardIntegration",
    "IntegrationDeliveryView",
    "IntegrationStep",
    "MergeIntoIntegration",
    "MergedIntoBranch",
    "OpenDeliveryPullRequest",
    "OpenPullRequestRef",
    "PULL_REQUEST_STEPS",
    "RefreshDeliveryPullRequest",
    "SyncIntegrationFromDefault",
    "UpdatePullRequestBranch",
    "delivered_tip",
    "delivery_marker",
    "delivery_title",
    "describe_step",
    "merge_commit_message",
    "merged_in_delivery",
    "render_delivery_body",
    "require_sha",
    "step_issue_number",
    "step_pr_number",
]
