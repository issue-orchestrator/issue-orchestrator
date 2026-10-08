"""The Tech lead page section projection (#7763): producer -> payload.

Pure inputs in, the generated contract model out. Each test names the lane
rule it pins; the payload is also validated against the generated schema so a
projection that drifts from the contract fails here, not in the browser.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

from issue_orchestrator.adapters.github.github_issue import GitHubIssue
from issue_orchestrator.contracts.ui_openapi_models import TechLeadPageSectionPayload
from issue_orchestrator.domain.action_liveness import (
    ActionIdentity,
    LivenessKey,
    LivenessRow,
    OutcomeKind,
)
from issue_orchestrator.domain.human_block import NeedsHumanCause
from issue_orchestrator.domain.scoped_rework import ReworkRequest, ReworkTarget
from issue_orchestrator.domain.tech_lead_approval import (
    ApprovalVerdict,
    ApprovalVerdictKind,
)
from issue_orchestrator.domain.tech_lead_artifacts import TriageClass
from issue_orchestrator.domain.tech_lead_charter import CharterOutcome
from issue_orchestrator.domain.tech_lead_charter_decisions import (
    CharterDecisionSource,
    CharterExecutionResult,
    TechLeadCharterDecision,
    decision_key,
)
from issue_orchestrator.domain.tech_lead_session import (
    OperatorDecision,
    StoredTechLeadOp,
    TechLeadCaseFileSummary,
)
from issue_orchestrator.control.tech_lead_charter_policy import TechLeadCharterPolicy
from issue_orchestrator.infra.config import Config
from issue_orchestrator.ports.pending_work_claim_store import NeedsHumanCauseRow
from issue_orchestrator.view_models.tech_lead_page import (
    ReworkReceiptView,
    TechLeadPageInputs,
    build_tech_lead_page_section,
)
from tests.approval_helpers import CLAIMED, GATED

REPO = "porchpin/porchpin"
NOW = datetime(2026, 10, 3, 12, 0, tzinfo=timezone.utc)


def _issue(number, labels=GATED, *, title="t", created="2026-10-01T00:00:00Z", updated="", state="open"):
    return GitHubIssue(number=number, repo=REPO, title=title, labels=tuple(labels),
                       state=state, created_at=created, updated_at=updated or created)


def _op(op_type="reset_retry", target=5, **extra):
    return StoredTechLeadOp(op_type=op_type, target_issue_number=target,
                            rationale="Reset it: the branch is poisoned.\nMore detail.",
                            source_run_id="r", source_session_name="s", source_action_id="A1",
                            created_at="2026-10-01T00:00:00Z", **extra)


def _decision(action_id, kind="kill_hung_session", *, target=13, at=NOW.isoformat(), **changes):
    verdict = TechLeadCharterPolicy.from_config(Config()).decide(kind)
    record = TechLeadCharterDecision.from_verdict(
        verdict, decision_id=decision_key("run", action_id), source=CharterDecisionSource.DECISION,
        run_id="run", action_id=action_id, anchor_issue_number=99, target_number=target,
        target_is_pr=False, decided_at=at, tracks_proposal=False,
    )
    return replace(record, **changes)


def _inputs(**changes) -> TechLeadPageInputs:
    base = TechLeadPageInputs(
        repository=REPO, now=NOW, proposals=(), ops={}, rework_receipts=(),
        needs_human_causes=(), issues=(), tech_lead_needs_human_label="tech-lead-needs-human",
        blocked_numbers=frozenset(), decisions=(), parked=(), case_files=(),
        health_interval_minutes=60, last_health_review_at=0.0, latest_run=None, merge_statuses={},
        rulings={},
    )
    return replace(base, **changes)


def _section(**changes) -> TechLeadPageSectionPayload:
    section = build_tech_lead_page_section(_inputs(**changes))
    # Contract round trip: the payload the browser receives is schema-valid.
    return TechLeadPageSectionPayload.model_validate(section.model_dump(mode="json"))


def test_empty_engine_projects_an_empty_but_complete_section() -> None:
    section = _section()
    assert section.waiting_count == 0 and section.waiting == []
    assert section.run.has_run is False and "No tech-lead run" in section.run.label
    assert section.health_review.label.startswith("Every 60 min; none has run yet")


def test_every_proposal_kind_waits_with_its_recommendation_and_effect() -> None:
    decision = OperatorDecision(title="Split #7 into two", body="why", follow_ups=())
    section = _section(
        proposals=((_issue(10), None), (_issue(11), None), (_issue(12, title="Fix flaky test"), None)),
        ops={10: _op(), 11: _op("propose_decision", target=7, decision=decision)},
    )
    by_number = {item.number: item for item in section.waiting}
    assert section.waiting_count == 3
    reset = by_number[10]
    assert reset.kind == "proposal" and reset.operation == "reset_retry"
    assert reset.recommendation == "Reset it: the branch is poisoned."
    assert "from scratch" in reset.approval_effect and reset.can_approve and reset.can_decline
    assert reset.status == "awaiting_approval" and reset.link.endswith("/issues/10")
    assert by_number[11].recommendation == "Split #7 into two"
    assert "retries #7" in by_number[11].approval_effect
    follow_up = by_number[12]
    assert follow_up.operation == "follow_up" and follow_up.recommendation == "Fix flaky test"
    assert "work queue" in follow_up.approval_effect


def test_verified_approval_and_rejected_claim_show_their_status() -> None:
    approved = ApprovalVerdict(20, ApprovalVerdictKind.MAINTAINER, actor="octo")
    rejected = ApprovalVerdict(21, ApprovalVerdictKind.BOT_ACTOR, actor="io-bot[bot]")
    section = _section(proposals=((_issue(20, CLAIMED), approved), (_issue(21, CLAIMED), rejected)))
    by_number = {item.number: item for item in section.waiting}
    assert by_number[20].status == "approved" and not by_number[20].can_approve
    assert "@octo" in by_number[20].status_label
    assert by_number[21].status == "approval_not_accepted" and by_number[21].can_approve
    assert "automation" in by_number[21].status_label


def test_rework_proposal_carries_its_detail_and_receipt_locks_the_decision() -> None:
    target = ReworkTarget(repository=REPO, pr_number=94, issue_number=5, head_sha="a" * 40,
                          branch="b", pr_labels=(), issue_labels=())
    request = ReworkRequest(target=target, evidence_identity="ev", report="<b>report</b>", feedback="tighten")
    op = _op("request_rework", rework_request=request)
    waiting = _section(proposals=((_issue(30), None),), ops={30: op}).waiting[0]
    rows = {row.label: row.value for row in waiting.details}
    assert rows["PR"] == f"{REPO}#94" and rows["Expected head"] == "a" * 40
    assert rows["Review report"] == "<b>report</b>"
    assert waiting.can_approve and waiting.can_decline
    receipt = ReworkReceiptView(30, request.key, 5, "queued", "Queued behind existing work")
    locked = _section(proposals=((_issue(30), None),), ops={30: op}, rework_receipts=(receipt,)).waiting[0]
    assert locked.status == "executing" and "Queued behind existing work" in locked.status_label
    assert not locked.can_approve and not locked.can_decline


def test_receipt_without_an_open_proposal_lists_under_doing() -> None:
    receipt = ReworkReceiptView(31, "k", 5, "executing", "Running")
    doing = _section(rework_receipts=(receipt,)).doing
    assert [(row.action_kind, row.target_number, row.in_flight) for row in doing] == [("request_rework", 5, True)]


def test_merge_held_prs_and_hand_overs_wait_without_approve_buttons() -> None:
    section = _section(
        needs_human_causes=(
            NeedsHumanCauseRow(40, NeedsHumanCause.MERGE_ESCALATION.value, "branch protection blocks merge"),
            NeedsHumanCauseRow(41, NeedsHumanCause.TECH_LEAD_ESCALATION.value, "needs a credential"),
            NeedsHumanCauseRow(42, NeedsHumanCause.AGENT_COMPLETION.value, "agent asked"),
            NeedsHumanCauseRow(43, NeedsHumanCause.MERGE_DECISION.value, "pick the API shape before merge"),
        ),
        issues=(_issue(41, ("needs-human", "tech-lead-needs-human"), title="Rotate key"),
                _issue(42, ("needs-human",))),
    )
    by_number = {item.number: item for item in section.waiting}
    assert set(by_number) == {40, 41, 43}  # an agent's work question is not a hand-over
    assert by_number[43].kind == "merge_ready_pr"  # a merge decision waits on you (#7678)
    assert by_number[40].kind == "merge_ready_pr" and by_number[40].recommendation == "branch protection blocks merge"
    assert by_number[41].kind == "hand_over" and by_number[41].recommendation == "needs a credential"
    assert not any(item.can_approve or item.can_decline for item in section.waiting)


def test_waiting_is_oldest_first_and_closed_proposals_drop_out() -> None:
    section = _section(proposals=(
        (_issue(50, created="2026-10-02T00:00:00Z"), None),
        (_issue(51, created="2026-09-30T00:00:00Z"), None),
        (_issue(52, state="closed"), None),
    ))
    assert [item.number for item in section.waiting] == [51, 50]


def test_doing_lane_is_the_last_day_with_in_flight_first() -> None:
    old = (NOW - timedelta(days=2)).isoformat()
    recent = (NOW - timedelta(hours=1)).isoformat()
    section = _section(decisions=(
        _decision("A1", outcome=CharterOutcome.EXECUTED, execution=CharterExecutionResult.APPLIED,
                  execution_at=recent),
        _decision("A2", outcome=CharterOutcome.EXECUTED, execution=CharterExecutionResult.APPLIED,
                  execution_at=old, decided_at=old),
        _decision("A3", outcome=CharterOutcome.EXECUTED, execution=None, decided_at=old),
        _decision("A4", outcome=CharterOutcome.PROPOSED),
    ))
    assert [(row.decision_id.split(":")[-1], row.in_flight, row.outcome) for row in section.doing] == [
        ("A3", True, "in_flight"),
        ("A1", False, "applied"),
    ]


def test_parked_actions_name_their_issue() -> None:
    row = LivenessRow(
        key=LivenessKey(ActionIdentity("issue:60", "reset_retry_issue"), "fp", escalation_issue=60),
        attempts=3, first_failed_at=NOW, last_failed_at=NOW,
        last_outcome=OutcomeKind.PERMANENT, last_reason="refused", next_attempt_at=None, escalated=True,
    )
    parked = _section(parked=(row,)).parked
    assert parked[0].issue_number == 60 and parked[0].escalated and parked[0].link.endswith("/60")


def test_watching_lists_only_still_blocked_triage_latest_first_class() -> None:
    section = _section(
        blocked_numbers=frozenset({70}),
        decisions=(
            _decision("T1", "post_comment", target=70, at="2026-10-01T00:00:00+00:00",
                      triage_class=TriageClass.EXPLAINED, triage_fingerprint="f1"),
            _decision("T2", "propose_decision", target=70, at="2026-10-02T00:00:00+00:00",
                      triage_class=TriageClass.OPERATOR_DECISION, triage_fingerprint="f2"),
            _decision("T3", "post_comment", target=71, triage_class=TriageClass.EXPLAINED, triage_fingerprint="f3"),
        ),
        case_files=(TechLeadCaseFileSummary(80, "flaky exchange", comment_count=4, area="review-exchange"),),
        last_health_review_at=(NOW - timedelta(minutes=30)).timestamp(),
    )
    assert [(item.issue_number, item.triage_class) for item in section.triaged] == [(70, "operator_decision")]
    assert section.case_files[0].issue_number == 80 and section.case_files[0].area == "review-exchange"
    assert section.health_review.next_due_at.startswith("2026-10-03T12:30")


def test_an_approved_proposal_is_listed_but_not_counted_as_waiting_on_you() -> None:
    """"N waiting on you" counts what needs the operator; an approval the
    engine is acting on waits on the engine."""
    approved = ApprovalVerdict(20, ApprovalVerdictKind.MAINTAINER, actor="octo")
    section = _section(proposals=((_issue(20, CLAIMED), approved), (_issue(21), None)))

    assert {item.number for item in section.waiting} == {20, 21}
    assert section.waiting_count == 1


def test_an_admitted_follow_up_is_ordinary_work_not_waiting() -> None:
    """Once the engine admits it (#7763), a follow-up is worked like any
    other issue; it must not linger on the page as 'approved'."""
    approved = ApprovalVerdict(22, ApprovalVerdictKind.MAINTAINER, actor="octo")
    section = _section(proposals=((_issue(22, ("tech-lead-proposal", "approved")), approved),))

    assert section.waiting == [] and section.waiting_count == 0


def test_merge_held_pr_shows_mergeability_and_checks_and_drops_once_released() -> None:
    from issue_orchestrator.control.merge_hold_status import MergeHoldStatus

    rows = (
        NeedsHumanCauseRow(40, NeedsHumanCause.MERGE_ESCALATION.value, "branch protection blocks merge"),
        NeedsHumanCauseRow(43, NeedsHumanCause.MERGE_ESCALATION.value, "checks timed out"),
    )
    section = _section(
        needs_human_causes=rows,
        merge_statuses={
            40: MergeHoldStatus(40, "Add audit log", True, "blocked", "FAILURE"),
            43: MergeHoldStatus(43, "Merged since", False, "unknown", "not read"),
        },
    )

    [held] = section.waiting
    assert (held.number, held.title) == (40, "Add audit log")
    details = {row.label: row.value for row in held.details}
    assert details["Mergeability"] == "blocked" and details["Checks"] == "FAILURE"
    assert section.waiting_count == 1


def test_a_proposal_stripped_of_every_gate_label_still_waits() -> None:
    """r7 F1: the body marker keeps it a proposal, so the inbox shows it."""
    from issue_orchestrator.domain.tech_lead_approval import with_proposal_marker

    stripped = replace(_issue(30, ("agent:backend",)), body=with_proposal_marker("b"))
    section = _section(proposals=((stripped, None),))

    assert [item.number for item in section.waiting] == [30]
    assert section.waiting_count == 1


def test_every_waiting_item_shows_its_issues_active_standing_rulings() -> None:
    """#8141: the operator sees the rulings that bind each item's agents."""
    from issue_orchestrator.ports.standing_rulings import SyncedRulings
    from tests.standing_ruling_helpers import a_ruling

    ruling = a_ruling(text="Runtime stamping replaces the static symbol-walk checker.\n\nDetail.")
    synced = SyncedRulings((ruling,), "2026-10-04T12:00:00+00:00")
    rulings = {7: synced, 40: synced, 41: synced}
    section = _section(
        proposals=((_issue(10), None),),
        ops={10: _op("propose_decision", target=7,
                     decision=OperatorDecision(title="Rework #379", body="why", follow_ups=()))},
        needs_human_causes=(
            NeedsHumanCauseRow(40, NeedsHumanCause.MERGE_DECISION.value, "merge after the ruling"),
            NeedsHumanCauseRow(41, NeedsHumanCause.TECH_LEAD_ESCALATION.value, "needs a person"),
        ),
        issues=(_issue(41, ("needs-human", "tech-lead-needs-human")),),
        rulings=rulings,
    )

    expected = (f"Standing ruling {ruling.ruling_id}",
                "Runtime stamping replaces the static symbol-walk checker. (maintainer ruling; maintainer,"
                " in a test; as of 2026-10-04 12:00 UTC)")
    for item in section.waiting:
        rows = [(row.label, row.value) for row in item.details if row.label.startswith("Standing ruling")]
        assert rows == [expected], item.number


def test_an_item_without_rulings_shows_none() -> None:
    section = _section(proposals=((_issue(10), None),), ops={10: _op()}, rulings={})

    assert not any(row.label.startswith("Standing ruling") for row in section.waiting[0].details)


def test_a_decision_lists_its_steps_and_the_operator_checklist() -> None:
    """#8691: what approval executes beyond the item, typed, and what only the
    operator can do, as a checklist rather than prose in the body."""
    from issue_orchestrator.domain.decision_steps import DecisionFollowThrough, DecisionStep, DecisionStepKind

    follow_through = DecisionFollowThrough(
        steps=(DecisionStep(DecisionStepKind.SET_MILESTONE, 327, milestone="M1 - Surfaces"),
               DecisionStep(DecisionStepKind.CLOSE_SUPERSEDED_PROPOSAL, 445)),
        operator_steps=("Raise the CI ceiling in .github/workflows/ci.yml",),
    )
    decision = OperatorDecision(title="Split #326", body="why", follow_ups=())
    section = _section(
        proposals=((_issue(459), None), (_issue(460), None)),
        ops={459: _op("propose_decision", target=326, decision=decision, follow_through=follow_through),
             460: _op()},
    )

    by_number = {item.number: item for item in section.waiting}
    assert by_number[459].approval_steps == [
        "Moves #327 to milestone `M1 - Surfaces`.",
        "Closes proposal #445, which this decision supersedes.",
    ]
    assert by_number[459].operator_steps == ["Raise the CI ceiling in .github/workflows/ci.yml"]
    assert "runs the 2 step(s) listed" in by_number[459].approval_effect
    assert (by_number[460].approval_steps, by_number[460].operator_steps) == ([], [])


def _delivery(**changes):
    from issue_orchestrator.domain.integration_branch import IntegrationDeliveryView

    base = IntegrationDeliveryView(
        pr_number=600, url=f"https://github.com/{REPO}/pull/600", head="integration", base="main",
        integration_tip="a" * 40, ahead_by=7, merged_pr_numbers=(476, 479, 511),
        observed_at="2026-10-03T11:00:00+00:00",
    )
    return replace(base, **changes)


def test_the_integration_delivery_pr_waits_on_you_with_what_to_do() -> None:
    """#8144: the operator's one merge is listed and counted, never approvable."""
    section = _section(delivery=_delivery())
    assert section.waiting_count == 1
    (item,) = section.waiting
    assert item.kind == "delivery_pr" and item.status == "delivery_ready"
    assert item.number == 600 and item.link == f"https://github.com/{REPO}/pull/600"
    assert item.title == "Deliver integration to main"
    assert item.status_label == "Ready for you to merge"
    assert "3 pull request(s)" in item.recommendation
    assert "merge commit (not squash or rebase)" in item.approval_effect
    assert "Never delete integration" in item.approval_effect
    assert item.can_approve is False and item.can_decline is False
    assert item.waiting_since == "2026-10-03T11:00:00+00:00"
    assert {row.label: row.value for row in item.details} == {
        "Integration tip": "a" * 40,
        "Commits ahead": "7",
        "Pull requests": "#476, #479, #511",
    }


def test_no_delivery_pr_waits_when_nothing_awaits_delivery() -> None:
    assert _section(delivery=None).waiting == []
    item = _section(delivery=_delivery(merged_pr_numbers=())).waiting[0]
    assert {row.label: row.value for row in item.details}["Pull requests"] == "None listed"
