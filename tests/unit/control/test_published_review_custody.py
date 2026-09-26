"""Which open PR carries an issue's published validated work (#7293)."""

from __future__ import annotations

import pytest

from issue_orchestrator.control.published_review_custody import (
    PublishedValidatedWorkHeld,
)
from issue_orchestrator.domain.validated_work import ValidatedWorkState

from tests.unit.control.published_review_support import (
    LATER_PUSH,
    PUBLISHED,
    DispositionStore,
    PullRequests,
    branch,
    custody,
    disposition,
    pr,
)

ISSUE = 392


def _owner(records, prs):
    store = DispositionStore({ISSUE: tuple(records)})
    pulls = PullRequests({ISSUE: list(prs)})
    return custody(store, pulls), pulls


def test_open_pr_at_the_published_head_holds_the_issue():
    owner, pulls = _owner(
        [disposition(ISSUE, ValidatedWorkState.RECOVERED)], [pr(ISSUE, 500)]
    )

    (hold,) = owner.holds(ISSUE)

    assert (hold.issue_number, hold.pr_number, hold.branch_name) == (
        ISSUE, 500, branch(ISSUE)
    )
    assert hold.published_head_sha == PUBLISHED
    # Uncached, complete read by the published record's own branch.
    assert pulls.reads == [(branch(ISSUE), "open-complete")]


def test_the_published_pr_still_holds_after_a_later_push():
    """A rework or base update on top does not make destroying the PR safe."""
    owner, _ = _owner(
        [disposition(ISSUE, ValidatedWorkState.RECOVERED, pr_number=500)],
        [pr(ISSUE, 500, head_sha=LATER_PUSH)],
    )

    assert [hold.pr_number for hold in owner.holds(ISSUE)] == [500]


@pytest.mark.parametrize(
    "open_pr",
    [
        pytest.param(pr(ISSUE, 500, state="closed"), id="pr-closed-by-operator"),
        pytest.param(pr(ISSUE, 500, state="merged"), id="pr-merged"),
        pytest.param(
            pr(ISSUE, 501, head_sha=LATER_PUSH), id="another-pr-not-at-the-published-head"
        ),
        pytest.param(
            pr(ISSUE, 500, branch_name=f"{ISSUE}-other"), id="pr-on-another-branch"
        ),
    ],
)
def test_nothing_holds_without_an_open_pr_carrying_the_work(open_pr):
    owner, _ = _owner(
        [disposition(ISSUE, ValidatedWorkState.RECOVERED, pr_number=500)], [open_pr]
    )

    assert owner.holds(ISSUE) == ()
    owner.require_released(ISSUE)


@pytest.mark.parametrize(
    "state",
    [
        ValidatedWorkState.QUEUED,
        ValidatedWorkState.PARKED,
        ValidatedWorkState.PUBLISHING,
        ValidatedWorkState.FAILED,
        ValidatedWorkState.ABANDONED,
    ],
)
def test_only_published_records_hold_and_others_spend_no_pr_read(state):
    owner, pulls = _owner([disposition(ISSUE, state)], [pr(ISSUE, 500)])

    assert owner.holds(ISSUE) == ()
    assert pulls.reads == []


def test_require_released_refuses_with_the_protected_pr_named():
    owner, _ = _owner(
        [disposition(ISSUE, ValidatedWorkState.RECOVERED, pr_number=500)],
        [pr(ISSUE, 500)],
    )

    with pytest.raises(PublishedValidatedWorkHeld) as refused:
        owner.require_released(ISSUE)

    assert refused.value.STALE_REASON == "published_validated_work_under_review"
    assert refused.value.observation()["holds"][0]["pr_number"] == 500
    assert "PR #500" in str(refused.value)
    assert "close PR #500" in str(refused.value)
