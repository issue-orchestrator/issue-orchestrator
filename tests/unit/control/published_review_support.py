"""Shared fixtures for published-validated-work-under-review tests (#7293)."""

from __future__ import annotations

from dataclasses import dataclass, field

from issue_orchestrator.control.published_review_custody import PublishedReviewCustody
from issue_orchestrator.domain.validated_work import (
    LineageRole,
    ValidatedWorkFailure,
    ValidatedWorkKey,
    ValidatedWorkState,
)
from issue_orchestrator.domain.validated_work_commands import (
    OperatorResolution,
    ValidatedWorkDisposition,
    ValidatedWorkDispositionBatch,
)
from issue_orchestrator.ports.pull_request_tracker import PRInfo

REPO = "acme/widgets"
BRANCH_SUFFIX = "-validated"
VALIDATED = "a" * 40
PUBLISHED = "b" * 40
LATER_PUSH = "c" * 40


def branch(issue_number: int) -> str:
    return f"{issue_number}{BRANCH_SUFFIX}"


def disposition(
    issue_number: int,
    state: ValidatedWorkState,
    *,
    pr_number: int | None = None,
    published_head_sha: str | None = PUBLISHED,
) -> ValidatedWorkDisposition:
    key = ValidatedWorkKey(REPO, issue_number, branch(issue_number), VALIDATED)
    return ValidatedWorkDisposition(
        record_id=key.record_id,
        key=key,
        evidence_id="e1:fixture",
        state=state,
        lineage_role=LineageRole.HEAD,
        reason="fixture",
        failure=(
            ValidatedWorkFailure.PUSH_FAILED
            if state is ValidatedWorkState.FAILED
            else None
        ),
        pr_number=pr_number,
        published_head_sha=(
            published_head_sha if state is ValidatedWorkState.RECOVERED else None
        ),
        resolution=(
            OperatorResolution("operator", "accepted the loss", "2026-09-23T00:00:00Z")
            if state is ValidatedWorkState.ABANDONED
            else None
        ),
    )


def pr(
    issue_number: int,
    number: int,
    *,
    state: str = "open",
    head_sha: str | None = PUBLISHED,
    branch_name: str | None = None,
) -> PRInfo:
    return PRInfo(
        number=number,
        title=f"#{issue_number}: validated work",
        url=f"https://github.test/{REPO}/pull/{number}",
        branch=branch_name if branch_name is not None else branch(issue_number),
        body="",
        state=state,
        labels=[],
        head_sha=head_sha,
    )


@dataclass
class DispositionStore:
    """The store's ``for_issue`` read, answered from explicit dispositions."""

    by_issue: dict[int, tuple[ValidatedWorkDisposition, ...]] = field(default_factory=dict)

    def for_issue(self, issue_number: int) -> ValidatedWorkDispositionBatch:
        records = self.by_issue.get(issue_number, ())
        if not records:
            return ValidatedWorkDispositionBatch.no_work(issue_number, "fixture")
        return ValidatedWorkDispositionBatch(issue_number, records, "fixture")


@dataclass
class PullRequests:
    """``get_prs_for_issue`` over explicit PRs, recording each read."""

    by_issue: dict[int, list[PRInfo]] = field(default_factory=dict)
    reads: list[tuple[int, str]] = field(default_factory=list)

    def get_prs_for_issue(self, issue_number: int, state: str = "open") -> list[PRInfo]:
        self.reads.append((issue_number, state))
        prs = self.by_issue.get(issue_number, [])
        if state == "all":
            return list(prs)
        return [item for item in prs if item.state == state]


def custody(
    store: DispositionStore, pull_requests: PullRequests
) -> PublishedReviewCustody:
    return PublishedReviewCustody(store, pull_requests)
