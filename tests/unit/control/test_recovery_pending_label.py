"""Recovery custody blocks scratch work and survives unrelated PR completion."""

import pytest

from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.scheduler import AvailabilityReason, Scheduler
from issue_orchestrator.control.tech_lead_issue_policy import (
    is_protected_tech_lead_label,
)
from issue_orchestrator.domain.models import Issue
from issue_orchestrator.infra.config import Config


@pytest.mark.parametrize("prefix", [None, "bot"])
def test_recovery_pending_is_reserved_blocks_launch_and_survives_cleanup(
    prefix: str | None,
) -> None:
    config = Config(repo="owner/repo", label_prefix=prefix)
    labels = LabelManager(config)
    recovery = labels.recovery_pending
    issue = Issue(number=7, title="Preserved validated work", labels=[recovery])
    (decision,) = Scheduler(config=config).evaluate_issues([issue])
    assert not decision.available
    assert decision.reason is AvailabilityReason.BLOCKED_LABEL
    assert recovery in decision.detail
    assert is_protected_tech_lead_label(recovery, config=config, labels=labels)
    assert is_protected_tech_lead_label(recovery.upper(), config=config, labels=labels)
    assert labels.recovered_workflow_labels([recovery, labels.pr_pending]) == [
        labels.pr_pending
    ]
    assert not labels.is_recovered_workflow_label(recovery.upper())


def test_configured_blocking_names_follow_registry_in_scheduler() -> None:
    config = Config(
        repo="owner/repo",
        label_prefix="bot",
        label_blocked="waiting-for-service",
        label_needs_human="operator-attention",
    )
    labels = LabelManager(config)
    issues = [
        Issue(number=7, title="Service unavailable", labels=[labels.blocked.upper()]),
        Issue(number=8, title="Operator decision", labels=[labels.needs_human]),
    ]
    decisions = Scheduler(config=config).evaluate_issues(issues)
    assert all(not decision.available for decision in decisions)
    assert all(
        decision.reason is AvailabilityReason.BLOCKED_LABEL for decision in decisions
    )
