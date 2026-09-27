from __future__ import annotations

from unittest.mock import Mock

from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.retry_policy import (
    OpenPullRequestIndex,
    OpenPullRequestListing,
    labels_to_remove_for_retry,
    retry_label_removals,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.pull_request_tracker import PRInfo

ISSUE = 42


def _label_manager() -> LabelManager:
    return LabelManager(Config())


def _pr(number: int, *, branch: str = f"{ISSUE}-work", title: str = "work", body: str = "") -> PRInfo:
    return PRInfo(number=number, title=title, url="u", branch=branch, body=body, state="open", labels=[])


def _reader(*prs: PRInfo) -> Mock:
    """The complete open-PR listing (closed PRs are never in it)."""
    reader = Mock(spec=OpenPullRequestListing)
    reader.list_open_prs_complete.return_value = list(prs)
    return reader


def test_labels_to_remove_for_retry_includes_blocking_and_pr_pending() -> None:
    lm = _label_manager()
    labels = ["agent:web", lm.blocked, lm.pr_pending, lm.tech_lead_needs_human]

    result = labels_to_remove_for_retry(labels, lm, has_open_pr=False)

    assert lm.blocked in result
    assert lm.pr_pending in result
    assert lm.tech_lead_needs_human in result
    assert "agent:web" not in result


def test_labels_to_remove_for_retry_excludes_non_blocking_labels() -> None:
    lm = _label_manager()
    labels = ["agent:web", "documentation", "enhancement"]

    result = labels_to_remove_for_retry(labels, lm, has_open_pr=False)

    assert result == []


def test_labels_to_remove_for_retry_dedupes_and_sorts() -> None:
    lm = _label_manager()
    labels = [lm.pr_pending, lm.blocked, lm.blocked, lm.pr_pending]

    result = labels_to_remove_for_retry(labels, lm, has_open_pr=False)

    assert result == sorted(set([lm.blocked, lm.pr_pending]))


def test_labels_to_remove_for_retry_keeps_pr_pending_while_a_pr_is_open() -> None:
    lm = _label_manager()
    labels = [lm.blocked_failed, lm.pr_pending]

    result = labels_to_remove_for_retry(labels, lm, has_open_pr=True)

    assert result == [lm.blocked_failed]


def test_retry_keeps_pr_pending_when_the_issue_has_an_open_pr() -> None:
    """#7293: Retry on an issue with an open PR releases its review, it does not relaunch."""
    lm = _label_manager()
    reader = _reader(_pr(7, branch="other-work"), _pr(9))

    result = retry_label_removals(
        ISSUE, [lm.blocked_failed, lm.pr_pending], lm, OpenPullRequestIndex(reader))

    assert result == [lm.blocked_failed]
    reader.list_open_prs_complete.assert_called_once_with()


def test_retry_strips_pr_pending_when_no_open_pr_belongs_to_the_issue() -> None:
    """A PR that merely MENTIONS the issue is not its PR (a search would say it is)."""
    lm = _label_manager()
    reader = _reader(_pr(7, branch="99-other", title=f"follow-up to #{ISSUE}", body=f"see #{ISSUE}"))

    result = retry_label_removals(
        ISSUE, [lm.blocked_failed, lm.pr_pending], lm, OpenPullRequestIndex(reader))

    assert result == sorted([lm.blocked_failed, lm.pr_pending])


def test_retry_without_pr_pending_spends_no_pr_read() -> None:
    lm = _label_manager()
    reader = _reader(_pr(9))

    result = retry_label_removals(ISSUE, [lm.blocked_failed], lm, OpenPullRequestIndex(reader))

    assert result == [lm.blocked_failed]
    reader.list_open_prs_complete.assert_not_called()


def test_a_closing_reference_ties_a_pr_to_its_issue() -> None:
    lm = _label_manager()
    reader = _reader(_pr(9, branch="feature/renamed", body=f"Closes #{ISSUE}"))

    result = retry_label_removals(
        ISSUE, [lm.blocked_failed, lm.pr_pending], lm, OpenPullRequestIndex(reader))

    assert result == [lm.blocked_failed]


def test_one_index_lists_open_prs_once_for_many_issues() -> None:
    lm = _label_manager()
    reader = _reader(_pr(9))
    index = OpenPullRequestIndex(reader)

    for issue in range(1, 51):
        retry_label_removals(issue, [lm.blocked_failed, lm.pr_pending], lm, index)

    reader.list_open_prs_complete.assert_called_once_with()
