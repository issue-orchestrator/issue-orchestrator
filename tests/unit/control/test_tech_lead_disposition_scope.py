"""Immutable failure-only target and tracker grants for terminal outcomes."""

from __future__ import annotations

import pytest
from issue_orchestrator.control.tech_lead_completion_obligations import build_investigation_obligation

from issue_orchestrator.control.label_manager import LabelManager
from issue_orchestrator.control.tech_lead_completion import (
    validate_decision_for_authority,
)
from issue_orchestrator.domain.tech_lead_artifacts import (
    ProposedTechLeadAction,
    TechLeadDecision,
    TechLeadFinding,
)
from issue_orchestrator.domain.tech_lead_session import (
    TechLeadLaunchAuthority,
    TechLeadSessionFlavor,
)
from issue_orchestrator.infra.config import Config

FOCUS = 6410
ANCHOR = 7000
TRACKER = 6914


def _authority() -> TechLeadLaunchAuthority:
    return TechLeadLaunchAuthority(
        flavor=TechLeadSessionFlavor.FAILURE_INVESTIGATION,
        anchor_issue_number=ANCHOR,
        focus_issue_number=FOCUS,
        recovery_tracker_numbers=(TRACKER,),
    )


def _decision(*actions: ProposedTechLeadAction) -> TechLeadDecision:
    return TechLeadDecision(
        summary="s",
        findings=(
            TechLeadFinding(
                id="T1",
                title="Validated work is stranded",
                classification="infra",
                evidence=("orchestrator log lines 10-20",),
            ),
        ),
        proposed_actions=actions,
    )


def _diagnosis() -> ProposedTechLeadAction:
    """The mandatory focus-issue comment every investigation must publish."""
    return ProposedTechLeadAction(
        id="A1",
        action_type="post_comment",
        target_number=FOCUS,
        body="Diagnosis: the branch holds validated work.",
        finding_ids=("T1",),
    )


def _defer(target: int = FOCUS, tracker: int = TRACKER) -> ProposedTechLeadAction:
    return ProposedTechLeadAction(
        id="A2",
        action_type="defer_to_tracker",
        target_number=target,
        tracker_number=tracker,
        body="Recovery is owned by the recovery lane.",
        finding_ids=("T1",),
    )


def _validate(decision: TechLeadDecision) -> str | None:
    config = Config()
    return validate_decision_for_authority(
        decision, _authority(), config=config, labels=LabelManager(config)
    )


def test_deferring_the_focus_issue_is_in_scope() -> None:
    assert _validate(_decision(_diagnosis(), _defer())) is None


def test_deferring_someone_elses_issue_is_rejected() -> None:
    error = _validate(_decision(_diagnosis(), _defer(target=9999)))

    assert error is not None
    assert "outside this session's launch scope" in error
    assert "defer_to_tracker" in error


def test_an_ungranted_tracker_is_rejected() -> None:
    assert "launch-granted" in _validate(_decision(_diagnosis(), _defer(tracker=99999)))


@pytest.mark.parametrize(
    "flavor", [TechLeadSessionFlavor.HEALTH_REVIEW, TechLeadSessionFlavor.BATCH_REVIEW]
)
def test_other_session_flavors_cannot_defer(flavor):
    config = Config()
    authority = TechLeadLaunchAuthority(flavor=flavor, anchor_issue_number=FOCUS)
    error = validate_decision_for_authority(
        _decision(_defer()), authority, config=config, labels=LabelManager(config)
    )
    assert "only for a failure investigation" in error


@pytest.mark.parametrize(
    "actions",
    [
        (),
        (_defer(), _defer()),
        (
            _defer(),
            ProposedTechLeadAction(
                id="A3",
                action_type="escalate_to_human",
                target_number=FOCUS,
                body="human",
            ),
        ),
    ],
)
def test_missing_or_conflicting_terminal_outcomes_fail(actions):
    assert "exactly one terminal disposition" in _validate(
        _decision(_diagnosis(), *actions)
    )


@pytest.mark.parametrize(
    "remedy", ["reset_retry", "escalate_to_human"]
)
def test_immediate_remedies_and_human_handoff_are_terminal(remedy):
    action = ProposedTechLeadAction(
        id="A2", action_type=remedy, target_number=FOCUS, body="remedy"
    )
    assert _validate(_decision(_diagnosis(), action)) is None


def test_kill_without_observed_generation_cannot_be_a_terminal_remedy():
    action = ProposedTechLeadAction(id="A2", action_type="kill_hung_session", target_number=FOCUS, body="remedy")
    assert "launch-observed worker generation" in _validate(_decision(_diagnosis(), action))


@pytest.mark.parametrize("target_is_pr", [False, True])
def test_focus_diagnosis_kind_cannot_escape_producer_to_terminal_requirement(target_is_pr):
    from dataclasses import replace
    from issue_orchestrator.control.actions import ActionResult, AddCommentAction, RecordTechLeadDispositionAction
    from issue_orchestrator.control.required_issue_comment import RequiredIssueCommentAction
    from issue_orchestrator.control.reconciliation import build_expected_for_mutation
    from issue_orchestrator.control.tech_lead_completion_gate import (
        require_investigation_terminal_effect, evaluate_required_act_level_outcome,
    )
    from issue_orchestrator.control.tech_lead_decision_actions import plan_tech_lead_decision_actions
    from issue_orchestrator.control.proposal_dedup_gate import DuplicateTargetGrant, OpenIssueCorpus
    from issue_orchestrator.domain.models import Issue
    decision = _decision(replace(_diagnosis(), target_is_pr=target_is_pr), _defer())
    violation = _validate(decision)
    if target_is_pr:
        assert "not a PR" in violation
    else:
        assert violation is None
    # Even bypassing validation cannot make the agent's kind flag weaken the
    # immutable focus obligation at the planner/completion boundary.
    config = Config()
    planned = plan_tech_lead_decision_actions(decision, config, LabelManager(config),
        anchor_issue=Issue(number=ANCHOR, title="investigation", labels=[]),
        expected=build_expected_for_mutation(), op_ledger={}, pattern_ledger={},
        source_run_id="run", source_session_name="session", observed_at="2026-08-09T00:00:00+00:00",
        observed_session_generation=lambda number: None,
        dedup_corpus=OpenIssueCorpus.disabled(), dedup_grant=DuplicateTargetGrant.none())
    actions = require_investigation_terminal_effect(planned, obligation=build_investigation_obligation(
        decision, focus_issue_number=_authority().focus_issue_number))
    [diagnosis] = [action for action in actions if isinstance(action, AddCommentAction)]
    [remedy] = [action for action in actions if isinstance(action, RecordTechLeadDispositionAction)]
    assert isinstance(diagnosis, RequiredIssueCommentAction) is not target_is_pr
    assert remedy in actions
    assert evaluate_required_act_level_outcome([
        ActionResult.fail(action, "diagnosis failed") if action is diagnosis else ActionResult.ok(action)
        for action in actions
    ]).failed


@pytest.mark.parametrize("conflict", ["same-decision", "persisted"])
def test_planning_rejection_cannot_erase_investigation_obligations(conflict):
    from unittest.mock import MagicMock
    from issue_orchestrator.control.action_applier import ActionApplier
    from issue_orchestrator.control.actions import AddCommentAction
    from issue_orchestrator.control.reconciliation import build_expected_for_mutation
    from issue_orchestrator.control.tech_lead_completion_gate import require_investigation_terminal_effect
    from issue_orchestrator.control.tech_lead_decision_actions import plan_tech_lead_decision_actions
    from issue_orchestrator.control.proposal_dedup_gate import DuplicateTargetGrant, OpenIssueCorpus
    from issue_orchestrator.control.tech_lead_reset_retry import (
        apply_completion_actions_gated, evaluate_required_act_level_outcome, effective_terminal_status,
    )
    from issue_orchestrator.control.tech_lead_actions import TechLeadPlanningFailureAction
    from issue_orchestrator.domain.models import Issue, SessionStatus
    from issue_orchestrator.domain.tech_lead_findings import PatternEvidence
    human = ProposedTechLeadAction(id="A3", action_type="flag_pattern", body="needs operator",
        pattern_signature="incident", fix_class="human")
    code = ProposedTechLeadAction(id="A4", action_type="flag_pattern", body="requires fix",
        pattern_signature="incident", fix_class="code")
    decision = _decision(_diagnosis(), _defer(), *([human, code] if conflict == "same-decision" else [code]))
    decision.validate()
    assert _validate(decision) is None
    config = Config()
    config.tech_lead.authority.flag_pattern = "execute"
    pattern_ledger = {} if conflict == "same-decision" else {"incident": PatternEvidence(
        signature="incident", case_file_issue_number=7001, observation_count=1, fix_class="human")}
    lowered = plan_tech_lead_decision_actions(decision, config, LabelManager(config),
        anchor_issue=Issue(number=ANCHOR, title="investigation", labels=[]),
        expected=build_expected_for_mutation(), op_ledger={}, pattern_ledger=pattern_ledger,
        source_run_id="run", source_session_name="session", observed_at="2026-08-09T00:00:00+00:00",
        observed_session_generation=lambda number: None,
        dedup_corpus=OpenIssueCorpus.disabled(), dedup_grant=DuplicateTargetGrant.none())
    assert len(lowered) == 1 and isinstance(lowered[0], TechLeadPlanningFailureAction)
    host = MagicMock()
    applier = ActionApplier(labels=MagicMock(), sessions=MagicMock(), events=MagicMock(), repository_host=host)
    planned = require_investigation_terminal_effect(lowered, obligation=build_investigation_obligation(
        decision, focus_issue_number=FOCUS))
    results, error = apply_completion_actions_gated(applier,
        [*planned, AddCommentAction(number=FOCUS, comment="success-only")], issue_number=FOCUS)
    assert error is None
    outcome = evaluate_required_act_level_outcome(results)
    assert outcome.failed
    assert effective_terminal_status(SessionStatus.COMPLETED, outcome) is SessionStatus.FAILED
    host.add_comment.assert_not_called()
    assert any("pattern_classification_conflict" in reason for reason in outcome.failures)


def test_empty_lowering_cannot_satisfy_trusted_investigation_obligations():
    from unittest.mock import MagicMock
    from issue_orchestrator.control.action_applier import ActionApplier
    from issue_orchestrator.control.actions import AddCommentAction
    from issue_orchestrator.control.tech_lead_completion_gate import require_investigation_terminal_effect
    from issue_orchestrator.control.tech_lead_reset_retry import apply_completion_actions_gated, evaluate_required_act_level_outcome
    host = MagicMock()
    applier = ActionApplier(labels=MagicMock(), sessions=MagicMock(), events=MagicMock(), repository_host=host)
    planned = require_investigation_terminal_effect([], obligation=build_investigation_obligation(
        _decision(_diagnosis(), _defer()), focus_issue_number=FOCUS))
    results, error = apply_completion_actions_gated(applier,
        [*planned, AddCommentAction(number=FOCUS, comment="success-only")], issue_number=FOCUS)
    assert error is None and evaluate_required_act_level_outcome(results).failed
    host.add_comment.assert_not_called()
