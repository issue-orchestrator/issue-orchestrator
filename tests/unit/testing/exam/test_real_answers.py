"""Grade real tech-lead output, and re-grade saved observations.

``case-b-tech-lead-decision.json`` is the decision the real tech lead (opus,
production prompt and authority) wrote in the exam's Case B run on main:
issue #7316 blocked, its PR #7317 green and awaiting review. It is the RIGHT
answer written in markdown prose, so it pins that the grader recognises a
right answer phrased the way tech leads actually write.
"""

from __future__ import annotations

import json
from pathlib import Path

from issue_orchestrator.testing.exam import (
    ExamObservation,
    GitHubCallCounts,
    PullRequestFact,
    PullRequestState,
    RunEnd,
    StallFacts,
    TechLeadActionDisposition,
    TechLeadActionFact,
    TechLeadRunFact,
    WorkItemFact,
    grade,
)
from issue_orchestrator.testing.exam.cases import (
    BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
    SUBJECT,
    blocked_issue_green_pr_awaiting_review,
)
from issue_orchestrator.testing.exam.scorecard import RemedyVerdict

DECISION = Path(__file__).parent / "fixtures" / "case-b-tech-lead-decision.json"
CASE_B = blocked_issue_green_pr_awaiting_review(blocked_failed_label="blocked-failed")


def _real_run() -> TechLeadRunFact:
    decision = json.loads(DECISION.read_text(encoding="utf-8"))
    decision = decision.get("decision", decision)
    return TechLeadRunFact(
        run_id="real-b",
        anchor_issue_number=7316,
        flavor="failure_investigation",
        phase="needs_human",
        detail="",
        summary=decision["summary"],
        findings_text="\n".join(f["title"] for f in decision["findings"]),
        report_text="",
        actions=tuple(
            TechLeadActionFact(
                action_type=a["action_type"],
                target_number=a.get("target_number"),
                body=a.get("body", ""),
                disposition=TechLeadActionDisposition.EXECUTED,
            )
            for a in decision["proposed_actions"]
        ),
    )


def _observation(runs: tuple[TechLeadRunFact, ...]) -> ExamObservation:
    subject = WorkItemFact(
        role=SUBJECT,
        issue_number=7316,
        issue_state="open",
        issue_labels=frozenset({"blocked-failed", "pr-pending", "needs-human"}),
        pull_requests=(
            PullRequestFact(
                number=7317,
                state=PullRequestState.DRAFT,
                labels=frozenset({"needs-code-review"}),
                branch="7316-exam-b-green-pr-awaiting-review",
                branch_exists=True,
                checks="SUCCESS",
            ),
        ),
        stall=StallFacts(
            last_transition="tech_lead.action_executed",
            last_transition_at="t",
            refusing_gate="review_validity:issue_blocked (blocked-failed, needs-human)",
            blocking_labels=("blocked-failed", "needs-human"),
            unanswered_screen="",
            parked_screen="",
        ),
        events=("tech_lead.run_requested", "tech_lead.action_executed"),
        approved_prs=frozenset(),
    )
    return ExamObservation(
        case_id=BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
        engine_commit="d5e487e",
        items=(subject,),
        tech_lead_runs=runs,
        tech_lead_receipts=(),
        owned_numbers=frozenset({7316, 7317}),
        github_calls=GitHubCallCounts.between(None, {"by_command": {"GET /search/issues": 1}}),
        elapsed_seconds=900.0,
        ended_by=RunEnd.TECH_LEAD_CONCLUDED,
        notes=("n",),
    )


def test_the_real_right_answer_passes() -> None:
    card = grade(CASE_B, _observation((_real_run(),)))

    assert card.passed, card.failures
    assert card.diagnosis is not None and card.diagnosis.missing == ()
    assert "blocked-failed" in card.diagnosis.evidence_clause
    assert card.remedy is not None
    assert card.remedy.verdict is RemedyVerdict.ACCEPTABLE
    assert "escalate_to_human->#7316 (executed)" in card.remedy.evidence


def test_markdown_does_not_hide_a_named_concept() -> None:
    from issue_orchestrator.testing.exam import TermGroup

    group = TermGroup("release", ("remove blocked-failed", "issue blocked"))
    assert group.matched_term("Please **remove `blocked-failed`** now") == "remove blocked-failed"
    assert group.matched_term("review_validity says issue_blocked") == "issue blocked"
    assert group.matched_term("nothing relevant") is None


def test_a_saved_observation_regrades_to_the_same_scorecard() -> None:
    observation = _observation((_real_run(),))

    restored = ExamObservation.from_dict(json.loads(json.dumps(observation.to_dict())))

    assert restored == observation
    assert grade(CASE_B, restored).to_dict() == grade(CASE_B, observation).to_dict()
