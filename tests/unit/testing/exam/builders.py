"""Synthetic exam observations for grader tests."""

from __future__ import annotations

from issue_orchestrator.testing.exam import (
    ExamObservation,
    GitHubCallCounts,
    PullRequestFact,
    PullRequestState,
    RunEnd,
    StallFacts,
    TechLeadActionDisposition,
    TechLeadActionFact,
    TechLeadReceipt,
    TechLeadRunFact,
    WorkItemFact,
)
from issue_orchestrator.testing.exam.cases import SUBJECT

ISSUE = 901
PR = 902

AUDIT = {
    "by_command": {
        "GET /repos/o/r/issues/901": 4,
        "GET /search/issues": 2,
        "POST /graphql": 3,
    }
}


def pr(
    *,
    state: PullRequestState = PullRequestState.DRAFT,
    labels: tuple[str, ...] = ("needs-code-review",),
    branch_exists: bool = True,
    number: int = PR,
) -> PullRequestFact:
    return PullRequestFact(
        number=number,
        state=state,
        labels=frozenset(labels),
        branch=f"{ISSUE}-exam",
        branch_exists=branch_exists,
        checks="SUCCESS",
    )


def stall(*, gate: str = "", labels: tuple[str, ...] = ()) -> StallFacts:
    return StallFacts(
        last_transition="review_exchange.completed",
        last_transition_at="2026-09-26T12:00:00Z",
        refusing_gate=gate,
        blocking_labels=labels,
        unanswered_screen="",
        parked_screen="",
    )


def item(
    *,
    issue_labels: tuple[str, ...] = (),
    prs: tuple[PullRequestFact, ...] = (),
    gate: str = "",
    approved: tuple[int, ...] | None = None,
    events: tuple[str, ...] = (),
) -> WorkItemFact:
    """``approved`` defaults to every READY/MERGED PR (as a completed review
    leaves it); pass ``()`` to model a PR nobody reviewed."""
    return WorkItemFact(
        role=SUBJECT,
        issue_number=ISSUE,
        issue_state="open",
        issue_labels=frozenset(issue_labels),
        pull_requests=prs,
        stall=stall(gate=gate, labels=tuple(label for label in issue_labels if "block" in label)),
        events=events,
        approved_prs=frozenset(
            approved
            if approved is not None
            else (p.number for p in prs if p.state in (PullRequestState.READY, PullRequestState.MERGED))
        ),
    )


def action(
    action_type: str,
    body: str,
    *,
    target: int | None = ISSUE,
    disposition: TechLeadActionDisposition = TechLeadActionDisposition.EXECUTED,
) -> TechLeadActionFact:
    return TechLeadActionFact(
        action_type=action_type, target_number=target, body=body, disposition=disposition
    )


def run(
    *actions: TechLeadActionFact,
    summary: str = "",
    phase: str = "completed",
    run_id: str = "run-1",
    anchor: int = ISSUE,
) -> TechLeadRunFact:
    return TechLeadRunFact(
        run_id=run_id,
        anchor_issue_number=anchor,
        flavor="health_review",
        phase=phase,
        detail="",
        summary=summary,
        findings_text="",
        report_text="",
        actions=tuple(actions),
    )


def observation(
    case_id: str,
    subject: WorkItemFact,
    *,
    runs: tuple[TechLeadRunFact, ...] = (),
    receipts: tuple[TechLeadReceipt, ...] = (),
    owned: frozenset[int] | None = None,
    repeating: tuple = (),
    ended_by: RunEnd = RunEnd.GOAL_REACHED,
) -> ExamObservation:
    """``owned`` defaults to the subject and its PRs, as the harness gathers it."""
    return ExamObservation(
        case_id=case_id,
        engine_commit="c3784fe0000000000000000000000000000000000",
        items=(subject,),
        tech_lead_runs=runs,
        tech_lead_receipts=receipts,
        repeating_failures=repeating,
        owned_numbers=owned
        if owned is not None
        else frozenset({subject.issue_number, *(p.number for p in subject.pull_requests)}),
        github_calls=GitHubCallCounts.between(None, AUDIT),
        elapsed_seconds=321.0,
        ended_by=ended_by,
    )
