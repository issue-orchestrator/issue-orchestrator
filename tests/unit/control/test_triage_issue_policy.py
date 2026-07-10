"""Tests for the triage-created issue policy owner (ADR-0031 / #6761 F4)."""

import pytest

from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.triage_issue_policy import (
    apply_triage_priority_prefix,
    batch_review_issue_labels,
    decision_issue_labels,
    is_protected_triage_label,
    protected_triage_label_violations,
    triage_issue_milestone,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.config_models import MilestoneStrategyConfig


def make_config(**overrides) -> Config:
    config = Config()
    config.triage_review_agent = "agent:triage"
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


class TestProtectedLabelSet:
    """The protected set derives from config/LabelManager plus family patterns."""

    @pytest.mark.parametrize(
        "label",
        [
            "in-progress",
            "needs-human",
            "needs-rework",
            "code-reviewed",
            "triage-reviewed",
            "triage-failed",
            "validation-failed",
            "publish-failed",
            "publish-fail-count-2",
            "blocked",
            "blocked-failed",
            "blocked:pr-closed",
            "agent:backend",
            "agent:triage",
            "triage:anything",
            "needs-batch-review",
        ],
    )
    def test_workflow_labels_are_protected(self, label: str) -> None:
        config = make_config()
        assert is_protected_triage_label(
            label, config=config, labels=LabelManager(config)
        )

    @pytest.mark.parametrize(
        "label", ["bug", "documentation", "team:backend", "ci", "P2"]
    )
    def test_plain_descriptive_labels_are_allowed(self, label: str) -> None:
        config = make_config()
        assert not is_protected_triage_label(
            label, config=config, labels=LabelManager(config)
        )

    def test_configured_names_are_protected_even_when_nonstandard(self) -> None:
        config = make_config()
        config.filtering.label = "my-scope"
        config.label_in_progress = "wip"
        labels = LabelManager(config)
        assert is_protected_triage_label("my-scope", config=config, labels=labels)
        assert is_protected_triage_label("wip", config=config, labels=labels)

    def test_violations_lists_offending_labels_only(self) -> None:
        config = make_config()
        violations = protected_triage_label_violations(
            ["bug", "in-progress", "agent:x"],
            config=config,
            labels=LabelManager(config),
        )
        assert violations == ["in-progress", "agent:x"]


class TestSharedComposition:
    def test_batch_labels_match_pre_extraction_planner_behavior(self) -> None:
        config = make_config()
        config.filtering.label = "io-scope"
        config.triage.explicit_labels = ["needs-batch-review"]
        config.triage.inherit_labels = ["team:backend", "absent"]

        labels = batch_review_issue_labels(
            config, source_labels=frozenset({"team:backend", "other"})
        )

        assert labels == (
            "agent:triage",
            "io-scope",
            "needs-batch-review",
            "team:backend",
        )

    def test_decision_labels_never_include_the_triage_agent(self) -> None:
        """A decision-created follow-up must not loop back into triage."""
        config = make_config()
        labels = decision_issue_labels(
            config,
            anchor_labels=["agent:triage"],
            agent_labels=("bug",),
            labels=LabelManager(config),
        )
        assert "agent:triage" not in labels
        assert labels == ("bug",)

    def test_decision_labels_reject_protected_agent_labels_loudly(self) -> None:
        config = make_config()
        with pytest.raises(ValueError, match="protected labels"):
            decision_issue_labels(
                config,
                anchor_labels=[],
                agent_labels=("needs-human",),
                labels=LabelManager(config),
            )

    def test_priority_prefix_applied_once(self) -> None:
        config = make_config()
        config.triage.priority = "P2"
        assert apply_triage_priority_prefix(config, "Fix it") == "[P2-000] Fix it"
        assert (
            apply_triage_priority_prefix(config, "[P1-042] Fix it") == "[P1-042] Fix it"
        )
        config.triage.priority = None
        assert apply_triage_priority_prefix(config, "Fix it") == "Fix it"

    @pytest.mark.parametrize(
        ("strategy", "expected"),
        [("earliest", 3), ("latest", 9), (None, None)],
    )
    def test_milestone_strategy(self, strategy, expected) -> None:
        config = make_config()
        config.triage.milestone_strategy = MilestoneStrategyConfig(
            inherit_from_issues=strategy
        )
        milestone = triage_issue_milestone(config, [(9, "M9"), (3, "M3")])
        assert milestone == expected

    def test_explicit_milestone_name_yields_none(self) -> None:
        """Name lookup unimplemented: explicit strategy must not guess."""
        config = make_config()
        config.triage.milestone_strategy = MilestoneStrategyConfig(explicit="M5")
        assert triage_issue_milestone(config, [(9, "M9")]) is None
