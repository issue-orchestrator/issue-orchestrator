"""Tests for the tech-lead-created issue policy owner (ADR-0031 / #6761 F4)."""

import pytest

from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.actions import TechLeadMilestoneIntent
from issue_orchestrator.control.tech_lead_issue_policy import (
    apply_tech_lead_priority_prefix,
    batch_review_issue_labels,
    case_file_issue_labels,
    decision_issue_labels,
    health_review_issue_labels,
    is_protected_tech_lead_label,
    protected_tech_lead_label_violations,
    resolve_tech_lead_milestone_number,
    tech_lead_follow_up_agent_label,
    tech_lead_issue_milestone_intent,
)
from issue_orchestrator.control.health_review_trigger import (
    HEALTH_REVIEW_ISSUE_TITLE,
)
from issue_orchestrator.domain.tech_lead_session import (
    HEALTH_REVIEW_MARKER_LABEL,
    TECH_LEAD_OBSERVATION_LABEL,
)
from issue_orchestrator.infra.config import Config
from issue_orchestrator.infra.config_models import MilestoneStrategyConfig


DEST_AGENT = "agent:web"


def make_config(**overrides) -> Config:
    from unittest.mock import Mock

    config = Config()
    config.tech_lead_review_agent = "agent:tech-lead"
    # Worker agent the orchestrator routes a create_issue proposal to (#6779 R5/R9).
    config.agents = {DEST_AGENT: Mock()}
    config.tech_lead_follow_up_agent = DEST_AGENT
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


class TestTechLeadFollowUpAgentLabel:
    """The create_issue destination is the typed, validated worker (#6779 R9)."""

    def test_returns_configured_worker_even_when_a_non_worker_sorts_first(self) -> None:
        """Dict order must NOT decide routing: a reviewer/tech_lead/goal-pilot
        agent appearing first is never chosen; the configured worker is."""
        from unittest.mock import Mock

        config = Config()
        # reviewer + tech_lead precede the worker in insertion order.
        config.agents = {
            "agent:reviewer": Mock(),
            "agent:tech-lead": Mock(),
            "agent:worker": Mock(),
        }
        config.tech_lead_follow_up_agent = "agent:worker"

        assert tech_lead_follow_up_agent_label(config) == "agent:worker"

    def test_fails_loudly_when_unset_rather_than_guessing_by_dict_order(self) -> None:
        from unittest.mock import Mock

        config = Config()
        config.agents = {"agent:reviewer": Mock(), "agent:worker": Mock()}
        config.tech_lead_follow_up_agent = None

        with pytest.raises(ValueError, match="tech_lead_follow_up_agent"):
            tech_lead_follow_up_agent_label(config)


class TestProtectedLabelSet:
    """The protected set derives from config/LabelManager plus family patterns."""

    @pytest.mark.parametrize(
        "label",
        [
            "in-progress",
            "needs-human",
            "needs-rework",
            "code-reviewed",
            "tech-lead-reviewed",
            "tech-lead-failed",
            "validation-failed",
            "publish-failed",
            "publish-fail-count-2",
            "blocked",
            "blocked-failed",
            "blocked:pr-closed",
            "agent:backend",
            "agent:tech-lead",
            "tech_lead:anything",
            "needs-batch-review",
        ],
    )
    def test_workflow_labels_are_protected(self, label: str) -> None:
        config = make_config()
        assert is_protected_tech_lead_label(
            label, config=config, labels=LabelManager(config)
        )

    @pytest.mark.parametrize(
        "label", ["bug", "documentation", "team:backend", "ci", "P2"]
    )
    def test_plain_descriptive_labels_are_allowed(self, label: str) -> None:
        config = make_config()
        assert not is_protected_tech_lead_label(
            label, config=config, labels=LabelManager(config)
        )

    def test_configured_names_are_protected_even_when_nonstandard(self) -> None:
        config = make_config()
        config.filtering.label = "my-scope"
        config.label_in_progress = "wip"
        labels = LabelManager(config)
        assert is_protected_tech_lead_label("my-scope", config=config, labels=labels)
        assert is_protected_tech_lead_label("wip", config=config, labels=labels)

    @pytest.mark.parametrize(
        "label",
        [
            "In-Progress",
            "NEEDS-HUMAN",
            "Code-Reviewed",
            "Tech-Lead-Failed",
            "AGENT:Backend",
            "Blocked:PR-Closed",
            "PUBLISH-FAIL-COUNT-2",
        ],
    )
    def test_protection_is_case_insensitive(self, label: str) -> None:
        """GitHub label names are case-insensitive; case-flipping an owned
        name must not bypass protection (#6761 re-review finding 3)."""
        config = make_config()
        assert is_protected_tech_lead_label(
            label, config=config, labels=LabelManager(config)
        )

    def test_mixed_case_configured_name_rejects_lowercase_agent_label(self) -> None:
        """config.label_in_progress='WIP' must reject an agent's 'wip'."""
        config = make_config()
        config.label_in_progress = "WIP"
        labels = LabelManager(config)
        assert is_protected_tech_lead_label("wip", config=config, labels=labels)
        assert is_protected_tech_lead_label("WiP", config=config, labels=labels)

    def test_violations_lists_offending_labels_only(self) -> None:
        config = make_config()
        violations = protected_tech_lead_label_violations(
            ["bug", "in-progress", "agent:x"],
            config=config,
            labels=LabelManager(config),
        )
        assert violations == ["in-progress", "agent:x"]


class TestSharedComposition:
    def test_batch_labels_match_pre_extraction_planner_behavior(self) -> None:
        config = make_config()
        config.filtering.label = "io-scope"
        config.tech_lead.explicit_labels = ["needs-batch-review"]
        config.tech_lead.inherit_labels = ["team:backend", "absent"]

        labels = batch_review_issue_labels(
            config, source_labels=frozenset({"team:backend", "other"})
        )

        assert labels == (
            "agent:tech-lead",
            "io-scope",
            "needs-batch-review",
            "team:backend",
        )

    def test_dedup_is_case_insensitive_first_spelling_wins(self) -> None:
        config = make_config()
        config.tech_lead.explicit_labels = ["Bug"]
        labels = decision_issue_labels(
            config,
            anchor_labels=[],
            agent_labels=("bug", "docs"),
            labels=LabelManager(config),
            destination_agent=DEST_AGENT,
        )
        assert labels == ("Bug", "docs", DEST_AGENT)

    def test_inherit_match_is_case_insensitive(self) -> None:
        config = make_config()
        config.tech_lead.inherit_labels = ["Team:Backend"]
        labels = decision_issue_labels(
            config,
            anchor_labels=["team:backend"],
            agent_labels=(),
            labels=LabelManager(config),
            destination_agent=DEST_AGENT,
        )
        assert labels == ("Team:Backend", DEST_AGENT)

    def test_decision_labels_never_include_the_tech_lead_agent(self) -> None:
        """A decision-created follow-up must not loop back into tech_lead."""
        config = make_config()
        labels = decision_issue_labels(
            config,
            anchor_labels=["agent:tech-lead"],
            agent_labels=("bug",),
            labels=LabelManager(config),
            destination_agent=DEST_AGENT,
        )
        assert "agent:tech-lead" not in labels
        assert labels == ("bug", DEST_AGENT)

    def test_decision_labels_route_to_the_orchestrator_owned_worker(self) -> None:
        """R5: the created issue carries a valid worker agent label so removing
        the gate alone makes normal discovery pick it up."""
        config = make_config()
        gated = decision_issue_labels(
            config,
            anchor_labels=[],
            agent_labels=("bug",),
            labels=LabelManager(config),
            destination_agent=DEST_AGENT,
            gate=True,
        )
        assert DEST_AGENT in gated
        # After the operator removes only the gate, a schedulable agent remains.
        after_approval = tuple(l for l in gated if l != "proposed-tech-lead")
        assert DEST_AGENT in after_approval

    def test_decision_labels_reject_unknown_destination_agent(self) -> None:
        config = make_config()
        with pytest.raises(ValueError, match="destination_agent"):
            decision_issue_labels(
                config,
                anchor_labels=[],
                agent_labels=("bug",),
                labels=LabelManager(config),
                destination_agent="agent:not-configured",
            )

    def test_decision_labels_reject_protected_agent_labels_loudly(self) -> None:
        config = make_config()
        with pytest.raises(ValueError, match="protected labels"):
            decision_issue_labels(
                config,
                anchor_labels=[],
                agent_labels=("needs-human",),
                labels=LabelManager(config),
                destination_agent=DEST_AGENT,
            )

    def test_priority_prefix_applied_once(self) -> None:
        config = make_config()
        config.tech_lead.priority = "P2"
        assert apply_tech_lead_priority_prefix(config, "Fix it") == "[P2-000] Fix it"
        assert (
            apply_tech_lead_priority_prefix(config, "[P1-042] Fix it") == "[P1-042] Fix it"
        )
        config.tech_lead.priority = None
        assert apply_tech_lead_priority_prefix(config, "Fix it") == "Fix it"

    @pytest.mark.parametrize(
        ("strategy", "expected"),
        [("earliest", 3), ("latest", 9), (None, None)],
    )
    def test_milestone_strategy(self, strategy, expected) -> None:
        config = make_config()
        config.tech_lead.milestone_strategy = MilestoneStrategyConfig(
            inherit_from_issues=strategy
        )
        intent = tech_lead_issue_milestone_intent(config, [(9, "M9"), (3, "M3")])
        assert intent.inherited_number == expected
        assert intent.explicit_name is None

    def test_explicit_strategy_yields_name_intent_not_a_lookup(self) -> None:
        """The explicit strategy plans a NAME; resolution belongs to the
        create-issue execution boundary, not planning (#6769 finding 4)."""
        config = make_config()
        config.tech_lead.milestone_strategy = MilestoneStrategyConfig(explicit="M5")
        intent = tech_lead_issue_milestone_intent(config, [(9, "M9")])
        assert intent == TechLeadMilestoneIntent(explicit_name="M5")

    def test_intent_rejects_carrying_both_shapes(self) -> None:
        with pytest.raises(ValueError, match="name OR a number"):
            TechLeadMilestoneIntent(explicit_name="M5", inherited_number=5)


class TestHealthReviewAnchorPolicy:
    """Health anchors are shaped by the SAME policy owner as batch anchors.

    Before #6763 finding 5 the health-review trigger hand-rolled its own label
    tuple, so it silently dropped ``tech_lead.explicit_labels``, the configured
    priority title, and the milestone strategy. The trigger now composes the
    anchor from ``tech_lead_issue_policy`` helpers; these tests pin that the
    health variant carries every configured behavior plus its marker.
    """

    def test_labels_include_agent_filter_explicit_and_marker(self) -> None:
        config = make_config()
        config.filtering.label = "io-scope"
        config.tech_lead.explicit_labels = ["needs-batch-review", "team:backend"]

        labels = health_review_issue_labels(config)

        assert labels == (
            "agent:tech-lead",
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
        assert "agent:tech-lead" in labels

    def test_health_anchor_labels_dedupe_case_insensitively(self) -> None:
        """An explicit label re-spelling the marker must not double it."""
        config = make_config()
        config.tech_lead.explicit_labels = [HEALTH_REVIEW_MARKER_LABEL.upper()]
        labels = health_review_issue_labels(config)
        assert sum(
            1 for label in labels if label.casefold() == HEALTH_REVIEW_MARKER_LABEL
        ) == 1

    def test_priority_title_shaping_applies_to_health_title(self) -> None:
        config = make_config()
        config.tech_lead.priority = "P1"
        assert (
            apply_tech_lead_priority_prefix(config, HEALTH_REVIEW_ISSUE_TITLE)
            == f"[P1-000] {HEALTH_REVIEW_ISSUE_TITLE}"
        )

    def test_explicit_milestone_intent_applies_with_no_source_prs(self) -> None:
        """Health anchors have no source PRs, so only the explicit strategy can
        apply; the intent carries the NAME for the applier to resolve."""
        config = make_config()
        config.tech_lead.milestone_strategy = MilestoneStrategyConfig(explicit="M9")
        intent = tech_lead_issue_milestone_intent(config, ())
        assert intent.explicit_name == "M9"
        assert intent.inherited_number is None

    def test_inherit_strategy_yields_no_milestone_without_source_prs(self) -> None:
        config = make_config()
        config.tech_lead.milestone_strategy = MilestoneStrategyConfig(
            inherit_from_issues="earliest"
        )
        intent = tech_lead_issue_milestone_intent(config, ())
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
        intent = TechLeadMilestoneIntent(explicit_name="M5")
        assert resolve_tech_lead_milestone_number(intent, self._milestones) == 5

    def test_number_and_none_intents_resolve_without_api_call(self) -> None:
        def _boom(_state: str):
            raise AssertionError("list_milestones must not be called")

        assert (
            resolve_tech_lead_milestone_number(
                TechLeadMilestoneIntent(inherited_number=7), _boom
            )
            == 7
        )
        assert resolve_tech_lead_milestone_number(TechLeadMilestoneIntent(), _boom) is None

    def test_unresolvable_name_fails_loudly(self) -> None:
        with pytest.raises(ValueError, match="does not match any"):
            resolve_tech_lead_milestone_number(
                TechLeadMilestoneIntent(explicit_name="Nope"), self._milestones
            )


class TestProposedTechLeadGate:
    """The proposed-tech-lead gate label (#6778): agent-rejected, owner-attached."""

    @pytest.mark.parametrize("label", ["proposed-tech-lead", "Proposed-Tech-Lead"])
    def test_agent_proposed_gate_label_is_rejected(self, label: str) -> None:
        config = make_config()
        labels = LabelManager(config)

        assert is_protected_tech_lead_label(label, config=config, labels=labels)
        assert protected_tech_lead_label_violations(
            [label], config=config, labels=labels
        ) == [label]

    def test_gate_flag_appends_orchestrator_attached_label(self) -> None:
        config = make_config()
        labels = LabelManager(config)

        composed = decision_issue_labels(
            config,
            anchor_labels=("agent:tech-lead",),
            agent_labels=("ci",),
            labels=labels,
            destination_agent=DEST_AGENT,
            gate=True,
        )

        assert composed[-1] == "proposed-tech-lead"
        assert "ci" in composed

    def test_gate_flag_defaults_off(self) -> None:
        config = make_config()
        labels = LabelManager(config)

        composed = decision_issue_labels(
            config,
            anchor_labels=("agent:tech-lead",),
            agent_labels=("ci",),
            labels=labels,
            destination_agent=DEST_AGENT,
        )

        assert "proposed-tech-lead" not in composed

    def test_gate_flag_never_launders_an_agent_proposed_gate(self) -> None:
        """Even with gate=True, an agent-proposed gate label still fails."""
        config = make_config()
        labels = LabelManager(config)

        with pytest.raises(ValueError, match="protected labels"):
            decision_issue_labels(
                config,
                anchor_labels=(),
                agent_labels=("proposed-tech-lead",),
                labels=labels,
                destination_agent=DEST_AGENT,
                gate=True,
            )


class TestTechLeadObservationLabel:
    """The pattern case-file observation label (#6781): agent-rejected,
    orchestrator-attached only, area-tagged."""

    @pytest.mark.parametrize(
        "label", ["tech-lead-observation", "Tech-Lead-Observation", "TECH-LEAD-OBSERVATION"]
    )
    def test_agent_proposed_observation_label_is_rejected(self, label: str) -> None:
        config = make_config()
        labels = LabelManager(config)

        assert is_protected_tech_lead_label(label, config=config, labels=labels)
        assert protected_tech_lead_label_violations(
            [label], config=config, labels=labels
        ) == [label]

    def test_case_file_labels_carry_scan_scope_and_observation(self) -> None:
        config = make_config()
        config.filtering.label = "io-scope"

        composed = case_file_issue_labels(config, area="db")

        assert composed == (
            "agent:tech-lead",
            "io-scope",
            TECH_LEAD_OBSERVATION_LABEL,
            "area:db",
        )

    def test_case_file_labels_omit_area_when_absent(self) -> None:
        config = make_config()
        config.filtering.label = "io-scope"

        composed = case_file_issue_labels(config, area=None)

        assert composed == ("agent:tech-lead", "io-scope", TECH_LEAD_OBSERVATION_LABEL)
        assert not any(label.startswith("area:") for label in composed)
