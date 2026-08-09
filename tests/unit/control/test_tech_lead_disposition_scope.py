"""Launch-scope contract for the ``defer_to_tracker`` disposition (#6971).

The disposition COMMENTS on and PARKS its target, so the target is held to the
same launch scope as any other routing proposal. Its tracker is deliberately
exempt: the orchestrator only reads that issue's open/closed state, and
requiring it inside the grant would block the one binding that makes the
disposition releasable — which is how an already-diagnosed issue would go back
to burning recovery budget.
"""

from __future__ import annotations

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


def test_the_tracker_may_be_any_open_issue_outside_the_grant() -> None:
    """A recovery mechanism issue is never inside a failure investigation's
    grant, and the orchestrator never writes to it."""
    assert _validate(_decision(_diagnosis(), _defer(tracker=99999))) is None
