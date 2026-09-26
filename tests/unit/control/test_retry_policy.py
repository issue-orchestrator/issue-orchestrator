from __future__ import annotations

from unittest.mock import Mock

from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.retry_policy import (
    OpenPullRequestProbe,
    labels_to_remove_for_retry,
    retry_label_removals,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.pull_request_tracker import PRInfo

ISSUE = 42


def _label_manager() -> LabelManager:
    return LabelManager(Config())


def _pr(number: int, state: str) -> PRInfo:
    return PRInfo(number=number, title=f"#{ISSUE}: work", url="u", branch=f"{ISSUE}-work",
                  body="", state=state, labels=[])


def _reader(*prs: PRInfo) -> Mock:
    reader = Mock(spec=OpenPullRequestProbe)
    reader.has_open_pr_for_issue_complete.return_value = any(pr.state == "open" for pr in prs)
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
    reader = _reader(_pr(7, "closed"), _pr(9, "open"))

    result = retry_label_removals(ISSUE, [lm.blocked_failed, lm.pr_pending], lm, reader)

    assert result == [lm.blocked_failed]
    # The complete, uncached probe - never a capped or cached PR list.
    reader.has_open_pr_for_issue_complete.assert_called_once_with(ISSUE)


def test_retry_strips_pr_pending_when_every_pr_is_closed_or_merged() -> None:
    lm = _label_manager()
    reader = _reader(_pr(7, "closed"), _pr(8, "merged"))

    result = retry_label_removals(ISSUE, [lm.blocked_failed, lm.pr_pending], lm, reader)

    assert result == sorted([lm.blocked_failed, lm.pr_pending])


def test_retry_without_pr_pending_spends_no_pr_read() -> None:
    lm = _label_manager()
    reader = _reader(_pr(9, "open"))

    result = retry_label_removals(ISSUE, [lm.blocked_failed], lm, reader)

    assert result == [lm.blocked_failed]
    reader.has_open_pr_for_issue_complete.assert_not_called()
