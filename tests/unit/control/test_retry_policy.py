from __future__ import annotations

from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.retry_policy import labels_to_remove_for_retry
from issue_orchestrator.infra.config import Config


def _label_manager() -> LabelManager:
    return LabelManager(Config())


def test_labels_to_remove_for_retry_includes_blocking_and_pr_pending() -> None:
    lm = _label_manager()
    labels = ["agent:web", lm.blocked, lm.pr_pending, lm.tech_lead_needs_human]

    result = labels_to_remove_for_retry(labels, lm)

    assert lm.blocked in result
    assert lm.pr_pending in result
    assert lm.tech_lead_needs_human in result
    assert "agent:web" not in result


def test_labels_to_remove_for_retry_excludes_non_blocking_labels() -> None:
    lm = _label_manager()
    labels = ["agent:web", "documentation", "enhancement"]

    result = labels_to_remove_for_retry(labels, lm)

    assert result == []


def test_labels_to_remove_for_retry_dedupes_and_sorts() -> None:
    lm = _label_manager()
    labels = [lm.pr_pending, lm.blocked, lm.blocked, lm.pr_pending]

    result = labels_to_remove_for_retry(labels, lm)

    assert result == sorted(set([lm.blocked, lm.pr_pending]))
