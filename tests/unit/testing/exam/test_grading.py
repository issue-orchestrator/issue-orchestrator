"""The exam grades OUTCOMES against each case's known right answer (#7304)."""

from __future__ import annotations

import json

import pytest

from issue_orchestrator.testing.exam import (
    PullRequestState,
    RunEnd,
    TechLeadActionDisposition,
    grade,
    render_summary,
)
from issue_orchestrator.testing.exam.cases import (
    BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
    HALTED_EXCHANGE_WITH_VALIDATED_WORK,
    blocked_issue_green_pr_awaiting_review,
    halted_exchange_with_validated_work,
)
from issue_orchestrator.testing.exam.scorecard import RemedyVerdict

from .builders import ISSUE, PR, action, item, observation, pr, run

CASE_A = halted_exchange_with_validated_work(
    code_reviewed_label="code-reviewed",
    blocked_failed_label="blocked-failed",
    needs_human_label="needs-human",
)
CASE_B = blocked_issue_green_pr_awaiting_review(blocked_failed_label="blocked-failed")

GOOD_DIAGNOSIS = (
    f"Issue #{ISSUE} carries blocked-failed, so its PR #{PR} never gets its"
    " code review even though CI is green."
)
GOOD_ESCALATION = (
    f"PR #{PR} is green and waiting on code review; remove blocked-failed from"
    f" #{ISSUE} so the review runs. Do not reset it."
)


class TestCaseAHaltedExchange:
    def test_fixed_engine_outcome_passes(self) -> None:
        subject = item(prs=(pr(state=PullRequestState.READY, labels=("code-reviewed",)),))

        card = grade(CASE_A, observation(HALTED_EXCHANGE_WITH_VALIDATED_WORK, subject))

        assert card.passed, card.failures
        assert card.stalls == ()

    def test_pre_fix_outcome_fails_and_reports_where_it_stalled(self) -> None:
        """The porchpin 09-23 shape: a recovered draft whose review the halt vetoes."""
        subject = item(
            issue_labels=("blocked-failed",),
            prs=(pr(state=PullRequestState.DRAFT),),
            gate="review_validity:issue_blocked (blocked-failed)",
        )

        card = grade(
            CASE_A,
            observation(HALTED_EXCHANGE_WITH_VALIDATED_WORK, subject, ended_by=RunEnd.QUIESCENT),
        )

        assert not card.passed
        failed = {goal.name for goal in card.goals if not goal.passed}
        assert failed == {
            "subject.pr_merged/ready",
            "subject.pr_label.code-reviewed",
            "subject.issue_free_of_blocks",
        }
        assert [(s.role, s.stall.refusing_gate) for s in card.stalls] == [
            ("subject", "review_validity:issue_blocked (blocked-failed)")
        ]
        summary = render_summary(card)
        assert "[FAIL]" in summary
        assert "refusing gate: review_validity:issue_blocked (blocked-failed)" in summary
        assert "ended by quiescent" in summary

    def test_no_published_pr_fails_every_pr_goal(self) -> None:
        card = grade(CASE_A, observation(HALTED_EXCHANGE_WITH_VALIDATED_WORK, item()))

        evidence = {goal.name: goal.evidence for goal in card.goals}
        assert evidence["subject.pr_merged/ready"] == f"issue #{ISSUE} has no pull request"
        assert not card.passed


class TestCaseBBlockedGreenPr:
    def _subject(self, **overrides: object) -> object:
        return item(
            issue_labels=("blocked-failed", "pr-pending"),
            prs=(overrides.pop("pr_fact", pr()),),
            gate="review_validity:issue_blocked (blocked-failed)",
        )

    def test_escalation_that_releases_the_review_is_acceptable_not_right(self) -> None:
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                self._subject(),
                runs=(run(action("escalate_to_human", GOOD_ESCALATION), summary=GOOD_DIAGNOSIS),),
            ),
        )

        assert card.passed, card.failures
        assert card.remedy is not None
        assert card.remedy.verdict is RemedyVerdict.ACCEPTABLE
        # Nothing in the action vocabulary can release a review yet.
        assert card.remedy.vocabulary_gap is True
        assert card.diagnosis is not None and card.diagnosis.passed

    def test_reset_retry_proposal_is_the_wrong_remedy(self) -> None:
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                self._subject(),
                runs=(
                    run(
                        action("escalate_to_human", GOOD_ESCALATION),
                        action(
                            "reset_retry",
                            "start over",
                            target=ISSUE,
                            disposition=TechLeadActionDisposition.PROPOSED,
                        ),
                        summary=GOOD_DIAGNOSIS,
                    ),
                ),
            ),
        )

        assert card.remedy is not None
        assert card.remedy.verdict is RemedyVerdict.WRONG
        assert not card.passed
        assert any(f.startswith("remedy wrong: reset_retry->#901") for f in card.failures)
        # Proposed, not executed, and the PR survived: nothing was destroyed.
        assert card.destructive == ()

    def test_executed_reset_that_closed_the_pr_is_destruction(self) -> None:
        closed = pr(state=PullRequestState.CLOSED_UNMERGED, branch_exists=False)
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                self._subject(pr_fact=closed),
                runs=(run(action("reset_retry", "reset", target=ISSUE), summary=GOOD_DIAGNOSIS),),
            ),
        )

        whats = [d.what for d in card.destructive]
        assert whats == [f"PR #{PR} closed unmerged", f"reset_retry executed on #{ISSUE} (run run-1)"]
        assert any(f.startswith("destructive:") for f in card.failures)
        assert any(f.startswith("goal subject.published_work_survives") for f in card.failures)

    def test_no_tech_lead_run_fails_diagnosis_and_remedy(self) -> None:
        card = grade(
            CASE_B,
            observation(BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW, self._subject(), ended_by=RunEnd.TIMEOUT),
        )

        assert "diagnosis: no tech-lead run completed" in card.failures
        assert card.remedy is not None and card.remedy.verdict is RemedyVerdict.MISSING
        assert card.stalls == ()  # the PR goals held; the tech lead is what failed

    def test_diagnosis_that_never_cites_the_item_fails(self) -> None:
        vague = "Some issue carries blocked-failed and its code review never runs."
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                self._subject(),
                runs=(run(action("post_comment", "board looks fine"), summary=vague),),
            ),
        )

        assert card.diagnosis is not None
        assert card.diagnosis.references_item is False
        assert "diagnosis: never cites the stuck issue/PR" in card.failures
        # The comment neither cites the item nor names the release: no remedy.
        assert card.remedy is not None and card.remedy.verdict is RemedyVerdict.MISSING

    def test_diagnosis_missing_a_concept_names_it(self) -> None:
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                self._subject(),
                runs=(run(summary=f"#{ISSUE} is stuck on blocked-failed."),),
            ),
        )

        assert card.diagnosis is not None
        assert card.diagnosis.missing == ("the withheld code review",)

    def test_rejected_escalation_is_not_an_acceptable_remedy(self) -> None:
        rejected = action(
            "escalate_to_human", GOOD_ESCALATION, disposition=TechLeadActionDisposition.REJECTED
        )
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                self._subject(),
                runs=(run(rejected, summary=GOOD_DIAGNOSIS, phase="failed"),),
            ),
        )

        assert card.remedy is not None and card.remedy.verdict is RemedyVerdict.MISSING

    def test_escalation_without_the_release_rationale_is_missing(self) -> None:
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                self._subject(),
                runs=(
                    run(
                        action("escalate_to_human", f"Please look at #{ISSUE}."),
                        summary=GOOD_DIAGNOSIS,
                    ),
                ),
            ),
        )

        assert card.remedy is not None and card.remedy.verdict is RemedyVerdict.MISSING


def test_scorecard_json_is_machine_readable() -> None:
    subject = item(
        issue_labels=("blocked-failed",),
        prs=(pr(),),
        gate="review_validity:issue_blocked (blocked-failed)",
    )
    card = grade(CASE_B, observation(BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW, subject))

    payload = json.loads(card.to_json())

    assert payload["schema_version"] == 1
    assert payload["case_id"] == BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW
    assert payload["passed"] is False
    assert payload["github_calls"] == {
        "total": 9,
        "by_class": {"search": 2, "graphql": 3, "core": 4, "other": 0},
        "top_commands": [
            {"command": "GET /repos/o/r/issues/901", "calls": 4},
            {"command": "POST /graphql", "calls": 3},
            {"command": "GET /search/issues", "calls": 2},
        ],
    }
    assert payload["remedy"]["verdict"] == "missing"
    assert payload["remedy"]["vocabulary_gap"] is True
    assert payload["diagnosis"]["tech_lead_ran"] is False
    assert payload["destructive_actions"] == []
    assert payload["elapsed_seconds"] == 321.0


def test_grading_refuses_an_observation_of_another_case() -> None:
    with pytest.raises(ValueError, match="not"):
        grade(CASE_A, observation(BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW, item()))
