"""Tests for the triage-created issue policy owner (ADR-0031 / #6761 F4)."""

import pytest

from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.actions import TriageMilestoneIntent
from issue_orchestrator.control.triage_issue_policy import (
    apply_triage_priority_prefix,
    batch_review_issue_labels,
    decision_issue_labels,
    health_review_issue_labels,
    is_protected_triage_label,
    protected_triage_label_violations,
    resolve_triage_milestone_number,
    triage_issue_milestone_intent,
)
from issue_orchestrator.control.health_review_trigger import (
    HEALTH_REVIEW_ISSUE_TITLE,
)
from issue_orchestrator.domain.triage_session import HEALTH_REVIEW_MARKER_LABEL
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

    @pytest.mark.parametrize(
        "label",
        [
            "In-Progress",
            "NEEDS-HUMAN",
            "Code-Reviewed",
            "Triage-Failed",
            "AGENT:Backend",
            "Blocked:PR-Closed",
            "PUBLISH-FAIL-COUNT-2",
        ],
    )
    def test_protection_is_case_insensitive(self, label: str) -> None:
        """GitHub label names are case-insensitive; case-flipping an owned
        name must not bypass protection (#6761 re-review finding 3)."""
        config = make_config()
        assert is_protected_triage_label(
            label, config=config, labels=LabelManager(config)
        )

    def test_mixed_case_configured_name_rejects_lowercase_agent_label(self) -> None:
        """config.label_in_progress='WIP' must reject an agent's 'wip'."""
        config = make_config()
        config.label_in_progress = "WIP"
        labels = LabelManager(config)
        assert is_protected_triage_label("wip", config=config, labels=labels)
        assert is_protected_triage_label("WiP", config=config, labels=labels)

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

    def test_dedup_is_case_insensitive_first_spelling_wins(self) -> None:
        config = make_config()
        config.triage.explicit_labels = ["Bug"]
        labels = decision_issue_labels(
            config,
            anchor_labels=[],
            agent_labels=("bug", "docs"),
            labels=LabelManager(config),
        )
        assert labels == ("Bug", "docs")

    def test_inherit_match_is_case_insensitive(self) -> None:
        config = make_config()
        config.triage.inherit_labels = ["Team:Backend"]
        labels = decision_issue_labels(
            config,
            anchor_labels=["team:backend"],
            agent_labels=(),
            labels=LabelManager(config),
        )
        assert labels == ("Team:Backend",)

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
        intent = triage_issue_milestone_intent(config, [(9, "M9"), (3, "M3")])
        assert intent.inherited_number == expected
        assert intent.explicit_name is None

    def test_explicit_strategy_yields_name_intent_not_a_lookup(self) -> None:
        """The explicit strategy plans a NAME; resolution belongs to the
        create-issue execution boundary, not planning (#6769 finding 4)."""
        config = make_config()
        config.triage.milestone_strategy = MilestoneStrategyConfig(explicit="M5")
        intent = triage_issue_milestone_intent(config, [(9, "M9")])
        assert intent == TriageMilestoneIntent(explicit_name="M5")

    def test_intent_rejects_carrying_both_shapes(self) -> None:
        with pytest.raises(ValueError, match="name OR a number"):
            TriageMilestoneIntent(explicit_name="M5", inherited_number=5)


class TestHealthReviewAnchorPolicy:
    """Health anchors are shaped by the SAME policy owner as batch anchors.

    Before #6763 finding 5 the health-review trigger hand-rolled its own label
    tuple, so it silently dropped ``triage.explicit_labels``, the configured
    priority title, and the milestone strategy. The trigger now composes the
    anchor from ``triage_issue_policy`` helpers; these tests pin that the
    health variant carries every configured behavior plus its marker.
    """

    def test_labels_include_agent_filter_explicit_and_marker(self) -> None:
        config = make_config()
        config.filtering.label = "io-scope"
        config.triage.explicit_labels = ["needs-batch-review", "team:backend"]

        labels = health_review_issue_labels(config)

        assert labels == (
            "agent:triage",
            "io-scope",
            HEALTH_REVIEW_MARKER_LABEL,
            "needs-batch-review",
            "team:backend",
        )

    def test_marker_label_is_always_present(self) -> None:
        """The marker is crash-safe truth (flavor derivation + dedup); it must
        survive even with no filter label and no explicit labels."""
        config = make_config()
        labels = health_review_issue_labels(config)
        assert HEALTH_REVIEW_MARKER_LABEL in labels
        assert "agent:triage" in labels

    def test_health_anchor_labels_dedupe_case_insensitively(self) -> None:
        """An explicit label re-spelling the marker must not double it."""
        config = make_config()
        config.triage.explicit_labels = [HEALTH_REVIEW_MARKER_LABEL.upper()]
        labels = health_review_issue_labels(config)
        assert sum(
            1 for label in labels if label.casefold() == HEALTH_REVIEW_MARKER_LABEL
        ) == 1

    def test_priority_title_shaping_applies_to_health_title(self) -> None:
        config = make_config()
        config.triage.priority = "P1"
        assert (
            apply_triage_priority_prefix(config, HEALTH_REVIEW_ISSUE_TITLE)
            == f"[P1-000] {HEALTH_REVIEW_ISSUE_TITLE}"
        )

    def test_explicit_milestone_intent_applies_with_no_source_prs(self) -> None:
        """Health anchors have no source PRs, so only the explicit strategy can
        apply; the intent carries the NAME for the applier to resolve."""
        config = make_config()
        config.triage.milestone_strategy = MilestoneStrategyConfig(explicit="M9")
        intent = triage_issue_milestone_intent(config, ())
        assert intent.explicit_name == "M9"
        assert intent.inherited_number is None

    def test_inherit_strategy_yields_no_milestone_without_source_prs(self) -> None:
        config = make_config()
        config.triage.milestone_strategy = MilestoneStrategyConfig(
            inherit_from_issues="earliest"
        )
        intent = triage_issue_milestone_intent(config, ())
        assert intent.explicit_name is None
        assert intent.inherited_number is None


class TestExplicitMilestoneResolution:
    """Name -> number resolution at the create-issue execution boundary (F4)."""

    def _milestones(self, _state: str):
        return [
            {"number": 3, "title": "M3", "state": "open"},
            {"number": 5, "title": "M5", "state": "closed"},
        ]

    def test_resolves_planned_name(self) -> None:
        intent = TriageMilestoneIntent(explicit_name="M5")
        assert resolve_triage_milestone_number(intent, self._milestones) == 5

    def test_number_and_none_intents_resolve_without_api_call(self) -> None:
        def _boom(_state: str):
            raise AssertionError("list_milestones must not be called")

        assert (
            resolve_triage_milestone_number(
                TriageMilestoneIntent(inherited_number=7), _boom
            )
            == 7
        )
        assert resolve_triage_milestone_number(TriageMilestoneIntent(), _boom) is None

    def test_unresolvable_name_fails_loudly(self) -> None:
        with pytest.raises(ValueError, match="does not match any"):
            resolve_triage_milestone_number(
                TriageMilestoneIntent(explicit_name="Nope"), self._milestones
            )
