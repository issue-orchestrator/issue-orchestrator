from types import SimpleNamespace

from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.review_validity import evaluate_review_validity
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
