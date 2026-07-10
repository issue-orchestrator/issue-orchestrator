"""Tests for triage decision -> orchestrator action mapping (ADR-0031)."""

import pytest

from issue_orchestrator.control.actions import (
    AddCommentAction,
    AddLabelAction,
    CreateTriageIssueAction,
    SurfaceTriageProposalAction,
)
from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.reconciliation import build_expected_for_mutation
from issue_orchestrator.control.triage_decision_actions import (
    plan_triage_decision_actions,
)
from issue_orchestrator.domain.models import Issue
from issue_orchestrator.domain.triage_artifacts import (
    ProposedTriageAction,
    TriageDecision,
    TriageFinding,
)
from issue_orchestrator.infra.config import Config


EXPECTED = build_expected_for_mutation()
NEEDS_HUMAN = "needs-human"


def _decision(*actions: ProposedTriageAction) -> TriageDecision:
    finding_ids = {ref for action in actions for ref in action.finding_ids}
    findings = tuple(
        TriageFinding(
            id=fid,
            title=f"Finding {fid}",
            classification="infra",
            evidence=("orchestrator log lines 10-20",),
        )
        for fid in sorted(finding_ids)
    )
    return TriageDecision(
        summary="summary",
        findings=findings,
        proposed_actions=tuple(actions),
    )


def _config(**authority_overrides: str) -> Config:
    config = Config()
    for key, value in authority_overrides.items():
        setattr(config.triage.authority, key, value)
    return config


def _anchor(number: int = 99, **overrides) -> Issue:
    fields = {
        "number": number,
        "title": f"Anchor issue {number}",
        "labels": ["agent:triage"],
        "repo": "owner/repo",
    }
    fields.update(overrides)
    return Issue(**fields)


def _plan(
    decision: TriageDecision,
    config: Config | None = None,
    anchor: Issue | None = None,
):
    config = config or _config()
    return plan_triage_decision_actions(
        decision,
        config,
        LabelManager(config),
        anchor_issue=anchor or _anchor(),
        expected=EXPECTED,
    )


def _shadow_digests(actions) -> list[AddCommentAction]:
    return [
        action
        for action in actions
        if isinstance(action, AddCommentAction)
        and "shadow mode" in action.comment
    ]


def test_post_comment_execute_maps_to_add_comment_with_provenance() -> None:
    action = ProposedTriageAction(
        id="A1",
        action_type="post_comment",
        target_number=42,
        target_is_pr=True,
        body="Diagnosis: flaky CI.",
        finding_ids=("T1", "T2"),
    )

    [planned] = _plan(_decision(action))

    assert isinstance(planned, AddCommentAction)
    assert planned.number == 42
    assert planned.is_pr is True
    assert planned.comment.startswith("Diagnosis: flaky CI.")
    assert planned.comment.endswith(
        "\n\n---\n*Proposed by triage session (action A1;"
        " findings: T1, T2) — ADR-0031.*"
    )
    assert "triage" in planned.reason and "A1" in planned.reason
    assert planned.expected is EXPECTED


def test_create_issue_execute_maps_to_create_triage_issue() -> None:
    action = ProposedTriageAction(
        id="A2",
        action_type="create_issue",
        title="Fix flaky CI runner",
        body="The runner disconnects mid-build.",
        labels=("bug",),
    )

    [planned] = _plan(_decision(action))

    assert isinstance(planned, CreateTriageIssueAction)
    assert planned.title == "Fix flaky CI runner"
    assert planned.body.startswith("The runner disconnects mid-build.")
    assert "(action A2; findings: none)" in planned.body
    assert "bug" in planned.labels
    assert planned.pr_count == 0
    assert planned.milestone is None
    assert "triage" in planned.reason and "A2" in planned.reason
    assert planned.expected is EXPECTED


class TestDecisionIssuePolicy:
    """Decision-created issues route through the triage: config owner (F4)."""

    def _issue_action(self, labels: tuple[str, ...] = ("bug",)) -> ProposedTriageAction:
        return ProposedTriageAction(
            id="A1",
            action_type="create_issue",
            title="Stabilize CI runner",
            body="Runner disconnects.",
            labels=labels,
        )

    def test_configured_labels_priority_and_scope_applied(self) -> None:
        config = _config()
        config.triage.explicit_labels = ["needs-batch-review"]
        config.triage.inherit_labels = ["team:backend", "not-on-anchor"]
        config.triage.priority = "P2"
        config.filtering.label = "io-scope"
        anchor = _anchor(labels=["agent:triage", "team:backend"])

        [planned] = _plan(_decision(self._issue_action()), config, anchor)

        assert isinstance(planned, CreateTriageIssueAction)
        assert planned.title.startswith("[P2-000] ")
        assert planned.labels == (
            "io-scope",
            "needs-batch-review",
            "team:backend",
            "bug",
        )

    def test_milestone_strategy_inherits_anchor_milestone(self) -> None:
        anchor = _anchor(milestone="M2", milestone_number=7)

        [planned] = _plan(_decision(self._issue_action()), _config(), anchor)

        assert isinstance(planned, CreateTriageIssueAction)
        assert planned.milestone == 7

    def test_protected_agent_labels_fail_loudly_at_planning(self) -> None:
        """Validation upstream must have rejected these; planning never
        silently forwards or filters a protected label."""
        with pytest.raises(ValueError, match="protected labels"):
            _plan(_decision(self._issue_action(labels=("in-progress",))))


def test_escalate_to_human_maps_to_routing_surface_only() -> None:
    """Escalation = needs-human label + comment; never EscalateToHumanAction.

    EscalateToHumanAction's applier terminates the target issue's runtime,
    which would give the always-execute escalation floor the same effect as
    the shadow-only kill_hung_session intent (#6764 authority hole).
    """
    action = ProposedTriageAction(
        id="A3",
        action_type="escalate_to_human",
        target_number=55,
        body="Session keeps looping.\nDetails follow.",
        finding_ids=("T1",),
    )

    [label, comment] = _plan(_decision(action))

    assert isinstance(label, AddLabelAction)
    assert label.issue_number == 55
    assert label.label == NEEDS_HUMAN
    assert label.expected is EXPECTED
    assert isinstance(comment, AddCommentAction)
    assert comment.number == 55
    assert comment.is_pr is False
    assert comment.comment.startswith("## ⚠️ Triage escalation")
    assert "Session keeps looping." in comment.comment
    assert "(action A3; findings: T1)" in comment.comment
    assert comment.expected is EXPECTED


def test_escalate_to_human_executes_even_in_full_propose_config() -> None:
    """escalate_to_human is the non-configurable floor: always executed."""
    config = _config(
        post_comment="propose", create_issue="propose", flag_pattern="propose"
    )
    action = ProposedTriageAction(
        id="A1",
        action_type="escalate_to_human",
        target_number=7,
        body="Needs a human.",
    )

    [label, comment] = _plan(_decision(action), config)

    assert isinstance(label, AddLabelAction)
    assert isinstance(comment, AddCommentAction)


def test_propose_authority_surfaces_shadow_proposal() -> None:
    config = _config(post_comment="propose")
    action = ProposedTriageAction(
        id="A1",
        action_type="post_comment",
        target_number=42,
        body="x" * 900,
        finding_ids=("T1",),
    )

    planned = _plan(_decision(action), config)

    [surfaced] = [a for a in planned if isinstance(a, SurfaceTriageProposalAction)]
    assert surfaced.mode == "shadow"
    assert surfaced.issue_number == 99  # anchor issue, not the target
    assert surfaced.action_id == "A1"
    assert surfaced.proposal_type == "post_comment"
    assert surfaced.target_number == 42
    assert surfaced.finding_ids == ("T1",)
    assert len(surfaced.body_preview) == 500  # capped at construction


def test_shadow_proposals_plan_durable_digest_comment() -> None:
    """Shadow records must reach the operator surface durably, not only as a
    trace event (#6761 finding 6)."""
    config = _config(post_comment="propose")
    action = ProposedTriageAction(
        id="A1",
        action_type="post_comment",
        target_number=42,
        body="Diagnosis for #42.",
        finding_ids=("T1",),
    )

    planned = _plan(_decision(action), config)

    [digest] = _shadow_digests(planned)
    assert digest.number == 99  # the anchor issue
    assert digest.is_pr is False
    assert "would-have-done" in digest.comment
    assert "A1" in digest.comment
    assert "post_comment" in digest.comment
    assert "#42" in digest.comment
    assert "Diagnosis for #42." in digest.comment
    assert "T1" in digest.comment
    assert digest.expected is EXPECTED
    # The digest complements the event-producing surface action.
    assert any(isinstance(a, SurfaceTriageProposalAction) for a in planned)


def test_execute_only_decision_plans_no_digest_comment() -> None:
    action = ProposedTriageAction(
        id="A1", action_type="post_comment", target_number=1, body="c"
    )

    planned = _plan(_decision(action))

    assert _shadow_digests(planned) == []


def test_flag_pattern_execute_surfaces_as_pattern() -> None:
    action = ProposedTriageAction(
        id="A4",
        action_type="flag_pattern",
        body="Three sessions hit the same 422.",
    )

    [planned] = _plan(_decision(action))

    assert isinstance(planned, SurfaceTriageProposalAction)
    assert planned.mode == "pattern"
    assert planned.proposal_type == "flag_pattern"
    assert planned.target_number == 0


def test_flag_pattern_propose_surfaces_as_shadow() -> None:
    """triage.authority.flag_pattern must not be dead config (#6761 F5)."""
    config = _config(flag_pattern="propose")
    action = ProposedTriageAction(
        id="A4",
        action_type="flag_pattern",
        body="Three sessions hit the same 422.",
    )

    planned = _plan(_decision(action), config)

    [surfaced] = [a for a in planned if isinstance(a, SurfaceTriageProposalAction)]
    assert surfaced.mode == "shadow"
    assert surfaced.proposal_type == "flag_pattern"
    assert len(_shadow_digests(planned)) == 1


@pytest.mark.parametrize("act_type", ["reset_retry", "kill_hung_session"])
def test_act_level_surfaces_shadow_even_with_propose_config(act_type: str) -> None:
    action = ProposedTriageAction(
        id="A5",
        action_type=act_type,
        target_number=13,
        body="Rationale.",
    )

    planned = _plan(_decision(action))

    [surfaced] = [a for a in planned if isinstance(a, SurfaceTriageProposalAction)]
    assert surfaced.mode == "shadow"
    assert surfaced.proposal_type == act_type
    assert surfaced.target_number == 13
    assert len(_shadow_digests(planned)) == 1


def test_mixed_decision_preserves_order_and_authority() -> None:
    config = _config(create_issue="propose")
    comment = ProposedTriageAction(
        id="A1", action_type="post_comment", target_number=1, body="c"
    )
    issue = ProposedTriageAction(
        id="A2", action_type="create_issue", title="t", body="b"
    )
    pattern = ProposedTriageAction(id="A3", action_type="flag_pattern", body="p")

    planned = _plan(_decision(comment, issue, pattern), config)

    assert isinstance(planned[0], AddCommentAction)
    assert isinstance(planned[1], SurfaceTriageProposalAction)
    assert planned[1].mode == "shadow"
    assert isinstance(planned[2], SurfaceTriageProposalAction)
    assert planned[2].mode == "pattern"
    # Shadow digest is appended after the proposal actions.
    assert len(_shadow_digests(planned)) == 1
    assert planned.index(_shadow_digests(planned)[0]) == len(planned) - 1


def test_authority_mode_for_unknown_action_raises() -> None:
    from issue_orchestrator.infra.config_models import TriageAuthorityConfig

    with pytest.raises(ValueError, match="unknown triage action type"):
        TriageAuthorityConfig().mode_for("merge_pr")


def test_authority_mode_for_escalate_is_always_execute() -> None:
    from issue_orchestrator.infra.config_models import TriageAuthorityConfig

    authority = TriageAuthorityConfig()
    assert authority.mode_for("escalate_to_human") == "execute"
