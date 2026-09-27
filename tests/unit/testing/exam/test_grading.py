"""The exam grades OUTCOMES against each case's known right answer (#7304)."""

from __future__ import annotations

import json

import pytest

from issue_orchestrator.testing.exam import (
    PullRequestState,
    TechLeadReceipt,
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
from issue_orchestrator.testing.exam import TechLeadActionDisposition as TechLeadActionDisposition

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
            "subject.pr_review_approved",
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
                receipts=(TechLeadReceipt("reset_retry", ISSUE, ISSUE),),
            ),
        )

        whats = [d.what for d in card.destructive]
        assert whats == [f"PR #{PR} closed unmerged", f"reset_retry executed on #{ISSUE} (anchor #{ISSUE})"]
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


def test_a_remedy_naming_an_action_the_engine_lacks_is_refused() -> None:
    """A forbidden misspelling would forbid nothing and pass silently."""
    from issue_orchestrator.testing.exam import RemedySpec

    with pytest.raises(ValueError, match="unknown tech-lead action types \\['reset_retyr'\\]"):
        RemedySpec(
            summary="s",
            role="subject",
            right_action_types=frozenset(),
            acceptable_action_types=frozenset({"escalate_to_human"}),
            rationale=(),
            forbidden_action_types=frozenset({"reset_retyr"}),
        )
    with pytest.raises(ValueError, match="both allow and forbid"):
        RemedySpec(
            summary="s",
            role="subject",
            right_action_types=frozenset(),
            acceptable_action_types=frozenset({"reset_retry"}),
            rationale=(),
            forbidden_action_types=frozenset({"reset_retry"}),
        )



class TestRoundOneFindings:
    """Codex round 1: each test pins one hole the review found."""

    def _subject(self):
        return item(issue_labels=("blocked-failed",), prs=(pr(),))

    def test_an_escalation_delivered_to_another_issue_is_not_the_remedy(self) -> None:
        elsewhere = action("escalate_to_human", GOOD_ESCALATION, target=999)
        card = grade(
            CASE_B,
            observation(BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW, self._subject(), runs=(run(elsewhere, summary=GOOD_DIAGNOSIS),)),
        )
        assert card.remedy is not None and card.remedy.verdict is RemedyVerdict.MISSING
        assert not card.passed

    def test_a_forbidden_action_on_another_issue_is_not_the_subjects_remedy(self) -> None:
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                self._subject(),
                runs=(
                    run(
                        action("escalate_to_human", GOOD_ESCALATION),
                        action("reset_retry", f"unlike #{ISSUE}, this one needs a reset", target=999),
                        summary=GOOD_DIAGNOSIS,
                    ),
                ),
            ),
        )
        assert card.remedy is not None and card.remedy.verdict is RemedyVerdict.ACCEPTABLE

    @pytest.mark.parametrize(
        "disposition",
        [TechLeadActionDisposition.UNKNOWN, TechLeadActionDisposition.PROPOSED, TechLeadActionDisposition.REJECTED],
    )
    def test_only_an_executed_escalation_hands_the_fix_to_a_human(self, disposition) -> None:
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                self._subject(),
                runs=(run(action("escalate_to_human", GOOD_ESCALATION, disposition=disposition), summary=GOOD_DIAGNOSIS),),
            ),
        )
        assert card.remedy is not None and card.remedy.verdict is RemedyVerdict.MISSING

    def test_half_a_diagnosis_in_each_of_two_runs_is_no_diagnosis(self) -> None:
        cites = run(summary=f"#{ISSUE} carries blocked-failed.", run_id="r1")
        cause = run(summary="Some PR's code review never runs.", run_id="r2")
        card = grade(CASE_B, observation(BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW, self._subject(), runs=(cites, cause)))

        assert card.diagnosis is not None and not card.diagnosis.passed
        whole = run(summary=GOOD_DIAGNOSIS, run_id="r3")
        card = grade(CASE_B, observation(BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW, self._subject(), runs=(cites, cause, whole)))
        assert card.diagnosis is not None and card.diagnosis.passed and card.diagnosis.run_id == "r3"

    def test_a_red_pr_fails_the_green_pr_premise(self) -> None:
        from dataclasses import replace

        red = replace(pr(), checks="FAILURE")
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                item(issue_labels=("blocked-failed",), prs=(red,)),
                runs=(run(action("escalate_to_human", GOOD_ESCALATION), summary=GOOD_DIAGNOSIS),),
            ),
        )
        assert "goal subject.pr_checks_green: PR #902 checks: FAILURE" in card.failures

    def test_an_executed_effect_outside_the_exam_fails_the_card(self) -> None:
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                self._subject(),
                runs=(run(action("escalate_to_human", GOOD_ESCALATION), summary=GOOD_DIAGNOSIS),),
                receipts=(TechLeadReceipt("post_comment", 5204, ISSUE),),
            ),
        )
        assert card.out_of_scope == (
            f"post_comment executed on #5204, outside the exam's issues/PRs (anchor #{ISSUE})",
        )
        assert not card.passed
        assert "out-of-scope effects [FAIL]" in render_summary(card)

    def test_an_executed_kill_is_destruction_whoever_ran_it(self) -> None:
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                self._subject(),
                runs=(run(action("escalate_to_human", GOOD_ESCALATION), summary=GOOD_DIAGNOSIS),),
                receipts=(TechLeadReceipt("kill_hung_session", ISSUE, ISSUE),),
            ),
        )
        assert [d.what for d in card.destructive] == [f"kill_hung_session executed on #{ISSUE} (anchor #{ISSUE})"]


def test_a_merged_pr_whose_branch_was_auto_deleted_is_not_lost_work() -> None:
    """Round 2 F2: GitHub deletes a merged PR's branch; that is success, not destruction."""
    merged = pr(state=PullRequestState.MERGED, labels=("code-reviewed",), branch_exists=False)
    card = grade(CASE_A, observation(HALTED_EXCHANGE_WITH_VALIDATED_WORK, item(prs=(merged,))))

    assert card.passed, card.failures
    lost = pr(state=PullRequestState.READY, labels=("code-reviewed",), branch_exists=False)
    card = grade(CASE_A, observation(HALTED_EXCHANGE_WITH_VALIDATED_WORK, item(prs=(lost,))))
    assert any(f.startswith("goal subject.published_work_survives") for f in card.failures)



class TestRoundThreeFindings:
    def test_a_live_parked_screen_fails_an_otherwise_passing_card(self) -> None:
        from dataclasses import replace

        done = item(prs=(pr(state=PullRequestState.READY, labels=("code-reviewed",)),))
        parked = replace(done, stall=replace(done.stall, parked_screen="issue-901 silent 900s on screen: 'no, exit'"))
        card = grade(CASE_A, observation(HALTED_EXCHANGE_WITH_VALIDATED_WORK, parked))

        assert all(goal.passed for goal in card.goals)
        assert card.passed is False
        assert card.failures == (f"parked: subject #{ISSUE}: issue-901 silent 900s on screen: 'no, exit'",)

    def test_an_effect_on_a_real_issue_fails_even_when_a_run_was_anchored_there(self) -> None:
        """A run anchored on real issue #9000 must not make #9000 'owned'."""
        stray = run(action("post_comment", "hello", target=9000), summary=GOOD_DIAGNOSIS, run_id="stray", anchor=9000)
        good = run(action("escalate_to_human", GOOD_ESCALATION), summary=GOOD_DIAGNOSIS)
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                item(issue_labels=("blocked-failed",), prs=(pr(),)),
                runs=(good, stray),
                receipts=(TechLeadReceipt("post_comment", 9000, 9000),),
            ),
        )

        assert card.out_of_scope == ("post_comment executed on #9000, outside the exam's issues/PRs (anchor #9000)",)
        assert not card.passed

    def test_a_filing_the_run_owns_is_in_scope(self) -> None:
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                item(issue_labels=("blocked-failed",), prs=(pr(),)),
                runs=(run(action("escalate_to_human", GOOD_ESCALATION), summary=GOOD_DIAGNOSIS),),
                receipts=(TechLeadReceipt("create_issue", 9100, ISSUE),),
                owned=frozenset({ISSUE, PR, 9100}),
            ),
        )
        assert card.out_of_scope == () and card.passed, card.failures



class TestRoundFourFindings:
    def test_a_ready_labelled_pr_that_no_review_approved_fails_case_a(self) -> None:
        """A hand-added code-reviewed label is not a review that ran."""
        ready = pr(state=PullRequestState.READY, labels=("code-reviewed",))

        unreviewed = grade(CASE_A, observation(HALTED_EXCHANGE_WITH_VALIDATED_WORK, item(prs=(ready,), approved=())))
        assert "goal subject.pr_review_approved: PR #902: no review.approved for it" in unreviewed.failures

        wrong_pr = grade(CASE_A, observation(HALTED_EXCHANGE_WITH_VALIDATED_WORK, item(prs=(ready,), approved=(777,))))
        assert "goal subject.pr_review_approved: PR #902: approved PRs: [777]" in wrong_pr.failures

        reviewed = grade(CASE_A, observation(HALTED_EXCHANGE_WITH_VALIDATED_WORK, item(prs=(ready,), approved=(PR,))))
        assert reviewed.passed, reviewed.failures

    @pytest.mark.parametrize(
        "advice",
        [
            f"Do not remove blocked-failed from #{ISSUE}; leave the review gated.",
            f"Never release the review of #{ISSUE} while CI is red.",
            f"Keep #{ISSUE} blocked instead of releasing it to code review.",
            f"You shouldn't clear blocked-failed on #{ISSUE}.",
        ],
    )
    def test_advice_against_the_release_is_not_the_remedy(self, advice: str) -> None:
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                item(issue_labels=("blocked-failed",), prs=(pr(),)),
                runs=(run(action("escalate_to_human", advice), summary=GOOD_DIAGNOSIS),),
            ),
        )
        assert card.remedy is not None and card.remedy.verdict is RemedyVerdict.MISSING
        assert not card.passed

    def test_a_release_next_to_a_do_not_reset_is_still_the_remedy(self) -> None:
        """The real tech lead's shape: affirm the release, forbid the reset."""
        advice = f"**Human action requested: release #{ISSUE} to code review. Do NOT reset or retry it.**"
        card = grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                item(issue_labels=("blocked-failed",), prs=(pr(),)),
                runs=(run(action("escalate_to_human", advice), summary=GOOD_DIAGNOSIS),),
            ),
        )
        assert card.remedy is not None and card.remedy.verdict is RemedyVerdict.ACCEPTABLE


def test_a_negation_in_an_earlier_sentence_does_not_flip_the_release() -> None:
    """Clauses are judged apart: "Do not reset it. Release the review." advises the release."""
    advice = f"Do not reset #{ISSUE} or close PR #{PR}. Release the review so it runs."
    card = grade(
        CASE_B,
        observation(
            BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
            item(issue_labels=("blocked-failed",), prs=(pr(),)),
            runs=(run(action("escalate_to_human", advice), summary=GOOD_DIAGNOSIS),),
        ),
    )
    assert card.remedy is not None and card.remedy.verdict is RemedyVerdict.ACCEPTABLE


class TestRoundFiveFindings:
    def _b(self, *, summary: str, advice: str):
        return grade(
            CASE_B,
            observation(
                BLOCKED_ISSUE_GREEN_PR_AWAITING_REVIEW,
                item(issue_labels=("blocked-failed",), prs=(pr(),)),
                runs=(run(action("escalate_to_human", advice), summary=summary),),
            ),
        )

    def test_a_release_negated_after_the_term_is_not_advised(self) -> None:
        card = self._b(summary=GOOD_DIAGNOSIS, advice=f"For #{ISSUE}: remove blocked-failed is not the answer; leave review gated.")
        assert card.remedy is not None and card.remedy.verdict is RemedyVerdict.MISSING

    def test_a_release_after_a_contrast_is_advised(self) -> None:
        card = self._b(summary=GOOD_DIAGNOSIS, advice=f"Do not reset or retry #{ISSUE}, but remove blocked-failed.")
        assert card.remedy is not None and card.remedy.verdict is RemedyVerdict.ACCEPTABLE

    def test_a_denied_cause_split_by_a_contrast_is_no_diagnosis(self) -> None:
        card = self._b(
            summary=f"Issue #{ISSUE} has blocked-failed, but that label does not prevent code review. The real cause is red CI.",
            advice=GOOD_ESCALATION,
        )
        assert card.diagnosis is not None and card.diagnosis.missing == ()
        assert card.diagnosis.evidence_clause == ""
        assert "diagnosis: no single clause connects the cause (concepts only named apart)" in card.failures
        assert not card.passed

    def test_the_connecting_clause_is_reported_as_evidence(self) -> None:
        card = self._b(summary=GOOD_DIAGNOSIS, advice=GOOD_ESCALATION)
        assert card.diagnosis is not None
        # "though" is a contrast word, so the clause ends before it.
        assert card.diagnosis.evidence_clause == f"issue #{ISSUE} carries blocked-failed, so its pr #{PR} never gets its code review even"
        assert "evidence: 'issue #901 carries blocked-failed" in render_summary(card)

    def test_a_second_pr_strands_the_published_work(self) -> None:
        """Round 5 F3: the newest PR reviewed and ready while the original draft is left behind."""
        original = pr(number=PR, state=PullRequestState.DRAFT)
        redo = pr(number=PR + 50, state=PullRequestState.READY, labels=("code-reviewed",))
        card = grade(CASE_A, observation(HALTED_EXCHANGE_WITH_VALIDATED_WORK, item(prs=(original, redo))))

        assert f"goal subject.single_pull_request: linked PRs: [{PR}, {PR + 50}]" in card.failures
        assert not card.passed
