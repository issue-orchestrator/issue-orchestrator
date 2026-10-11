"""An issue already delivered in part is not closed by a receipt that forgot --partial (#8689).

porchpin #327 is a multi-PR issue. Slices 1 and 2 merged as ``Refs #327`` PRs
#511 and #515. Slice 3a's review exchange halted, and the coder's forced last
receipt omitted ``--partial``. Recovery published PR #525 with body line 1
``Closes #327``: nothing compared the receipt with the issue's own history.

Every publication path prepares its PR through the one owner
(:class:`PullRequestPreparation`): the live pipeline, manual publication and
retained-work recovery. These tests plant the issue's merged history behind a
fake GitHub host and drive that owner.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import Mock

import pytest

from issue_orchestrator.control.completion_ports import GitAdapter, LabelAdapter
from issue_orchestrator.control.completion_preparation import PreparedPullRequest
from issue_orchestrator.control.pull_request_preparation import PullRequestPreparationRefusal
from issue_orchestrator.domain.issue_delivery import (
    DeliveryBasis,
    IssueDelivery,
    MergedPullRequest,
    stated_delivery,
)
from issue_orchestrator.domain.models import CompletionOutcome, CompletionRecord, RequestedAction
from issue_orchestrator.domain.pr_issue_reference import declares_partial_delivery
from issue_orchestrator.execution.session_output_adapter import FileSystemSessionOutput
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.working_copy import BranchCommitMessagesResult
from issue_orchestrator.ports.pull_request_tracker import PRInfo
from tests.callback_endpoint_helpers import ready_callback_endpoint
from tests.run_allocation_helpers import make_completion_processor

REPO = "porchpin/porchpin"
ISSUE = 327
BRANCH = "327-slice-3a"


def _pr(
    number: int, body: str, *, state: str = "merged", branch: str | None = None,
    merged_at: str | None = None,
) -> PRInfo:
    """A PR; a merged one merges in number order unless ``merged_at`` says otherwise."""
    if state == "merged" and merged_at is None:
        merged_at = f"2026-10-01T{number // 60 % 24:02d}:{number % 60:02d}:00Z"
    return PRInfo(
        number=number, title=f"#{ISSUE}: slice", url=f"https://github.com/{REPO}/pull/{number}",
        branch=branch or f"{ISSUE}-slice-{number}", body=body, state=state, labels=[],
        merged_at=merged_at,
    )


class FakeGitHubHost:
    """Answers the issue's merged-PR history and the branch's open PRs."""

    def __init__(self, merged: list[PRInfo], open_on_branch: list[PRInfo] | None = None) -> None:
        self.merged = {pr.number: pr for pr in merged}
        self.open_on_branch = open_on_branch or []
        self.history_reads = 0

    def merged_pr_history(self, issue_number: int) -> tuple[MergedPullRequest, ...]:
        """One read answers the bodies and merge times; no per-PR GET (codex r3 F2)."""
        self.history_reads += 1
        assert issue_number == ISSUE
        return tuple(
            MergedPullRequest(pr.number, pr.body, datetime.fromisoformat(pr.merged_at.replace("Z", "+00:00")))
            for pr in self.merged.values()
        )

    def get_pr(self, pr_number: int) -> PRInfo | None:  # pragma: no cover
        raise AssertionError("the delivery history needs no per-PR read")

    def get_prs_for_branch(self, branch: str, state: str = "open") -> list[PRInfo]:
        return [pr for pr in self.open_on_branch if pr.branch == branch]

    def get_prs_for_issue(self, issue_number: int, state: str = "open") -> list[PRInfo]:
        return []

    def create_pr(self, **kwargs):  # pragma: no cover - preparation never writes
        raise AssertionError("PR preparation must not create a PR")

    def add_comment(self, issue_or_pr_number: int, body: str) -> str:  # pragma: no cover
        raise AssertionError("PR preparation must not comment")

    def set_pr_base(self, pr_number: int, base: str) -> None:  # pragma: no cover
        raise AssertionError("PR preparation must not retarget")


SLICES_MERGED_AS_PARTIAL = [
    _pr(511, f"Refs #{ISSUE}\n\nPartial delivery: slice 1"),
    _pr(515, f"Refs #{ISSUE}\n\nPartial delivery: slice 2"),
]


def _record(**overrides) -> CompletionRecord:
    fields = dict(
        session_id="s", timestamp="2026-10-07T17:04:00Z", outcome=CompletionOutcome.COMPLETED,
        summary="slice 3a", requested_actions=[RequestedAction.PUSH_BRANCH, RequestedAction.CREATE_PR],
        implementation="Slice 3a: the Pickup DO's schema", problems="None",
    )
    fields.update(overrides)
    return CompletionRecord(**fields)


def _preparation(host: FakeGitHubHost, *commit_messages: str):
    git = Mock(spec=GitAdapter)
    git.branch_commit_messages_against_base.return_value = BranchCommitMessagesResult(
        success=True, messages=commit_messages or ("Slice 3a: Pickup DO schema",),
    )
    processor = make_completion_processor(
        agent_callback_endpoint=ready_callback_endpoint(), label_adapter=Mock(spec=LabelAdapter),
        pr_adapter=host, git_adapter=git, session_output=FileSystemSessionOutput(),
        label_config={},
        config=Config(repo=REPO, worktree_base_branch_override="main", review_exchange_mode="via-draft-pr"),
    )
    return processor.pull_requests


def _prepare(host: FakeGitHubHost, record: CompletionRecord, *commit_messages: str):
    return _preparation(host, *commit_messages).prepare(
        worktree=Path("/tmp/wt-327"), record=record, issue_number=ISSUE,
        issue_title="Pickup route", branch=BRANCH, agent_label="agent:coder",
        exchange_mode=None, exchange_result=None,
    )


def test_a_receipt_without_partial_on_a_partially_delivered_issue_never_publishes_a_closing_pr() -> None:
    """The reproduction (#8689): merged slices say ``Refs #327``; the forced
    receipt for slice 3a omits --partial and its commits close nothing."""
    host = FakeGitHubHost(SLICES_MERGED_AS_PARTIAL)

    prepared = _prepare(host, _record(partial_pr=False))

    if isinstance(prepared, PullRequestPreparationRefusal):
        return
    assert prepared.body.splitlines()[0] != f"Closes #{ISSUE}"
    assert prepared.body.splitlines()[0] == f"Refs #{ISSUE}"


def test_the_inferred_partial_delivery_refs_the_issue_and_says_why() -> None:
    host = FakeGitHubHost(SLICES_MERGED_AS_PARTIAL)

    prepared = _prepare(host, _record())

    assert isinstance(prepared, PreparedPullRequest)
    assert prepared.partial_pr is True
    assert prepared.delivery == IssueDelivery(DeliveryBasis.INFERRED_PARTIAL, evidence_pr=515)
    assert declares_partial_delivery(prepared.body, ISSUE, repo_slug=REPO)
    assert "Partial delivery (inferred)" in prepared.body
    assert "merged PR #515" in prepared.body
    assert "--finishes-issue" in prepared.body


def test_a_completion_that_finishes_the_issue_closes_it_without_reading_history() -> None:
    host = FakeGitHubHost(SLICES_MERGED_AS_PARTIAL)

    prepared = _prepare(host, _record(finishes_issue=True))

    assert isinstance(prepared, PreparedPullRequest)
    assert prepared.body.splitlines()[0] == f"Closes #{ISSUE}"
    assert prepared.partial_pr is False
    assert host.history_reads == 0


def test_a_partial_claim_needs_no_history_read() -> None:
    host = FakeGitHubHost([])

    prepared = _prepare(host, _record(partial_pr=True))

    assert isinstance(prepared, PreparedPullRequest)
    assert prepared.body.splitlines()[0] == f"Refs #{ISSUE}"
    assert "Partial delivery (inferred)" not in prepared.body
    assert host.history_reads == 0


@pytest.mark.parametrize(
    ("merged", "partial"),
    [
        pytest.param([], False, id="nothing-merged"),
        # The latest merged PR closed the issue: it was reopened as new work.
        pytest.param([*SLICES_MERGED_AS_PARTIAL, _pr(520, f"Closes #{ISSUE}")], False, id="reopened"),
        # An earlier mistaken close does not end a delivery that later slices continued.
        pytest.param([_pr(400, f"Closes #{ISSUE}"), *SLICES_MERGED_AS_PARTIAL], True, id="closed-then-continued"),
        # A merged PR that only mentions the issue is not one of its slices.
        pytest.param([*SLICES_MERGED_AS_PARTIAL, _pr(530, f"Closes #530\n\nSee #{ISSUE} for context")], True,
                     id="mention-is-not-a-slice"),
        # Another repository's issue with the same number is not this issue.
        pytest.param([_pr(531, f"Refs other/repo#{ISSUE}")], False, id="other-repository"),
    ],
)
def test_the_latest_merged_pr_that_links_the_issue_decides(merged, partial) -> None:
    prepared = _prepare(FakeGitHubHost(merged), _record())

    assert isinstance(prepared, PreparedPullRequest)
    assert prepared.partial_pr is partial
    assert prepared.body.splitlines()[0] == (f"Refs #{ISSUE}" if partial else f"Closes #{ISSUE}")


class _RateLimitedHistory(FakeGitHubHost):
    def merged_pr_history(self, issue_number: int):
        import httpx

        from issue_orchestrator.adapters.github.rate_limit import github_http_failure

        raise github_http_failure(
            "GitHub POST /graphql failed: 403",
            status_code=403,
            headers=httpx.Headers({"x-ratelimit-remaining": "0", "x-ratelimit-reset": "4000000000"}),
            response_text='{"message": "API rate limit exceeded"}',
            method="POST",
            url="/graphql",
        )


def test_an_unreadable_history_is_a_retryable_refusal_with_the_reset() -> None:
    """Unread is not whole: publishing ``Closes`` on a failed read is the bug."""
    prepared = _prepare(_RateLimitedHistory([]), _record())

    assert isinstance(prepared, PullRequestPreparationRefusal)
    assert "could not read the merged PRs of #327" in prepared.message
    assert prepared.rate_limit is not None


def test_an_inferred_partial_delivery_refuses_a_commit_that_closes_the_issue() -> None:
    """The live push runs the same guard before it writes the branch."""
    host = FakeGitHubHost(SLICES_MERGED_AS_PARTIAL)
    preparation = _preparation(host, f"Slice 3a\n\nFixes #{ISSUE}")

    refusal = preparation.partial_delivery_refusal(
        record=_record(), worktree=Path("/tmp/wt-327"), issue_number=ISSUE, branch=BRANCH,
    )

    assert refusal is not None
    assert "already delivered in part (merged PR #515" in refusal.message
    assert "close it by keyword" in refusal.message


def test_an_inferred_partial_delivery_refuses_an_open_pr_that_closes_the_issue() -> None:
    """porchpin #525 itself: the branch's open PR says ``Closes #327``. The
    refusal names the way out for a PR that really finishes the issue."""
    open_pr = _pr(525, f"Closes #{ISSUE}\n\nSlice 3a", state="open", branch=BRANCH)
    host = FakeGitHubHost(SLICES_MERGED_AS_PARTIAL, open_on_branch=[open_pr])

    prepared = _prepare(host, _record())

    assert isinstance(prepared, PullRequestPreparationRefusal)
    assert "existing PR #525 closes it on merge" in prepared.message
    assert "--finishes-issue" in prepared.message


def test_a_finishing_completion_may_publish_through_an_open_closing_pr() -> None:
    open_pr = _pr(525, f"Closes #{ISSUE}\n\nSlice 4", state="open", branch=BRANCH)
    host = FakeGitHubHost(SLICES_MERGED_AS_PARTIAL, open_on_branch=[open_pr])

    prepared = _prepare(host, _record(finishes_issue=True))

    assert isinstance(prepared, PreparedPullRequest)
    assert prepared.body.splitlines()[0] == f"Closes #{ISSUE}"


def test_merge_time_not_pr_number_orders_the_history() -> None:
    """Codex r1 F1: #515 closed the issue and merged first; the issue was
    reopened and the older #511 then merged as a slice. #511 is the latest
    delivery, so the issue is mid-delivery."""
    host = FakeGitHubHost([
        _pr(511, f"Refs #{ISSUE}", merged_at="2026-10-07T12:00:00Z"),
        _pr(515, f"Closes #{ISSUE}", merged_at="2026-10-05T12:00:00Z"),
    ])

    prepared = _prepare(host, _record())

    assert isinstance(prepared, PreparedPullRequest)
    assert prepared.delivery == IssueDelivery(DeliveryBasis.INFERRED_PARTIAL, evidence_pr=511)


def test_a_finishing_completion_refuses_an_open_pr_that_only_refs_the_issue() -> None:
    """Codex r2 F1: reuse keeps the open PR's body. A final slice published
    through an open ``Refs #327`` PR would leave the finished issue open."""
    open_pr = _pr(525, f"Refs #{ISSUE}\n\nSlice 4", state="open", branch=BRANCH)
    host = FakeGitHubHost(SLICES_MERGED_AS_PARTIAL, open_on_branch=[open_pr])

    prepared = _prepare(host, _record(finishes_issue=True))

    assert isinstance(prepared, PullRequestPreparationRefusal)
    assert "existing PR #525 does not close it" in prepared.message
    assert "'Closes #327'" in prepared.message


def test_a_delivery_cannot_be_both_partial_and_finished() -> None:
    with pytest.raises(ValueError, match="cannot both"):
        stated_delivery(partial_pr=True, finishes_issue=True)
    with pytest.raises(ValueError, match="only an inferred partial delivery names its evidence PR"):
        IssueDelivery(DeliveryBasis.WHOLE, evidence_pr=511)
