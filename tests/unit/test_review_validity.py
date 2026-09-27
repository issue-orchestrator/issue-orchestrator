from types import SimpleNamespace

import pytest

from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.review_validity import (
    evaluate_review_validity,
    evaluate_review_withholding,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.pull_request_tracker import PRInfo


def test_query_filtered_pr_does_not_require_embedded_review_label() -> None:
    config = Config()
    config.code_review_label = "needs-code-review"
    validity = evaluate_review_validity(
        config=config,
        label_manager=LabelManager(config),
        issue=None,
        pr=PRInfo(
            number=1,
            title="PR",
            url="https://example.test/pull/1",
            branch="1-feature",
            body="Closes #1",
            state="open",
            labels=[],
        ),
        review_label_confirmed=True,
    )

    assert validity.valid is True
    assert validity.reason == "ok"


def test_direct_pr_snapshot_requires_review_label_when_missing() -> None:
    config = Config()
    config.code_review_label = "needs-code-review"
    validity = evaluate_review_validity(
        config=config,
        label_manager=LabelManager(config),
        issue=SimpleNamespace(labels=["agent:web"]),
        pr=PRInfo(
            number=1,
            title="PR",
            url="https://example.test/pull/1",
            branch="1-feature",
            body="Closes #1",
            state="open",
            labels=[],
        ),
    )

    assert validity.valid is False
    assert validity.reason == "review_label_missing"


# -- the owner's answer with and without one block (#7399) --------------------


def _withholding(issue_labels: list[str], pr_labels: list[str] | None = None):
    config = Config()
    config.code_review_label = "needs-code-review"
    return evaluate_review_withholding(
        config=config,
        label_manager=LabelManager(config),
        issue=SimpleNamespace(labels=issue_labels),  # type: ignore[arg-type]
        pr=PRInfo(
            number=2, title="PR", url="https://example.test/pull/2", branch="1-feature",
            body="Closes #1", state="open",
            labels=pr_labels if pr_labels is not None else ["needs-code-review"],
        ),
        block_label="blocked-failed",
    )


def test_a_review_withheld_only_by_the_issue_block() -> None:
    withholding = _withholding(["blocked-failed", "pr-pending"])

    assert withholding.current.reason == "issue_blocked"
    assert withholding.without_block.valid
    assert withholding.withheld_only_by_block


@pytest.mark.parametrize(
    ("issue_labels", "pr_labels"),
    [
        (["pr-pending"], None),  # nothing withholds it
        (["blocked-failed", "needs-human"], None),  # a second block
        (["blocked-failed", "blocked"], None),  # a human's block
        (["blocked-failed", "needs-rework"], None),  # rework is owed
        (["blocked-failed"], []),  # discovery would not list it at all
        (["blocked-failed"], ["needs-code-review", "needs-rework"]),  # the PR owes rework
    ],
)
def test_anything_besides_the_block_means_not_only_the_block(
    issue_labels: list[str], pr_labels: list[str] | None
) -> None:
    assert not _withholding(issue_labels, pr_labels).withheld_only_by_block
