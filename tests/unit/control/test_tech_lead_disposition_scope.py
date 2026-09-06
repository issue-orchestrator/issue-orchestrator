"""Immutable failure-only target and tracker grants for terminal outcomes."""

from __future__ import annotations

import pytest

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
